import configparser
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from fastapi import HTTPException
from pydantic_settings import BaseSettings, SettingsConfigDict


def split_ini_list(raw: str | None) -> list[str]:
    # Given a string value from an .ini file, splits it into multiple
    # strings over line boundaries, using the typical form supported
    # in .ini files.
    # e.g.
    #
    #    [section]
    #    my-setting=
    #      foo
    #      bar
    #
    # => returns ["foo", "bar"]

    if not raw:
        return []

    return [elem.strip() for elem in raw.split("\n") if elem.strip()]


@dataclass
class CacheFlushRule:
    name: str
    """Name of this rule (from the config file)."""

    templates: list[str]
    """List of URL/ARL templates.

    Each template may be either:
    - a base URL, e.g. "https://cdn.example.com/cdn-root"
    - an ARL template, e.g. "S/=/123/22334455/{ttl}/cdn1.example.com/{path}"

    Templates may contain 'ttl' and 'path' placeholders to be substituted
    when calculating cache keys for flush.
    When there is no 'path' in a template, the path will instead be
    appended.
    """

    includes: list[re.Pattern[str]]
    """List of patterns applied to decide whether this rule is
    applicable to any given path.

    Patterns are non-anchored regular expressions.
    A path must match at least one pattern in order for cache flush
    to occur for that path.

    There is a default pattern of ".*", meaning that all paths will
    be included by default.

    Note that these includes are evaluated *after* the set of paths
    for flush have already been filtered to include only entry points
    (e.g. repomd.xml and other mutable paths). It is not possible to
    use this mechanism to enable cache flushing of non-entry-point
    paths.
    """

    excludes: list[re.Pattern[str]]
    """List of patterns applied to decide whether this rule should
    be skipped for any given path.

    Patterns are non-anchored regular expressions.
    If a path matches any pattern, cache flush won't occur.

    excludes are applied after includes.
    """

    def matches(self, path: str) -> bool:
        """True if this rule matches the given path."""

        # We always match against absolute paths with a leading /,
        # regardless of how the input was formatted.
        path = "/" + path.removeprefix("/")

        # Must match at least one 'includes'.
        for pattern in self.includes:
            if pattern.search(path):
                break
        else:
            return False

        # Must not match any 'excludes'.
        for pattern in self.excludes:
            if pattern.search(path):
                return False

        return True

    @classmethod
    def load_all(
        cls: type["CacheFlushRule"],
        config: configparser.ConfigParser,
        env_section: str,
        names: Iterable[str],
    ) -> list["CacheFlushRule"]:

        out: list[CacheFlushRule] = []
        for rule_name in names:
            section_name = f"cache_flush.{rule_name}"
            templates = split_ini_list(config.get(section_name, "templates"))
            includes = [
                re.compile(s)
                for s in split_ini_list(
                    config.get(section_name, "includes", fallback=".*")
                )
            ]
            excludes = [
                re.compile(s)
                for s in split_ini_list(
                    config.get(section_name, "excludes", fallback=None)
                )
            ]
            out.append(
                cls(
                    name=rule_name,
                    templates=templates,
                    includes=includes,
                    excludes=excludes,
                )
            )

        # backwards-compatibility: if no rules were defined, but old-style
        # cache flush config was specified, read it into a rule with default
        # 'includes' and 'excludes'.
        if not names and (
            config.has_option(env_section, "cache_flush_urls")
            or config.has_option(env_section, "cache_flush_arl_templates")
        ):
            out.append(
                cls(
                    name=f"{env_section}-legacy",
                    templates=split_ini_list(
                        config.get(
                            env_section, "cache_flush_urls", fallback=None
                        )
                    )
                    + split_ini_list(
                        config.get(
                            env_section,
                            "cache_flush_arl_templates",
                            fallback=None,
                        )
                    ),
                    includes=[re.compile(r".*")],
                    excludes=[],
                )
            )

        return out


class Environment(object):
    def __init__(
        self,
        name,
        aws_profile,
        bucket,
        table,
        config_table,
        cdn_url,
        cdn_key_id,
        cache_flush_rules=None,
    ):
        self.name = name
        self.aws_profile = aws_profile
        self.bucket = bucket
        self.table = table
        self.config_table = config_table
        self.cdn_url = cdn_url
        self.cdn_key_id = cdn_key_id
        self.cache_flush_rules: list[CacheFlushRule] = cache_flush_rules or []

    @property
    def cdn_private_key(self):
        return os.getenv("EXODUS_GW_CDN_PRIVATE_KEY_%s" % self.name.upper())

    @property
    def fastpurge_enabled(self) -> bool:
        """True if this environment has fastpurge-based cache flushing enabled.

        When True, it is guaranteed that all needed credentials for fastpurge
        are available for this environment.
        """
        return (
            # There must be at least one cache flush rule in config...
            bool(self.cache_flush_rules)
            # ... and *all* fastpurge credentials must be set
            and self.fastpurge_access_token
            and self.fastpurge_client_secret
            and self.fastpurge_client_token
            and self.fastpurge_host
        )

    @property
    def fastpurge_client_secret(self):
        return os.getenv(
            "EXODUS_GW_FASTPURGE_CLIENT_SECRET_%s" % self.name.upper()
        )

    @property
    def fastpurge_host(self):
        return os.getenv("EXODUS_GW_FASTPURGE_HOST_%s" % self.name.upper())

    @property
    def fastpurge_access_token(self):
        return os.getenv(
            "EXODUS_GW_FASTPURGE_ACCESS_TOKEN_%s" % self.name.upper()
        )

    @property
    def fastpurge_client_token(self):
        return os.getenv(
            "EXODUS_GW_FASTPURGE_CLIENT_TOKEN_%s" % self.name.upper()
        )


class MigrationMode(str, Enum):
    upgrade = "upgrade"
    model = "model"
    none = "none"


class Settings(BaseSettings):
    # Settings for the server.
    #
    # Most settings defined here can be overridden by an environment variable
    # of the same name, prefixed with "EXODUS_GW_". Please add doc strings only
    # for those (and not for other computed fields, like 'environments'.)

    call_context_header: str = "X-RhApiPlatform-CallContext"
    """Name of the header from which to extract call context (for authentication
    and authorization).
    """

    upload_meta_fields: dict[str, str] = {}
    """Permitted metadata field names for s3 uploads and their regex
    for validation. E.g., "exodus-migration-md5": "^[0-9a-f]{32}$"
    """

    publish_paths: dict[str, dict[str, list[str]]] = {}
    """A set of user or service accounts which are only authorized to publish to a
    particular set of path(s) in a given CDN environment and the regex(es) describing
    the paths to which the user or service account is authorized to publish. The user or
    service account will be prevented from publishing to any paths that do not match the
    defined regular expression(s).
    E.g., '{"pre": {"fake-user":
    ["^(/content)?/origin/files/sha256/[0-f]{2}/[0-f]{64}/[^/]{1,300}$"]}}'

    Any user or service account not included in this configuration is considered to have
    unrestricted publish access (i.e., can publish to any path).
    """

    log_config: dict[str, Any] = {
        "version": 1,
        "incremental": True,
        "disable_existing_loggers": False,
    }
    """Logging configuration in dictConfig schema."""

    ini_path: str | None = None
    """Path to an exodus-gw.ini config file with additional settings."""

    environments: list[Environment] = []
    # List of environment objects derived from exodus-gw.ini.

    db_service_user: str = "exodus-gw"
    """db service user name"""
    db_service_pass: str = "exodus-gw"
    """db service user password"""
    db_service_host: str = "exodus-gw-db"
    """db service host"""
    db_service_port: str = "5432"
    """db service port"""

    db_url: str | None = None
    """Connection string for database. If set, overrides the ``db_service_*`` settings."""

    db_reset: bool = False
    """If set to True, drop all DB tables during startup.

    This setting is intended for use during development.
    """

    db_migration_mode: MigrationMode = MigrationMode.upgrade
    """Adjusts the DB migration behavior when the exodus-gw service starts.

    Valid values are:

        upgrade (default)
            Migrate the DB to ``db_migration_revision`` (default latest) when
            the service starts up.

            This is the default setting and should be left enabled for typical
            production use.

        model
            Don't use migrations. Instead, attempt to initialize the database
            from the current version of the internal sqlalchemy model.

            This is intended for use during development while prototyping
            schema changes.

        none
            Don't perform any DB initialization at all.
    """

    db_migration_revision: str = "head"
    """If ``db_migration_mode`` is ``upgrade``, this setting can be used to override
    the target revision when migrating the DB.
    """

    db_session_max_tries: int = 3
    """The maximum number of attempts to recreate a DB session within a request."""

    item_yield_size: int = 5000
    """Number of publish items to load from the service DB at one time."""

    write_batch_size: int = 25
    """Maximum number of items to write to the DynamoDB table at one time."""
    write_max_tries: int = 20
    """Maximum write attempts to the DynamoDB table."""
    write_max_workers: int = 10
    """Maximum number of worker threads used in the DynamoDB batch writes."""
    write_queue_size: int = 1000
    """Maximum number of items the queue can hold at one time."""
    write_queue_timeout: int = 60 * 10
    """Maximum amount of time (in seconds) to wait for queue items.
    Defaults to 10 minutes.
    """
    publish_timeout: int = 24
    """Maximum amount of time (in hours) between updates to a pending publish before
    it will be considered abandoned. Defaults to one day.
    """

    history_timeout: int = 24 * 14
    """Maximum amount of time (in hours) to retain historical data for publishes and
    tasks. Publishes and tasks in a terminal state will be erased after this time has
    passed. Defaults to two weeks.
    """

    path_history_timeout: int = 700
    """Maximum amount of time (in days) to retain data on published paths for
    the purpose of cache flushing.
    """

    task_deadline: int = 2
    """Maximum amount of time (in hours) a task should remain viable. Defaults to two
    hours.
    """

    actor_time_limit: int = 30 * 60000
    """Maximum amount of time (in milliseconds) actors may run. Defaults to 30
    minutes.
    """
    actor_max_backoff: int = 5 * 60000
    """Maximum amount of time (in milliseconds) actors may use while backing
    off retries. Defaults to five (5) minutes.
    """

    entry_point_files: list[str] = [
        "repomd.xml",
        "repomd.xml.asc",
        "PULP_MANIFEST",
        "PULP_MANIFEST.asc",
        "treeinfo",
        "extra_files.json",
    ]
    """List of file names that should be saved for last when publishing."""

    phase2_patterns: list[re.Pattern[str]] = [
        # kickstart repos; note the logic here matches
        # the manual workaround RHELDST-27642
        re.compile(r"/kickstart/.*(?<!\.rpm)$"),
    ]
    """List of patterns which, if any have matched, force a path to
    be handled during phase 2 of commit.

    These patterns are intended for use with repositories not cleanly
    separated between mutable entry points and immutable content.

    For example, in-place updates to kickstart repositories may not
    only modify entry points such as extra_files.json but also
    arbitrary files referenced by that entry point, all of which should
    be processed during phase 2 of commit in order for updates to
    appear atomic.
    """

    autoindex_filename: str = ".__exodus_autoindex"
    """Filename for indexes automatically generated during publish.

    Can be set to an empty string to disable generation of indexes.
    """

    autoindex_partial_excludes: list[str] = ["/kickstart/"]
    """Background processing of autoindexes will be disabled for paths matching
    any of these values.
    """

    config_cache_ttl: int = 2
    """Time (in minutes) config is expected to live in components that consume it.

    Determines the delay for deployment task completion to allow for
    existing caches to expire and the newly deployed config to take effect.
    """

    worker_health_filepath: str = (
        "/tmp/exodus-gw-last-healthy"  # nosec - Bandit doesn't like that /tmp is used.
    )
    """The path to a file used to verify healthiness of a worker. Intended to be used by OCP"""

    worker_keepalive_timeout: int = 60 * 5
    """Background worker keepalive timeout, in seconds. If a worker fails to update its
    status within this time period, it is assumed dead.

    This setting affects how quickly the system can recover from issues such as a worker
    process being killed unexpectedly.
    """

    worker_keepalive_interval: int = 60
    """How often, in seconds, should background workers update their status."""

    cron_cleanup: str = "0 */12 * * *"
    """cron-style schedule for cleanup task.

    exodus-gw will run a cleanup task approximately according to this schedule, removing old
    data from the system."""

    scheduler_interval: int = 15
    """How often, in minutes, exodus-gw should check if a scheduled task is ready to run.

    Note that the cron rules applied to each scheduled task are only as accurate as this
    interval allows, i.e. each rule may be triggered up to ``scheduler_interval`` minutes late.
    """

    scheduler_delay: int = 5
    """Delay, in minutes, after exodus-gw workers start up before any scheduled tasks
    should run."""

    cdn_flush_on_commit: bool = True
    """Whether 'commit' tasks should automatically flush CDN cache for
    affected URLs.

    Only takes effect for environments where cache flush credentials/settings
    have been configured.
    """

    cdn_listing_flush: bool = True
    """Whether listing paths in the config should be flushed while deploying the
    config."""

    cdn_cookie_ttl: int = 60 * 720
    """Time (in seconds) cookies generated by ``cdn-redirect`` remain valid."""

    cdn_signature_timeout: int = 60 * 30
    """Time (in seconds) signed URLs remain valid."""

    cdn_max_expire_days: int = 365
    """Maximum permitted value for ``expire_days`` option on
    ``cdn-access`` endpoint.

    Clients obtaining signed cookies for CDN using ``cdn-access`` will be
    forced to renew their cookies at least this frequently.
    """

    s3_pool_size: int = 3
    """Number of S3 clients to cache"""

    mirror_writes_enabled: bool = True
    """Whether both the original url and releasever alias are written during
     phase 1 commits."""

    model_config = SettingsConfigDict(env_prefix="exodus_gw_")


def load_settings() -> Settings:
    """Return the currently active settings for the server.

    This function will load settings from config files and environment
    variables. It is intended to be called once at application startup.

    Request handler functions should access settings via ``app.state.settings``.
    """

    settings = Settings()
    config = configparser.ConfigParser()

    # Try to find config here by default...
    filenames = [
        os.path.join(os.path.dirname(__file__), "../exodus-gw.ini"),
        "/opt/app/config/exodus-gw.ini",
    ]

    # ...but also allow pointing at a specific config file if this path
    # has been set. Note that putting this at the end gives it the highest
    # precedence, as the behavior is to load all the existing files in
    # order with each one potentially overriding settings from the prior.
    if settings.ini_path:
        filenames.append(settings.ini_path)

    config.read(filenames)

    for logger in config["loglevels"] if "loglevels" in config else []:
        settings.log_config.setdefault("loggers", {})

        log_config = settings.log_config
        dest = log_config if logger == "root" else log_config["loggers"]

        dest.update({logger: {"level": config.get("loglevels", logger)}})

    for env in [sec for sec in config.sections() if sec.startswith("env.")]:
        aws_profile = config.get(env, "aws_profile", fallback=None)
        bucket = config.get(env, "bucket", fallback=None)
        table = config.get(env, "table", fallback=None)
        config_table = config.get(env, "config_table", fallback=None)
        cdn_url = config.get(env, "cdn_url", fallback=None)
        cdn_key_id = config.get(env, "cdn_key_id", fallback=None)

        cache_flush_rule_names = split_ini_list(
            config.get(env, "cache_flush_rules", fallback=None)
        )
        cache_flush_rules = CacheFlushRule.load_all(
            config, env, cache_flush_rule_names
        )

        settings.environments.append(
            Environment(
                name=env.replace("env.", ""),
                aws_profile=aws_profile,
                bucket=bucket,
                table=table,
                config_table=config_table,
                cdn_url=cdn_url,
                cdn_key_id=cdn_key_id,
                cache_flush_rules=cache_flush_rules,
            )
        )

    return settings


def get_environment(env: str, settings: Settings | None = None):
    """Return the corresponding environment object for the given environment
    name.
    """

    settings = settings or load_settings()

    for env_obj in settings.environments:
        if env_obj.name == env:
            return env_obj

    raise HTTPException(
        status_code=404, detail="Invalid environment=%s" % repr(env)
    )
