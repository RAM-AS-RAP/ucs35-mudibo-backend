"""SQL migrations are the source of truth for the schema; keep these classes in step with them."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base; schema creation is managed exclusively by migrations."""
