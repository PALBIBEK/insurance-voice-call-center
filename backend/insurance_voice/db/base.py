import datetime

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {
        dict: sa.JSON,
        list: sa.JSON,
    }


class TimestampMixin:
    created_at: Mapped[datetime.datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
