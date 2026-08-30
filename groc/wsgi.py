"""WSGI entrypoint for production servers, e.g. `gunicorn groc.wsgi:app`.

Also the Vercel entrypoint, imported by api/index.py -- Vercel's Postgres/Neon
storage integration auto-injects a connection string env var (the exact name
varies: DATABASE_URL, POSTGRES_URL, ...), which db.connect() recognizes as a
Postgres DSN (vs. a plain SQLite file path) and routes accordingly.
"""
import os

from .webapp import create_app

db_path = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("POSTGRES_URL")
    or os.environ.get("GROC_DB_PATH", "groc.db")
)
app = create_app(db_path=db_path)
