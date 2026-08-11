import re
import unicodedata
import uuid
import asyncio
import logging

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
import meilisearch
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.tables import Book

logger = logging.getLogger(__name__)


QDRANT_COLLECTION = "documents"
MEILISEARCH_INDEX = "documents"
VECTOR_SIZE = 3072

MEILISEARCH_FILTERABLE = ["tenant_id", "book_id", "language", "book_type"]
MEILISEARCH_SEARCHABLE = ["text", "chapter", "book_name", "author"]

# Arabic diacritics (tashkeel), tatweel, hamza-above/below marks (which NFD
# decomposition surfaces from أ/إ/آ), and Latin combining marks — stripped
# before hashing so OCR diacritic variance doesn't change a chunk's ID.
_DIACRITICS_RE = re.compile(r"[\u064B-\u0655\u0670\u0640\u0300-\u036F]")
_ALEF_FORMS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})
# Latin transliteration artifacts (ʿ ʾ ʼ ʻ for hamza) that OCR and
# transliteration pipelines emit inconsistently.
_MODIFIER_MARKS = str.maketrans({"ʿ": "", "ʾ": "", "ʼ": "", "ʻ": ""})


def _normalise_for_id(text: str) -> str:
    """Canonical form of chunk text for ID generation.

    OCR output is not perfectly deterministic across runs (diacritic
    variance: tashkeel marks, tatweel, hamza/alef spelling), so hashing the
    raw OCR text would mint a different ID for "the same" logical chunk on
    every reprocess and leave near-duplicate vectors behind. Hashing this
    normalised form makes reprocessing the same page idempotent.
    """
    t = unicodedata.normalize("NFD", text)
    t = _DIACRITICS_RE.sub("", t)
    t = t.translate(_MODIFIER_MARKS)
    t = t.translate(_ALEF_FORMS)
    t = unicodedata.normalize("NFC", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def _make_chunk_id(chunk: dict) -> str:
    raw = f"{_normalise_for_id(chunk['text'])}|{chunk['page_start']}|{chunk['page_end']}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, raw))


async def _ensure_qdrant_collection(client: AsyncQdrantClient) -> None:
    collections = await client.get_collections()
    names = {c.name for c in collections.collections}
    if QDRANT_COLLECTION not in names:
        await client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )


def _wait_for_task(client: meilisearch.Client, task_uid: int) -> None:
    """Wait for a Meilisearch task with a generous timeout.

    The meilisearch-python default wait is 5000ms, which raises a
    MeilisearchTimeoutError whenever the index is busy processing a queue of
    tasks (e.g. several concurrent uploads). The task keeps running
    server-side after the wait expires, so a timeout here is a warning, not a
    failure: re-check the task status once and only raise if it actually
    failed. Chunk ids are stable hashes, so a task that completes in the
    background is still idempotent on any re-import.
    """
    timeout_ms = settings.MEILISEARCH_TASK_TIMEOUT_MS
    interval_ms = settings.MEILISEARCH_TASK_INTERVAL_MS
    try:
        client.wait_for_task(task_uid, timeout_in_ms=timeout_ms, interval_in_ms=interval_ms)
    except meilisearch.errors.MeilisearchTimeoutError:
        status = client.get_task(task_uid).status
        if status == "failed":
            raise
        logger.warning(
            "meilisearch task %d still %s after %dms; it will complete server-side",
            task_uid, status, timeout_ms,
        )


def _ensure_meilisearch_index(client: meilisearch.Client) -> None:
    resp = client.get_indexes()
    indexes = resp.get("results", [])
    exists = any(i.uid == MEILISEARCH_INDEX for i in indexes)

    if not exists:
        task = client.create_index(MEILISEARCH_INDEX, {"primaryKey": "id"})
        _wait_for_task(client, task.task_uid)
        index = client.index(MEILISEARCH_INDEX)
    else:
        index = client.index(MEILISEARCH_INDEX)

    current = index.get_settings()
    want_filterable = set(MEILISEARCH_FILTERABLE)
    want_searchable = set(MEILISEARCH_SEARCHABLE)
    have_filterable = set(current.get("filterableAttributes") or [])
    have_searchable = set(current.get("searchableAttributes") or [])

    if not have_filterable.issuperset(want_filterable):
        task = index.update_filterable_attributes(list(have_filterable | want_filterable))
        _wait_for_task(client, task.task_uid)

    if not have_searchable.issuperset(want_searchable):
        task = index.update_searchable_attributes(list(have_searchable | want_searchable))
        _wait_for_task(client, task.task_uid)


async def index_to_qdrant(chunks: list[dict], vectors: list[list[float]]) -> None:
    logger.info(
        "[TRACE] qdrant index start chunks=%d vectors=%d vector_size=%d collection=%s",
        len(chunks), len(vectors), VECTOR_SIZE, QDRANT_COLLECTION,
    )
    client = AsyncQdrantClient(
        host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=10
    )
    try:
        await _ensure_qdrant_collection(client)

        points = [
            PointStruct(
                id=_make_chunk_id(chunk),
                vector=vector,
                payload={
                    "tenant_id": chunk.get("tenant_id"),
                    "book_id": chunk.get("book_id"),
                    "book_name": chunk.get("book_name"),
                    "author": chunk.get("author"),
                    "language": chunk.get("language"),
                    "book_type": chunk.get("book_type"),
                    "chapter": chunk.get("chapter"),
                    "page_start": chunk.get("page_start"),
                    "page_end": chunk.get("page_end"),
                    "physical_page_start": chunk.get("physical_page_start"),
                    "physical_page_end": chunk.get("physical_page_end"),
                    "printed_page_start": chunk.get("printed_page_start"),
                    "printed_page_end": chunk.get("printed_page_end"),
                    "text": chunk.get("text"),
                    "bbox": chunk.get("bbox"),
                    "page_bboxes": chunk.get("page_bboxes"),
                    "page_offsets": chunk.get("page_offsets"),
                    "minio_path": chunk.get("minio_path"),
                },
            )
            for chunk, vector in zip(chunks, vectors)
        ]

        await client.upsert(collection_name=QDRANT_COLLECTION, points=points)
        logger.info(
            "[TRACE] qdrant upsert done points=%d collection=%s",
            len(points), QDRANT_COLLECTION,
        )
    finally:
        await client.close()


async def index_to_meilisearch(chunks: list[dict]) -> None:
    logger.info(
        "[TRACE] meilisearch index start docs=%d index=%s",
        len(chunks), MEILISEARCH_INDEX,
    )
    client = meilisearch.Client(
        settings.MEILISEARCH_URL, settings.MEILISEARCH_KEY
    )

    def _sync() -> None:
        _ensure_meilisearch_index(client)

        documents = []
        for chunk in chunks:
            doc = {
                "id": _make_chunk_id(chunk),
                "text": chunk.get("text"),
                "tenant_id": chunk.get("tenant_id"),
                "book_id": chunk.get("book_id"),
                "book_name": chunk.get("book_name"),
                "author": chunk.get("author"),
                "language": chunk.get("language"),
                "book_type": chunk.get("book_type"),
                "chapter": chunk.get("chapter"),
                "page_start": chunk.get("page_start"),
                "page_end": chunk.get("page_end"),
                "physical_page_start": chunk.get("physical_page_start"),
                "physical_page_end": chunk.get("physical_page_end"),
                "printed_page_start": chunk.get("printed_page_start"),
                "printed_page_end": chunk.get("printed_page_end"),
                "bbox": chunk.get("bbox"),
                "page_bboxes": chunk.get("page_bboxes"),
                "page_offsets": chunk.get("page_offsets"),
                "minio_path": chunk.get("minio_path"),
            }
            documents.append(doc)

        task = client.index(MEILISEARCH_INDEX).add_documents(documents)
        _wait_for_task(client, task.task_uid)
        logger.info(
            "[TRACE] meilisearch add_documents done docs=%d task_uid=%s",
            len(documents), task.task_uid,
        )

    await asyncio.to_thread(_sync)


async def update_book_status(
    book_id: str, chunk_count: int, db: AsyncSession
) -> None:
    stmt = (
        update(Book)
        .where(Book.id == uuid.UUID(book_id))
        .values(total_chunks=chunk_count, status="ready")
    )
    await db.execute(stmt)
    await db.commit()
