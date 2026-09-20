import uuid
from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime, Text
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict] = {
        str: Text,
        dict: JSON,
        datetime: DateTime(timezone=True),
    }


def new_id() -> str:
    """Return a random UUID4 as a string for client-side primary keys."""
    return str(uuid.uuid4())