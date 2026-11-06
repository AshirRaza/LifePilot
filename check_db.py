import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

from lifepilot.db.url import normalize_database_url  # noqa: E402

load_dotenv()

database_url = normalize_database_url(os.environ["DATABASE_URL"])
engine = create_engine(
    database_url,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10},
)

try:
    with engine.connect() as connection:
        result = connection.execute(text("SELECT 1"))
        print("PostgreSQL connection successful:", result.scalar_one())
finally:
    engine.dispose()
