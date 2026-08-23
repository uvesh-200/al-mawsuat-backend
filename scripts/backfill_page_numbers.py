#!/usr/bin/env python
"""Backfill page numbers for previously ingested books.

This script re-extracts the printed footer page number from each PDF page and
updates the ``page_start`` / ``page_end`` payloads in both Qdrant and
Meilisearch.

Usage::

    # Dry-run (no writes) — show what would change
    python scripts/backfill_page_numbers.py --dry-run

    # Fix a single book
    python scripts/backfill_page_numbers.py --book-id 394ed100-6eb5-45d9-bd7f-f73466e72b05

    # Fix all books
    python scripts/backfill_page_numbers.py --all-books

Run from the project root with the .venv activated (or inside the container).
Services (Qdrant, Meilisearch, MinIO, Postgres) must be reachable.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Allow running from project root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

import fitz
import meilisearch
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointIdsList
from sqlalchemy import select

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.models.tables import Book
from app.features.ingestion.extractor import _extract_footer_page_number
from app.core.storage import storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("backfill_page_numbers")

QDRANT_COLLECTION = "documents"
MEILISEARCH_INDEX = "documents"
BATCH_SIZE = 100


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_all_books() -> list[Book]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Book).where(Book.status == "ready"))
        return list(result.scalars().all())


async def _get_book(book_id: str) -> Book | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Book).where(Book.id == book_id)
        )
        return result.scalar_one_or_none()


def _build_physical_to_footer_map(pdf_bytes: bytes) -> dict[int, int | None]:
    """Return {physical_page (1-based): footer_page_num | None} for all pages."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    mapping: dict[int, int | None] = {}
    for i in range(len(doc)):
        raw_text = doc[i].get_text("text")
        mapping[i + 1] = _extract_footer_page_number(raw_text)
    doc.close()
    return mapping


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------

async def _fetch_qdrant_points(client: AsyncQdrantClient, book_id: str) -> list:
    """Fetch all Qdrant points for a book (paginating with scroll)."""
    points = []
    next_offset = None
    while True:
        result, next_offset = await client.scroll(
            collection_name=QDRANT_COLLECTION,
            scroll_filter=Filter(
                must=[FieldCondition(key="book_id", match=MatchValue(value=book_id))]
            ),
            limit=BATCH_SIZE,
            offset=next_offset,
            with_payload=True,
            with_vectors=False,
        )
        points.extend(result)
        if next_offset is None:
            break
    return points


async def _patch_qdrant(
    client: AsyncQdrantClient,
    points: list,
    mapping: dict[int, int | None],
    dry_run: bool,
) -> tuple[int, int]:
    """Patch page_start / page_end in Qdrant. Returns (patched, skipped)."""
    patched = skipped = 0
    batch_ids: list[str] = []
    batch_payloads: list[dict] = []

    for pt in points:
        phys_start = (
            pt.payload.get("physical_page_start")
            or pt.payload.get("physical_page")
            or pt.payload.get("page_start")
        )
        phys_end = (
            pt.payload.get("physical_page_end")
            or pt.payload.get("physical_page")
            or pt.payload.get("page_end")
        )

        new_start = mapping.get(phys_start)
        new_end = mapping.get(phys_end)

        # Use footer number where available; fall back to existing value
        final_start = new_start if new_start is not None else pt.payload.get("page_start")
        final_end = new_end if new_end is not None else pt.payload.get("page_end")

        if (
            final_start == pt.payload.get("page_start")
            and final_end == pt.payload.get("page_end")
        ):
            skipped += 1
            continue

        logger.info(
            "  [%s] page_start %s→%s  page_end %s→%s%s",
            str(pt.id)[:8],
            pt.payload.get("page_start"), final_start,
            pt.payload.get("page_end"), final_end,
            " [DRY RUN]" if dry_run else "",
        )

        if not dry_run:
            batch_ids.append(pt.id)
            batch_payloads.append({"page_start": final_start, "page_end": final_end})
            patched += 1

            if len(batch_ids) >= BATCH_SIZE:
                await _flush_qdrant_batch(client, batch_ids, batch_payloads)
                batch_ids.clear()
                batch_payloads.clear()
        else:
            patched += 1

    if not dry_run and batch_ids:
        await _flush_qdrant_batch(client, batch_ids, batch_payloads)

    return patched, skipped


async def _flush_qdrant_batch(
    client: AsyncQdrantClient, ids: list, payloads: list[dict]
) -> None:
    for pt_id, payload in zip(ids, payloads):
        await client.set_payload(
            collection_name=QDRANT_COLLECTION,
            payload=payload,
            points=PointIdsList(points=[pt_id]),
        )


# ---------------------------------------------------------------------------
# Meilisearch
# ---------------------------------------------------------------------------

def _patch_meilisearch(
    book_id: str,
    mapping: dict[int, int | None],
    dry_run: bool,
) -> tuple[int, int]:
    """Patch page_start / page_end in Meilisearch. Returns (patched, skipped)."""
    client = meilisearch.Client(settings.MEILISEARCH_URL, settings.MEILISEARCH_KEY)
    index = client.index(MEILISEARCH_INDEX)

    offset = 0
    limit = BATCH_SIZE
    patched = skipped = 0

    while True:
        result = index.search(
            "",
            opt_params={
                "filter": [f"book_id = {book_id}"],
                "limit": limit,
                "offset": offset,
            },
        )
        hits = result.get("hits", [])
        if not hits:
            break

        updates: list[dict] = []
        for hit in hits:
            phys_start = (
                hit.get("physical_page_start")
                or hit.get("physical_page")
                or hit.get("page_start")
            )
            phys_end = (
                hit.get("physical_page_end")
                or hit.get("physical_page")
                or hit.get("page_end")
            )
            new_start = mapping.get(phys_start)
            new_end = mapping.get(phys_end)
            # Keep the existing value when no printed footer number exists
            new_start = new_start if new_start is not None else hit.get("page_start")
            new_end = new_end if new_end is not None else hit.get("page_end")

            if new_start == hit.get("page_start") and new_end == hit.get("page_end"):
                skipped += 1
                continue

            updates.append({"id": hit["id"], "page_start": new_start, "page_end": new_end})
            patched += 1

        if updates and not dry_run:
            task = index.update_documents(updates)
            client.wait_for_task(task.task_uid)

        offset += limit
        if len(hits) < limit:
            break

    return patched, skipped


# ---------------------------------------------------------------------------
# Per-book backfill
# ---------------------------------------------------------------------------

async def backfill_book(book: Book, dry_run: bool) -> None:
    logger.info("Processing book %s (%s)...", book.id, book.title)

    # Load PDF from MinIO
    minio_path = None
    # We need the minio_path from a Qdrant point for this book
    qdrant = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=30)
    try:
        points = await _fetch_qdrant_points(qdrant, str(book.id))
    finally:
        await qdrant.close()

    if not points:
        logger.warning("No Qdrant points found for book %s — skipping.", book.id)
        return

    minio_path = points[0].payload.get("minio_path")
    if not minio_path:
        logger.warning("No minio_path on points for book %s — skipping.", book.id)
        return

    logger.info("  Loading PDF from MinIO: %s", minio_path)
    pdf_bytes = await storage.get_file(settings.MINIO_BUCKET_BOOKS, minio_path)
    logger.info("  Building physical→footer page mapping...")
    mapping = _build_physical_to_footer_map(pdf_bytes)
    extracted = sum(1 for v in mapping.values() if v is not None)
    logger.info(
        "  Footer extraction: %d/%d pages (%d%%)",
        extracted, len(mapping), int(extracted / max(len(mapping), 1) * 100),
    )

    # Patch Qdrant
    qdrant = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=30)
    try:
        q_patched, q_skipped = await _patch_qdrant(qdrant, points, mapping, dry_run)
    finally:
        await qdrant.close()

    # Patch Meilisearch
    m_patched, m_skipped = await asyncio.to_thread(
        _patch_meilisearch, str(book.id), mapping, dry_run
    )

    logger.info(
        "  Book %s done%s: Qdrant patched=%d skipped=%d | "
        "Meilisearch patched=%d skipped=%d",
        book.id,
        " [DRY RUN]" if dry_run else "",
        q_patched, q_skipped,
        m_patched, m_skipped,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    if args.all_books:
        books = await _get_all_books()
        if not books:
            logger.info("No ready books found.")
            return
        logger.info("Found %d books to process.", len(books))
    elif args.book_id:
        book = await _get_book(args.book_id)
        if not book:
            logger.error("Book %s not found in database.", args.book_id)
            sys.exit(1)
        books = [book]
    else:
        logger.error("Specify --book-id <id> or --all-books")
        sys.exit(1)

    for book in books:
        try:
            await backfill_book(book, dry_run=args.dry_run)
        except Exception:
            logger.exception("Failed to backfill book %s", book.id)

    logger.info("Backfill complete%s.", " (DRY RUN — no changes written)" if args.dry_run else "")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--book-id", help="UUID of a single book to fix")
    group.add_argument("--all-books", action="store_true", help="Fix all ready books")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing to Qdrant or Meilisearch",
    )
    args = parser.parse_args()
    asyncio.run(main(args))
