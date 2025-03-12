import asyncio
import contextvars
import logging
from datetime import datetime, timezone
from os.path import basename, dirname
from queue import Empty, Full, Queue
from threading import Thread
from typing import Any

import dramatiq
from dramatiq.middleware import CurrentMessage
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, lazyload

from exodus_gw.aws.dynamodb import DynamoDB
from exodus_gw.aws.util import uris_with_aliases
from exodus_gw.database import db_engine
from exodus_gw.models import (
    CommitModes,
    CommitTask,
    Item,
    Publish,
    PublishedPath,
)
from exodus_gw.schemas import PublishStates, TaskStates
from exodus_gw.settings import Settings, get_environment

from .autoindex import AutoindexEnricher
from .cache import Flusher
from .progress import ProgressLogger

LOG = logging.getLogger("exodus-gw")


class _BatchWriter:
    """Use as context manager recommended. Otherwise, the threads must
    be cleaned up manually.
    """

    def __init__(
        self,
        dynamodb: DynamoDB,
        settings: Settings,
        item_count: int,
        message: str,
        delete: bool = False,
    ):
        self.dynamodb = dynamodb
        self.settings = settings
        self.delete = delete
        self.queue: Any = Queue(self.settings.write_queue_size)
        self.sentinel = object()
        self.threads: list[Thread] = []
        self.errors: list[Exception] = []
        self.progress_logger = ProgressLogger(
            message=message,
            items_total=item_count,
        )

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def adjust_total(self, increment: int):
        self.progress_logger.adjust_total(increment)

    def start(self):
        for i in range(self.settings.write_max_workers):
            # These threads are considered as belonging to whatever actor spawned them.
            # This is indicated by propagating the context downwards.
            # Mainly influences logging.
            context = contextvars.copy_context()

            thread = Thread(
                name=f"batchwriter-{i}",
                daemon=True,
                target=context.run,
                args=(self.write_batches,),
            )
            thread.start()
            self.threads.append(thread)

    def stop(self):
        for _ in range(len(self.threads)):
            # A sentinel for each worker to get from the shared queue.
            try:
                self.queue.put(
                    self.sentinel,
                    timeout=self.settings.write_queue_timeout,
                )
            except Full as err:
                self.append_error(err)

        for thread in self.threads:
            thread.join()

        if self.queue.qsize() > 0:
            # Don't warn for excess sentinels.
            if not self.queue.get_nowait() is self.sentinel:
                self.append_error(
                    RuntimeError("Commit incomplete, queue not empty")
                )

        if self.errors:
            raise self.errors[0]

    def append_error(self, err: Exception):
        LOG.error(
            "Exception while submitting batch write(s)",
            exc_info=err,
            extra={"event": "publish", "success": False},
        )
        self.errors.append(err)

    def queue_batches(self, items: list[Item]) -> list[str]:
        batches = self.dynamodb.get_batches(items)
        timeout = self.settings.write_queue_timeout
        queued_item_ids: list[str] = []

        for batch in batches:
            # Don't attempt to put more items on the queue if error(s)
            # already encountered.
            if not self.errors:
                try:
                    self.queue.put(batch, timeout=timeout)
                    queued_item_ids.extend(
                        [str(item.id) for item in list(batch)]
                    )
                except Full as err:
                    self.append_error(err)

        return queued_item_ids

    def write_batches(self):
        """Will either submit batch write or delete requests based on
        the 'delete' attribute.
        """
        while not self.errors:
            # Don't attempt to write more batches if error(s) already
            # encountered by other thread(s).
            try:
                got = self.queue.get(timeout=self.settings.write_queue_timeout)
                if got is self.sentinel:
                    break
                self.dynamodb.write_batch(got, delete=self.delete)
                self.progress_logger.update(len(got))
            except (RuntimeError, ValueError, Empty) as err:
                if err is not Empty:
                    self.append_error(err)
                break


class CommitBase:
    # Acceptable publish states for this type of commit.
    # Subclasses need to override.
    PUBLISH_STATES: list[PublishStates] = []

    def __init__(
        self,
        publish_id: str,
        env: str,
        from_date: str,
        actor_msg_id: str,
        settings: Settings,
    ):
        self.env = env
        self.from_date = from_date
        self.written_item_ids: list[str] = []
        self.settings = settings
        self.db = Session(bind=db_engine(self.settings))
        self.task = self._query_task(actor_msg_id)
        self.publish = self._query_publish(publish_id)
        self.env_obj = get_environment(env)
        self._dynamodb = None

    @property
    def dynamodb(self):
        if self._dynamodb is None:
            self._dynamodb = DynamoDB(
                self.env,
                self.settings,
                self.from_date,
                self.env_obj,
                self.task.deadline,
                self.should_mirror_writes,
            )
        return self._dynamodb

    @property
    def should_mirror_writes(self):
        return False

    @property
    def task_ready(self) -> bool:
        task = self.task
        now = datetime.utcnow()
        if task.state in (TaskStates.complete, TaskStates.failed):
            LOG.warning(
                "Task %s in unexpected state, '%s'",
                task.id,
                task.state,
                extra={"event": "publish"},
            )
            return False
        if task.deadline and (task.deadline.timestamp() < now.timestamp()):
            LOG.warning(
                "Task %s expired at %s",
                task.id,
                task.deadline,
                extra={"event": "publish", "success": False},
            )
            self.on_failed()
            self.db.commit()
            return False
        return True

    @property
    def publish_ready(self) -> bool:
        if self.publish.state in self.PUBLISH_STATES:
            return True
        LOG.warning(
            "Publish %s in unexpected state, '%s'",
            self.publish.id,
            self.publish.state,
            extra={"event": "publish"},
        )
        return False

    def check_item(self, item: Item):
        # Last chance to verify item before writing to DynamoDB.
        if not item.object_key:
            # Incoming items are always verified to have either
            # object_key OR link_to, with link_to then being resolved to an
            # object_key at the time commit is enqueued, so there is no
            # legitimate way to get here.
            #
            # If this error is hit, it's always a bug in exodus-gw (and e.g. not
            # bad input from client). For example, it could be a bug in the
            # link_to resolution logic.
            raise ValueError("BUG: missing object_key for %s" % item.web_uri)

    def is_phase2(self, item: Item) -> bool:
        # Return True if item should be handled in phase 2 of commit.
        name = basename(item.web_uri)
        if (
            name == self.settings.autoindex_filename
            or name in self.settings.entry_point_files
        ):
            # Typical entrypoint
            return True

        for pattern in self.settings.phase2_patterns:
            # Arbitrary patterns from settings.
            # e.g. this is where /kickstart/ is expected to be handled.
            if pattern.search(item.web_uri):
                LOG.debug(
                    "%s: phase2 forced via pattern %s", item.web_uri, pattern
                )
                return True

        return False

    @property
    def item_select(self):
        # Returns base of the SELECT query to find all items for commit.
        #
        # Can be overridden in subclasses.
        return (
            select(Item)
            .where(Item.publish_id == self.publish.id, Item.dirty == True)
            .order_by(Item.web_uri)
        )

    @property
    def item_count(self):
        # Items included in publish.
        #
        # Intentionally not cached because the item count can be
        # changed during commit (e.g. autoindex)
        return (
            self.db.query(Item)
            .filter(Item.publish_id == self.publish.id)
            .count()
        )

    @property
    def has_items(self) -> bool:
        if self.item_count > 0:
            LOG.debug(
                "Prepared to write %d item(s) for publish %s",
                self.item_count,
                self.publish.id,
                extra={"event": "publish"},
            )
            return True
        LOG.debug(
            "No items to write for publish %s",
            self.publish.id,
            extra={"event": "publish", "success": True},
        )
        return False

    def _query_task(self, actor_msg_id: str):
        return (
            self.db.query(CommitTask)
            .filter(CommitTask.id == actor_msg_id)
            .first()
        )

    def _query_publish(self, publish_id: str):
        publish = (
            self.db.query(Publish)
            .filter(Publish.id == publish_id)
            .options(lazyload(Publish.items))
            .first()
        )
        return publish

    def should_write(self) -> bool:
        if not self.task_ready:
            return False
        if not self.publish_ready:
            self.task.state = TaskStates.failed
            self.db.commit()
            return False
        if not self.has_items:
            # An empty commit just instantly succeeds...
            self.on_succeeded()
            self.db.commit()
            return False
        return True

    @property
    def written_item_ids_batched(self):
        """self.written_item_ids, but batched into chunks."""
        chunk_size = self.settings.item_yield_size
        for index in range(0, len(self.written_item_ids), chunk_size):
            batch = self.written_item_ids[index : index + chunk_size]
            if batch:
                yield batch

    def write_publish_items(self) -> list[Item]:
        """Query for publish items, batching and yielding them to
        conserve memory, and submit batch write requests via
        _BatchWriter.

        The implementation on the base class handles phase1 items only
        and returns the list of uncommitted phase2 items.
        Subclasses should override this.
        """

        statement = self.item_select.with_for_update().execution_options(
            yield_per=self.settings.item_yield_size
        )
        partitions = self.db.execute(statement).partitions()

        # Save any entry point items to publish last.
        final_items: list[Item] = []

        wrote_count = 0

        # The queue is empty at this point but we want to write batches
        # as they're put rather than wait until they're all queued.
        with _BatchWriter(
            self.dynamodb,
            self.settings,
            self.item_count,
            "Writing phase 1 items",
        ) as bw:
            # Being queuing item batches.
            for partition in partitions:
                items: list[Item] = []

                # Flatten partition and extract any entry point items.
                for row in partition:
                    item: Item = row.Item

                    self.check_item(item)

                    if self.is_phase2(item):
                        LOG.debug(
                            "Delayed write for %s",
                            item.web_uri,
                            extra={"event": "publish"},
                        )
                        final_items.append(item)
                        bw.adjust_total(-1)
                    else:
                        items.append(item)

                wrote_count += len(items)

                # Submit items to be batched and queued, saving item
                # IDs for rollback and marking as no longer dirty.
                self.written_item_ids.extend(bw.queue_batches(items))

        return final_items

    def rollback_publish_items(self, exception: Exception) -> None:
        """Breaks the list of item IDs into chunks and iterates over
        each, querying corresponding items and submitting batch delete
        requests.
        """

        LOG.warning(
            "Rolling back %d item(s) due to error",
            len(self.written_item_ids),
            exc_info=exception,
            extra={"event": "publish"},
        )

        for item_ids in self.written_item_ids_batched:
            with _BatchWriter(
                self.dynamodb,
                self.settings,
                len(item_ids),
                "Rolling back",
                delete=True,
            ) as bw:
                items = self.db.query(Item).filter(Item.id.in_(item_ids))
                bw.queue_batches(items)

    def on_succeeded(self):
        # Called when commit operation has succeeded.
        # The task has succeeded...
        self.task.state = TaskStates.complete

        # And any written items are no longer dirty.
        # We know they can't have been updated while we were running
        # because we selected them "FOR UPDATE" earlier.
        for item_ids in self.written_item_ids_batched:
            self.db.query(Item).filter(Item.id.in_(item_ids)).update(
                {Item.dirty: False}
            )

    def on_failed(self):
        # Called when commit operation has failed.
        self.task.state = TaskStates.failed

    def pre_write(self):
        # Any steps prior to DynamoDB write.
        # Base implementation does nothing.
        pass


class CommitPhase1(CommitBase):
    # phase1 commit is allowed to proceed in either of these states.
    PUBLISH_STATES = [PublishStates.committing, PublishStates.pending]

    @property
    def should_mirror_writes(self):
        return self.settings.mirror_writes_enabled

    @property
    def item_select(self):
        # Query for items to be handled by phase1 commit.
        #
        # During phase1 commit, it is possible that the publish might
        # have other items still being added as symlinks (i.e.
        # link_to set but object_key unset) which have not yet been
        # resolved. We won't be able to publish those yet, so extend
        # the query to filter them out.
        return super().item_select.where(
            func.coalesce(Item.object_key, "") != ""  # pylint: disable=E1102
        )

    def write_publish_items(self) -> list[Item]:
        final_items = super().write_publish_items()

        # In phase1 we don't process the final items, but we'll log
        # how many have been left for later.
        LOG.info(
            "Phase 1: committed %s items, phase 2: %s items remaining",
            len(self.written_item_ids),
            len(final_items),
            extra={"event": "publish"},
        )

        return []


class CommitPhase2(CommitBase):
    PUBLISH_STATES = [PublishStates.committing]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.flush_paths: list[str] = []

    def flush_cache(self) -> None:
        if self.settings.cdn_flush_on_commit:
            flusher = Flusher(
                self.flush_paths,
                self.settings,
                self.env,
                self.dynamodb.aliases_for_flush,
            )
            flusher.run()

    def add_flush_paths(self, paths: list[str]):
        for path in paths:
            if basename(path) == self.settings.autoindex_filename:
                # For an autoindex, we need to flush cache for the "directory"
                # containing the index, not the autoindex file itself.
                # e.g. if we just published "/some/dir/<autoindex>"
                # then we need to flush "/some/dir/".
                path = dirname(path)
                if not path.endswith("/"):
                    path = path + "/"
            self.flush_paths.append(path)

    def write_publish_items(self) -> list[Item]:
        final_items = super().write_publish_items()

        # In phase2 we go ahead and write the final items.
        LOG.info(
            "Phase 1: committed %s items, phase 2: committing %s items",
            len(self.written_item_ids),
            len(final_items),
            extra={"event": "publish"},
        )

        # Start a new context manager to raise any errors from previous
        # and skip additional write attempts.
        with _BatchWriter(
            self.dynamodb,
            self.settings,
            len(final_items),
            "Writing phase 2 items",
        ) as bw:
            if final_items:
                self.written_item_ids.extend(bw.queue_batches(final_items))
                self.add_flush_paths([item.web_uri for item in final_items])

        # Flush cache for what we've just written.
        self.flush_cache()

        return []

    def rollback_publish_items(self, exception: Exception) -> None:
        super().rollback_publish_items(exception)

        # If we've rolled back, we should also attempt to flush cache
        # to restore CDN edge as close as possible back to the prior state.
        self.flush_cache()

    def pre_write(self):
        # If any index files should be automatically generated for this publish,
        # generate and add them now before processing the items.
        enricher = AutoindexEnricher(self.publish, self.env, self.settings)
        asyncio.run(enricher.run())

    # phase2 commit also completes the publish, one way or another.
    def on_succeeded(self):
        super().on_succeeded()

        # Record info on the published paths using an upsert.
        updated_paths = uris_with_aliases(
            self.flush_paths, self.dynamodb.aliases_for_flush
        )
        if updated_paths:
            now = datetime.now(tz=timezone.utc)
            statement = insert(PublishedPath).values(
                [
                    {"env": self.env, "web_uri": path, "updated": now}
                    for path in updated_paths
                ]
            )
            statement = statement.on_conflict_do_update(
                index_elements=["env", "web_uri"],
                set_={
                    c.name: c for c in statement.excluded if not c.primary_key
                },
            )
            self.db.execute(statement)

        self.publish.state = PublishStates.committed

    def on_failed(self):
        super().on_failed()
        self.publish.state = PublishStates.failed


@dramatiq.actor(
    time_limit=Settings().actor_time_limit,
    max_backoff=Settings().actor_max_backoff,
)
def commit(
    publish_id: str,
    env: str,
    from_date: str,
    commit_mode: str | None = None,
    settings: Settings = Settings(),
) -> None:
    message = CurrentMessage.get_current_message()
    assert message
    actor_msg_id = message.message_id

    commit_mode = commit_mode or CommitModes.phase2.value
    commit_class: type[CommitBase] = {
        CommitModes.phase1.value: CommitPhase1,
        CommitModes.phase2.value: CommitPhase2,
    }[commit_mode]
    commit_obj = commit_class(
        publish_id, env, from_date, actor_msg_id, settings
    )

    if not commit_obj.should_write():
        return

    commit_obj.task.state = TaskStates.in_progress
    commit_obj.db.commit()

    # Do any relevant commit steps prior to the main DynamoDB writes.
    # Anything which happens here is not covered by rollback.
    commit_obj.pre_write()

    try:
        commit_obj.write_publish_items()
        commit_obj.on_succeeded()
        commit_obj.db.commit()
    except Exception as exc_info:  # pylint: disable=broad-except
        LOG.exception(
            "Task %s encountered an error",
            commit_obj.task.id,
            extra={"event": "publish", "success": False},
        )
        try:
            commit_obj.rollback_publish_items(exc_info)
        finally:
            commit_obj.on_failed()
            commit_obj.db.commit()
        return
