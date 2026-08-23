"""One-shot Phase 1 Step A import rewriter (UTF-8 safe).

Run from repo root:  python scripts/migrate_step_a.py
Rewrites imports after moving:
  app/config.py            -> app/core/config.py
  app/models/db.py         -> app/core/db.py
  app/storage/minio_client -> app/core/storage.py
  app/core/auth.py         -> app/core/security.py
"""
from pathlib import Path
import subprocess

REPLACEMENTS = [
    ("from app.config import", "from app.core.config import"),
    ("import app.config", "import app.core.config"),
    ("app.config.settings", "app.core.config.settings"),
    ("from app.models.db import", "from app.core.db import"),
    ("from app.storage.minio_client import", "from app.core.storage import"),
    ("from app.storage import", "from app.core import"),
    ("from app.core.auth import", "from app.core.security import"),
    ("app.core.auth.", "app.core.security."),
]


def main():
    files = subprocess.run(
        ["git", "grep", "-l",
         "-E", r"app\.config|app\.models\.db|app\.storage|app\.core\.auth",
         "--", "*.py"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    for f in files:
        p = Path(f)
        text = p.read_text(encoding="utf-8")
        orig = text
        for old, new in REPLACEMENTS:
            text = text.replace(old, new)
        if text != orig:
            p.write_text(text, encoding="utf-8", newline="\n")
            print("rewrote", f)
    print("done:", len(files), "files scanned")


if __name__ == "__main__":
    main()
