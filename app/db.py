from __future__ import annotations

from sqlmodel import Session, create_engine, Engine

_engine: Engine | None = None


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("DB not initialised — has lifespan run?")
    return _engine


def get_session() -> Session:
    return Session(get_engine())
