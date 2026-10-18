import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

database_url = os.environ["DATABASE_URL"]
engine = create_engine(database_url)

with engine.connect() as connection:
    result = connection.execute(text("SELECT 1"))
    print("PostgreSQL connection successful:", result.scalar_one())

engine.dispose()
