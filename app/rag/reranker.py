from sentence_transformers import CrossEncoder

_cross_encoder = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")


def rerank(question: str, results: list[dict], top_k: int = 5) -> list[dict]:
    if not results:
        return []

    vector_results = [r for r in results if r.get("score", 0) != 0.5]
    keyword_results = [r for r in results if r.get("score", 0) == 0.5]

    rrf_scores: dict[str, float] = {}
    for i, r in enumerate(vector_results):
        key = r.get("text", "")
        rrf_scores[key] = rrf_scores.get(key, 0) + 1.0 / (i + 60)
    for i, r in enumerate(keyword_results):
        key = r.get("text", "")
        rrf_scores[key] = rrf_scores.get(key, 0) + 1.0 / (i + 60)

    seen: set[str] = set()
    deduped = []
    for r in results:
        key = r.get("text", "")
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    deduped.sort(key=lambda r: rrf_scores.get(r.get("text", ""), 0), reverse=True)

    fused_top = deduped[:20]
    if not fused_top:
        return []

    pairs = [(question, r["text"]) for r in fused_top]
    scores = _cross_encoder.predict(pairs, apply_softmax=True)

    for r, s in zip(fused_top, scores):
        r["score"] = float(s)

    fused_top.sort(key=lambda r: r["score"], reverse=True)
    return fused_top[:top_k]
