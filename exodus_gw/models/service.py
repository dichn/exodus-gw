from datetime import datetime

from sqlalchemy import Column, DateTime, String, event
from sqlalchemy.types import Uuid

from .base import Base


class Task(Base):

    __tablename__ = "tasks"

    id = Column(Uuid(as_uuid=False), primary_key=True)
    publish_id = Column(Uuid(as_uuid=False))
    state = Column(String, nullable=False)
    updated = Column(DateTime())
    deadline = Column(DateTime())


@event.listens_for(Task, "before_update")
def task_before_update(_mapper, _connection, task):
    task.updated = datetime.utcnow()
