"""BUG 2 proof: run the keyword leg against the live Meilisearch index with
the fixed keyword query (original-language + English terms) for the
non-English (Arabic) book, and show raw_count > 0 with real hits."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from app.features.qa.agent import _build_keyword_queries
from app.features.qa.retriever import keyword_search
from app.features.qa.reranker import _query_terms, normalise_transliteration

BOOK_ID = "daf67c12-873d-4690-b8be-c31efab7ae2a"
TENANT = "default"


async def main() -> None:
    question = "من هو صدر الشريعة؟"
    normalised = normalise_transliteration(question)
    # what the OLD code built (English leg only)
    old_terms = _query_terms("Who is Sadr al-Sharia?") or _query_terms(normalised)
    old_query = " ".join(old_terms)
    # what the NEW code builds: one keyword leg per script, searched separately
    primary, secondary = _build_keyword_queries(normalised, "Who is Sadr al-Sharia?", question)

    print(f"OLD keyword query (english leg only): {old_query!r}")
    old_hits = await keyword_search(old_query, TENANT, top_k=20, book_id=BOOK_ID)
    print(f"OLD leg raw_count={len(old_hits)}")

    print(f"NEW primary keyword leg (original language): {primary!r}")
    print(f"NEW secondary keyword leg (english):         {secondary!r}")
    new_hits = await keyword_search(primary, TENANT, top_k=20, book_id=BOOK_ID, alt_query=secondary)
    print(f"NEW merged keyword raw_count={len(new_hits)}")
    for h in new_hits:
        print(f"  hit page={h.get('page_start')}-{h.get('page_end')} text={h.get('text','')[:90]!r}")


asyncio.run(main())
