"""RAG orchestration — public surface.

Phase 1 split the former 1170-line agent into cohesive modules under
app/features/qa/; this module remains the stable import surface for the
API router, the phase0 evidence harness and the unit tests.
"""
from app.features.qa.citations import (  # noqa: F401
    _CITATION_TAG_RE,
    _answer_language,
    _is_citation_question,
    _iter_tag_indices,
    _resolve_citations,
    _validate_citations,
)
from app.features.qa.claim_matching import (  # noqa: F401
    _claim_sentence,
    _claim_tokens,
    _normalise_token,
)
from app.features.qa.graph import (  # noqa: F401
    generate_node,
    graph,
    no_result_node,
    quality_check_node,
    retrieve_node,
    retry_node,
)
from app.features.qa.llm import _build_providers, _chat_with_retry  # noqa: F401
from app.features.qa.page_resolution import (  # noqa: F401
    _claim_page,
    _dense_page,
    _locate_claim,
)
from app.features.qa.passages import (  # noqa: F401
    _annotate_pages,
    _format_passages,
)
from app.features.qa.retrieval import (  # noqa: F401
    _build_keyword_query,
    _build_keyword_queries,
)
from app.features.qa.state import AgentState  # noqa: F401
