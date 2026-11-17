import os
from functools import lru_cache

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from lifepilot.db.url import normalize_database_url


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Create a SQLAlchemy engine from DATABASE_URL in the environment."""
    load_dotenv()
    database_url = normalize_database_url(os.environ["DATABASE_URL"])
    # Bounded pool for concurrent API workers; pre-ping avoids stale connections.
    pool_size = int(os.environ.get("LIFEPILOT_DB_POOL_SIZE") or "5")
    max_overflow = int(os.environ.get("LIFEPILOT_DB_MAX_OVERFLOW") or "10")
    pool_timeout = int(os.environ.get("LIFEPILOT_DB_POOL_TIMEOUT") or "30")
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=max(1, pool_size),
        max_overflow=max(0, max_overflow),
        pool_timeout=max(1, pool_timeout),
        connect_args={"connect_timeout": 10},
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """Return a session factory bound to the shared engine."""
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)
