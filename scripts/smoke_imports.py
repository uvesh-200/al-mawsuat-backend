import app.main  # noqa: F401
import app.workers.celery_app  # noqa: F401
import app.workers.processor  # noqa: F401
from alembic.config import Config  # noqa: F401

print("IMPORTS-OK")
