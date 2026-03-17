"""Render/Gunicorn compatibility entrypoint."""

from app import app as application

# Some setups expect `app`, some expect `application`.
app = application
