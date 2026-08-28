"""WSGI entrypoint for production servers, e.g. `gunicorn groc.wsgi:app`."""
import os

from .webapp import create_app

app = create_app(db_path=os.environ.get("GROC_DB_PATH", "groc.db"))
