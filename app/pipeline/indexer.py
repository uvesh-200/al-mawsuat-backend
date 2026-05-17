import uuid
import asyncio

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
import meilisearch
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.tables import Book


QDRANT_COLLECTION = "documents"
MEILISEARCH_INDEX = "documents"
VECTOR_SIZE = 1024

MEILISEARCH_FILTERABLE = ["tenant_id", "book_id", "language", "book_type"]
MEILISEARCH_SEARCHABLE = ["text", "chapter", "book_name", "author"]


def _make_chunk_id(chunk: dict) -> str:
    raw = f"{chunk['text']}|{chunk['page_start']}|{chunk['page_end']}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, raw))


async def _ensure_qdrant_collection(client: AsyncQdrantClient) -> None:
    collections = await client.get_collections()
    names = {c.name for c in collections.collections}
    if QDRANT_COLLECTION not in names:
        await client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )


def _ensure_meilisearch_index(client: meilisearch.Client) -> None:
    resp = client.get_indexes()
    indexes = resp.get("results", [])
    exists = any(i.uid == MEILISEARCH_INDEX for i in indexes)

    if not exists:
        task = client.create_index(MEILISEARCH_INDEX, {"primaryKey": "id"})
        client.wait_for_task(task.task_uid)
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
        client.wait_for_task(task.task_uid)

    if not have_searchable.issuperset(want_searchable):
        task = index.update_searchable_attributes(list(have_searchable | want_searchable))
        client.wait_for_task(task.task_uid)


async def index_to_qdrant(chunks: list[dict], vectors: list[list[float]]) -> None:
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
                    "text": chunk.get("text"),
                    "bbox": chunk.get("bbox"),
                    "minio_path": chunk.get("minio_path"),
                },
            )
            for chunk, vector in zip(chunks, vectors)
        ]

        await client.upsert(collection_name=QDRANT_COLLECTION, points=points)
    finally:
        await client.close()


async def index_to_meilisearch(chunks: list[dict]) -> None:
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
                "bbox": chunk.get("bbox"),
                "minio_path": chunk.get("minio_path"),
            }
            documents.append(doc)

        task = client.index(MEILISEARCH_INDEX).add_documents(documents)
        client.wait_for_task(task.task_uid)

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
