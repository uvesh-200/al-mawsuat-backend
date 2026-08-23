# REFACTOR_REPORT — al-mawsuat-backend

Living report for the enterprise refactor on branch `refactor/enterprise-cleanup`.
Per-phase evidence lives in `refactor-evidence/` and is committed.

---

## Phase 0 — Highlight & Citation Coordinate Accuracy (DONE)

**Goal:** prove and fix every failure mode where highlights land on the wrong
place (or nowhere) and citations point at pages the viewer cannot reconcile.

### Root causes → fixes

| ID | Root cause | Fix | Where |
|----|-----------|-----|-------|
| RC1 | Gemini text-only OCR stored synthetic char-width boxes as real word geometry; API gated only the top-level bbox, never `page_bboxes` entries; viewer prefers per-page boxes | Words stamped `geom="real"\|"synthetic"` at extraction; per-page boxes built from real-geometry words only; every entry re-gated in `_build_source` with logged drops | `app/pipeline/extractor.py`, `app/pipeline/chunker.py`, `app/api/ask.py` |
| RC2 | Mixed printed/physical page spaces: ⟨Page N⟩ markers used `printed or physical`; printed **0** is falsy → two distinct pages both labelled "Page 1"; UI badge, citation text and highlight page could disagree | All citation resolution unified to PHYSICAL page space (markers, Source lines, `_claim_page` fallbacks) | `app/rag/agent.py` |
| RC3 | `page_offsets.end_char` inclusive while JS `slice()` is end-exclusive → viewer snippets off-by-one | `end_char` exclusive uniformly; contract documented in chunker + viewer | `app/pipeline/chunker.py`, frontend `KitabViewer.tsx` |
| RC4 | Silent degradations: no-highlight render cached permanently under the request's bbox key; implausible bbox nulled without logging; viewer dropped malformed entries silently | Pure `render_highlight()` returning `{mode, rect, reason}`; loud logging on every degradation path; `none`-mode renders never cached; dev-mode `console.warn` in viewer | `app/api/highlight.py`, frontend `KitabViewer.tsx` |
| RC5 | RTL line reversal suspected of mislocating Arabic snippets | Verified empirically (AR/UR cases locate within ±1pt of stored box); reversal retained unchanged | `app/api/highlight.py::_locate_bbox` |

### Evidence

- Harness: `scripts/phase0/fixtures_gen.py` (synthetic AR/UR/EN books incl.
  scanned-image and front-matter-offset cases) +
  `scripts/phase0/dump_pipeline.py` (stage A extract → B chunk → C payload →
  D source-gating + viewer query mirror → E real `/highlight` call with
  pixel-diff region measurement and storage-stub cache inspection).
- Captures: `refactor-evidence/phase0/{before,after}{,-container}/` — 9 cases
  each, incl. rendered PNGs and machine-readable `summary.json`.
- Key before/after facts:
  - BEFORE synthetic-Gemini boxes passed the ≤3000pt plausibility gate yet sat
    outside the page rect → HTTP 200 rendering **nothing**, cached forever;
    short-line variant drew a garbage band at `[0,0,209,17]` on an unrelated
    page. AFTER: such chunks carry zero geometry end-to-end; viewer falls back
    to snippet locate; unmatched synthetic text draws nothing (correct).
  - Citation probe markers `[1,1,2]` (mixed space, two pages labelled "Page 1")
    → `[1,2,3]` (`marker_space_mixed=false`) on host and container.
  - All real-geometry renders pixel-region-identical before/after — no
    regressions, including the tesseract LCS locate path inside the container.

### Verification

- 146 backend unit tests green on host (PyMuPDF 1.27) **and** inside the api
  container (pinned PyMuPDF 1.24.3): updated
  `test_chunker_page_bboxes.py`, rewritten `test_printed_citations.py`
  (physical-space contract), new `test_highlight_render.py`.
- Frontend `tsc --noEmit` clean.

### Breaking changes

- Answer-text page numbers are now PHYSICAL page indices. Books with front
  matter previously displayed printed footer numbers inconsistently (and
  incorrectly when printed=0); they now always match the rendered PDF page.
  `printed_*` fields remain on payloads but are never used for display.

### Known caveats

- `citation_probe.resolved_citation_page` can disagree with the expected page
  when fixture sentences repeat across pages (templated fixtures share
  vocabulary; content-based resolution cannot distinguish identical text).
  Marker-space unification — the actual defect — is proven by
  `markers_emitted == physical_pages_of_slices`; exact-resolution semantics
  are covered by unit tests.
- `SourceItem.page` remains `page_start` for multi-page chunks (badge shows
  the chunk's start page; per-page navigation uses `page_offsets`). Badge
  semantics change is UX work outside Phase 0 scope.

### Commits

- `8cd3d18` fix(phase0): unify highlight/citation geometry to real coordinates and one page space
- `1d8dc4c` chore(phase0): drop nested duplicate evidence capture

Frontend counterpart: `0a1d0ce` fix(viewer): warn on malformed source geometry entries and document half-open offsets.

---

## Upcoming phases

- **Phase 1**: backend feature-based restructure (routers/services/repositories/schemas/workers, ≤300-line files)
- **Phase 2**: frontend feature folders + typed API client layer
- **Phase 3+**: multi-tenancy hardening, admin/analytics endpoints, perf work,
  documentation — per spec sections 4–9 (details filled in as each lands)

## Phase 1 - Structural decomposition (COMPLETE, host-verified)

Commits: e5da558 (core/), 7960091 (features/ + workers/), 1c1302b (ingestion split + rag removal).

- app/core/: config.py, db.py, storage.py (ex minio_client), security.py (ex core/auth)
- app/features/{auth,users,catalog,jobs,system,highlights,books,qa,ingestion}/ routers moved from api/
- workers/ -> app/workers/; alembic env + compose Dockerfile CMD updated
- qa monolith (rag/agent.py 1170L) decomposed into features/qa/{prompts,llm,retrieval,claim_matching,citations,page_resolution,passages,state,graph}; agent.py kept as re-export facade; monolith deleted
- pipeline -> features/ingestion/: extractor{,/page_numbers,/words}, chunker{,/layout,/paragraphs,/geometry}, shared sizing.py
- qa/router.py (400L) split into router + ask_service.py
- Constraint met: every module <=300 lines; facades re-export legacy names so tests import unchanged targets.
- Verification: 146/146 unit tests green after each step; full-app import smoke blocked only by pre-existing missing sentry_sdk in host venv.
- Caveats: Docker image not yet rebuilt against the new layout (docker compose build required before deploy); git CRLF warnings are cosmetic.

### Container verification (d725ebf)

Rebuilt fastapi/worker/beat images against the new layout and booted the full stack:
- alembic upgrade head runs via entrypoint (validates app.core.db wiring)
- 146/146 unit tests pass inside the image (run as python -m pytest)
- gunicorn serves; GET /health -> {status ok, postgres/qdrant/redis ok}
- celery worker ready; beat starts cleanly

Three latent split bugs found only by container verification:
1. workers.celery_app still imported dead repo-root workers.processor path
2. qa/graph.py never compiled its StateGraph (rag_graph missing)
3. agent facade missing LLM_ERROR_FALLBACK / NO_RESULT_REFUSALS / rag_graph re-exports

### E2E verification (283f8e3)

Ran tests/e2e against the live refactored stack. Found and fixed four more
split-induced defects that only manifest at runtime:
- retrieval.py missing translate_to_english import (every /ask 500'd)
- graph.py missing _iter_tag_indices + CONSISTENCY_CHECK_PROMPT imports
- ask_service.py: page used before assignment on bbox-gate warning paths
  (latent since Phase 0; fired on real synthetic geometry)
Plus words.py/extractor.py/paragraphs.py import gaps found by the new
scripts/static_import_check.py (AST undefined-name sweep, now clean).

Result: 27 passed / 3 skipped (fixture book 394ed100 not present in this
environment's volumes; highlight + mahbubi tests now adapt or skip with a
clear reason). Unit suite stays at 146 green.
