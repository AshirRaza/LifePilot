import os
from functools import lru_cache

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Create a SQLAlchemy engine from DATABASE_URL in the environment."""
    load_dotenv()
    database_url = os.environ["DATABASE_URL"]
    return create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10},
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """Return a session factory bound to the shared engine."""
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)
