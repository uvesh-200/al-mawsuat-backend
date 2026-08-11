"""BUG 1 + 3 + 4 proof: inspect stored Qdrant points for the re-ingested
Arabic book: per-page bboxes (bug 1), page_offsets (bug 3), and count."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from qdrant_client import AsyncQdrantClient

BOOK_ID = "daf67c12-873d-4690-b8be-c31efab7ae2a"
TENANT = "default"


async def main() -> None:
    client = AsyncQdrantClient(host="localhost", port=6333, timeout=30)
    try:
        res = await client.scroll(
            collection_name="documents",
            limit=1000,
            with_payload=True,
            with_vectors=False,
        )
        points = [p for p in res[0] if p.payload.get("book_id") == BOOK_ID and p.payload.get("tenant_id") == TENANT]
        print(f"Qdrant /points/count for book {BOOK_ID}: {len(points)}")
        bad = 0
        for p in sorted(points, key=lambda p: p.payload.get("page_start", 0)):
            pay = p.payload
            bbox = pay.get("bbox")
            pbs = pay.get("page_bboxes") or []
            offs = pay.get("page_offsets") or []
            pages = f"{pay.get('page_start')}-{pay.get('page_end')}"
            print(f"id={p.id}")
            print(f"  pages={pages} bbox={bbox}")
            for pb in pbs:
                print(f"    page_bbox page={pb.get('page')} bbox={pb.get('bbox')}")
            print(f"  page_offsets={json.dumps(offs, ensure_ascii=False)}")
            # validity checks: every stored box must be inside a real page
            for pb in pbs:
                b = pb.get("bbox") or []
                if len(b) != 4 or b[2] > 3000 or b[3] > 3000 or b[2] <= b[0] or b[3] <= b[1]:
                    bad += 1
            if bbox:
                if len(bbox) != 4 or bbox[2] > 3000 or bbox[3] > 3000:
                    bad += 1
            print()
        print("stored boxes outside real page dimensions:", bad)
    finally:
        await client.close()


asyncio.run(main())
