# TASK-14 — LangGraph Agent and vLLM Answer Generation

**Feature:** AI agent that retrieves and generates grounded answers  
**Repo:** al-mawsuat-backend  
**Week:** 5  
**Depends on:** TASK-12, TASK-13

---

## Description

Write the LangGraph agent that orchestrates the full RAG flow and connects it to the vLLM inference server for answer generation. This is the core intelligence of the system.

**vLLM setup**

Add a `vllm` Docker service to `docker-compose.yml`:
- Image: `vllm/vllm-openai:latest`
- Command: `--model mistralai/Mistral-7B-Instruct-v0.3 --device cpu`
- Mount the HuggingFace cache directory so the model is not re-downloaded
- Internal port 8000, mapped to host port 8003
- Set `--max-model-len 4096` to limit memory use on CPU

If RAM is under 16GB during development, use Groq API instead: set `VLLM_BASE_URL` to `https://api.groq.com/openai/v1` and `VLLM_MODEL` to `llama-3.1-8b-instant` in `.env`. The code is identical — vLLM uses the OpenAI-compatible API format.

**File: `app/agent/nodes.py`**

Define the state type `AgentState` as a TypedDict with fields: `question`, `tenant_id`, `passages`, `best_score`, `retry_count`, `answer`, `sources`, `no_result`, `streaming`.

Define these node functions:

`async def retrieve_node(state) -> state` — detects language, translates query to Arabic, embeds query, runs vector_search and keyword_search in parallel using `asyncio.gather`, calls reranker, sets `state.passages` and `state.best_score`.

`def quality_check_node(state) -> str` — returns `"generate"` if `best_score >= 0.35`, `"retry"` if `retry_count < 2`, else `"no_result"`.

`async def retry_node(state) -> state` — asks vLLM to rephrase the question differently using a simple prompt `"Rephrase this question in Arabic: {question}"`. Increments `retry_count`. Sets `state.question` to rephrased version.

`async def generate_node(state) -> state` — builds the grounding prompt (below), calls vLLM via the OpenAI-compatible client. Sets `state.answer` and `state.sources`.

`def no_result_node(state) -> state` — sets `state.answer = "No relevant information found in the provided sources."` and `state.no_result = True`.

The grounding prompt template (hardcoded in this file):
```
SYSTEM:
You are an Islamic knowledge assistant specialising in the Deobandi tradition.
Answer ONLY using the passages provided. Do not use your own knowledge.
If the passages do not answer the question, say exactly:
"No relevant information found in the provided sources."
Always show original Arabic or Urdu text before any translation.
Cite every claim: [Book Name, Page X, Chapter Y]

PASSAGES:
{passages}

QUESTION: {question}
```

**File: `app/agent/rag_agent.py`**

Build and compile the LangGraph graph:
- Entry point: `retrieve`
- `retrieve` → conditional edge using `quality_check_node` → `generate`, `retry`, or `no_result`
- `retry` → conditional edge using `quality_check_node` → `generate` or `no_result`
- `generate` → END
- `no_result` → END

Export a compiled `rag_graph` singleton.

---

## Acceptance criteria

- [ ] `rag_graph.ainvoke({"question": "ما حكم الربا؟", "tenant_id": "al-mawsuat-deobandiyyah", ...})` returns a state with a non-empty `answer`
- [ ] The answer contains at least one citation in format `[Book Name, Page X]`
- [ ] The answer does not contain information not present in the uploaded books (verify manually)
- [ ] Asking "What is the speed of light?" (off-topic) returns `no_result: True`
- [ ] A question that retrieves poor results on first try is retried with a rephrased version
- [ ] Retry never happens more than 2 times (max `retry_count` is 2)
- [ ] vLLM server responds at `http://localhost:8003/v1/models` with the model name
