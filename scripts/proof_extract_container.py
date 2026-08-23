"""Extract pages (tesseract OCR) and dump pages.json — run INSIDE the worker
container (which has tesseract). Extraction is unaffected by the bug fixes;
chunk/index run on the host with the fixed code."""
import asyncio
import json
import os
import sys

sys.path.insert(0, "/app")
os.environ.setdefault("OCR_ENGINE", "tesseract")

from app.pipeline.extractor import extract
from app.core.storage import storage


async def main() -> None:
    pdf = await storage.get_file("books", sys.argv[1])
    pages = await extract(pdf)
    with open("/tmp/pages.json", "w", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False)
    print(f"wrote /tmp/pages.json with {len(pages)} pages")


asyncio.run(main())
