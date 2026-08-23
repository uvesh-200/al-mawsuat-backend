"""Phase 1 Step C: rewrite workers.celery_app references to app.workers."""
from pathlib import Path

FILES = [
    "app/features/books/admin_router.py",
    "app/features/books/router.py",
    "app/workers/celery_app.py",
    "app/workers/processor.py",
]

for f in FILES:
    p = Path(f)
    t = p.read_text(encoding="utf-8")
    t2 = (
        t.replace("from workers.celery_app import",
                  "from app.workers.celery_app import")
        .replace('"workers.celery_app.', '"app.workers.celery_app.')
    )
    if t2 != t:
        p.write_text(t2, encoding="utf-8", newline="\n")
        print("updated", f)
