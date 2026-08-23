"""BUG 3 proof: run the real RAG graph on a non-English question whose answer
is grounded in a multi-page chunk; show the resolved citation points at the
actual page the cited sentence falls on (per page_offsets), not page_start."""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

from app.features.qa.agent import rag_graph, _format_passages

TENANT = "default"
BOOK_ID = "daf67c12-873d-4690-b8be-c31efab7ae2a"
QUESTION = sys.argv[1] if len(sys.argv) > 1 else "من هو مصطفى بن عبد الله القسطنطيني الرومي الحنفي؟"


async def main() -> None:
    state = {
        "question": QUESTION,
        "tenant_id": TENANT,
        "book_id": BOOK_ID,
        "language": None,
        "passages": [],
        "best_score": 0.0,
        "top_vector_score": 0.0,
        "retry_count": 0,
        "answer": "",
        "sources": [],
        "no_result": False,
        "streaming": False,
        "embed_failed": False,
    }
    result = await rag_graph.ainvoke(state)
    print("\n=== passages shown to the LLM ===")
    print(_format_passages(result["passages"]))
    print("\n=== final answer ===")
    print(result["answer"])
    print("\n=== sources ===")
    for s in result["sources"]:
        print(
            f"page_start={s.get('page_start')} page_offsets={s.get('page_offsets')} "
            f"score={s.get('score')}"
        )


asyncio.run(main())
