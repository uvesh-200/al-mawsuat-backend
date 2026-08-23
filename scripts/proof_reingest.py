"""Re-ingest harness: runs the REAL pipeline (extract -> chunk -> embed ->
index) with the fixed code, against the live docker services. Mirrors
workers/processor.py but runs from the host venv so the fixed code is used.

Usage: python scripts/proof_reingest.py --book-id <id> --minio-path <p> \
       --title <t> [--language ar] [--book-type <t>] [--no-delete]
"""
import argparse
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MINIO_ENDPOINT", "localhost:9000")

sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

from app.core.config import settings
from app.features.ingestion.chunker import chunk as chunk_text
from app.features.ingestion.extractor import extract
from app.features.ingestion.indexer import index_to_meilisearch, index_to_qdrant
from app.core.embedder import embed_texts
from app.core.storage import storage

logger = logging.getLogger("proof")


async def _delete_points(book_id: str) -> None:
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue
    client = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=10)
    try:
        await client.delete(
            collection_name="documents",
            points_selector=FilterSelector(
                filter=Filter(must=[
                    FieldCondition(key="book_id", match=MatchValue(value=book_id)),
                    FieldCondition(key="tenant_id", match=MatchValue(value=settings.DEFAULT_TENANT_ID)),
                ])
            ),
        )
    finally:
        await client.close()
    import meilisearch
    ms = meilisearch.Client(settings.MEILISEARCH_URL, settings.MEILISEARCH_KEY)
    hits = ms.index("documents").search("", opt_params={"filter": [f"book_id={book_id}"], "limit": 1000}).get("hits", [])
    ids = [h["id"] for h in hits]
    if ids:
        ms.index("documents").delete_documents(ids)


async def run(args: argparse.Namespace) -> None:
    if args.delete_first:
        await _delete_points(args.book_id)
        logger.info("deleted existing points for book %s", args.book_id)

    if args.pages_file:
        with open(args.pages_file, encoding="utf-8") as f:
            pages = json.load(f)
        logger.info("loaded pages from %s: %d pages", args.pages_file, len(pages))
    else:
        pdf_bytes = await storage.get_file(settings.MINIO_BUCKET_BOOKS, args.minio_path)
        logger.info("pdf_bytes=%d", len(pdf_bytes))
        pages = await extract(pdf_bytes)
        logger.info("extracted pages=%d words=%d", len(pages), sum(len(p["words"]) for p in pages))

    chunks = await asyncio.to_thread(chunk_text, pages, settings.DEFAULT_TENANT_ID)
    for c in chunks:
        c["book_id"] = args.book_id
        c["book_name"] = args.title
        c["author"] = args.author or ""
        c["language"] = args.language
        c["book_type"] = args.book_type or ""
        c["minio_path"] = args.minio_path
    logger.info("chunks=%d", len(chunks))
    for c in chunks:
        logger.info(
            "  chunk pages=%s..%s tokens=%d bbox=%s page_bboxes=%s page_offsets=%s text=%r",
            c["page_start"], c["page_end"], c["token_count"],
            c["bbox"], c.get("page_bboxes"), c.get("page_offsets"), c["text"][:80],
        )

    vectors = await embed_texts([c["text"] for c in chunks])
    await index_to_qdrant(chunks, vectors)
    await index_to_meilisearch(chunks)
    logger.info("indexing done")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--book-id", required=True)
    ap.add_argument("--minio-path", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--author", default="")
    ap.add_argument("--language", default="ar")
    ap.add_argument("--book-type", default="")
    ap.add_argument("--no-delete", action="store_true")
    ap.add_argument("--pages-file", default="")
    args = ap.parse_args()
    args.delete_first = not args.no_delete
    asyncio.run(run(args))
