# Phase 0 Evidence: Highlight & Citation Coordinate Accuracy

Artifacts produced by `scripts/phase0/fixtures_gen.py` + `scripts/phase0/dump_pipeline.py`.
Four captures: host/container × before/after the fixes.

## Root causes proven

| # | Defect | Proof artifact |
|---|--------|----------------|
| RC1 | Gemini text-only OCR stores synthetic char-width boxes as real geometry; `ask.py` gated only the top-level bbox (≤3000pt), never `page_bboxes` entries | `*/synthetic_gemini_*/stage_d_source.json`: BEFORE per-page boxes pass `plausible=true` but `inside_page_rect=false` (x1≈1340–1390 on a 595pt page); short-line variant passes containment and renders a garbage band at [0,0,209,17] on an unrelated page (`synthetic_gemini_shortlines/stage_e_render.json`, BEFORE) |
| RC2 | Mixed printed/physical page spaces: markers used `printed or physical` — printed **0** is falsy → two different pages labelled "Page 1" | `edge_offset_frontmatter/citation_probe.json` BEFORE: `markers_emitted=[1,1,2]` for slices of pages [1,2,3]; AFTER: `[1,2,3]`, `marker_space_mixed=false` |
| RC3 | `page_offsets.end_char` was inclusive while JS `slice()`/Python slicing treat end as exclusive → viewer snippets off by one char | `test_chunker_page_bboxes.py::test_page_offsets_tile_the_chunk_text` + `test_printed_citations.py::test_annotate_slices_are_end_exclusive` |
| RC4 | Silent fallbacks: no-highlight render cached permanently under the bbox key; implausible bbox nulled without logging; malformed viewer entries dropped silently | BEFORE `synthetic_gemini_geometry/minio/`: 200-response PNG with no band cached under the bogus bbox key. AFTER log: "produced no highlight; response served uncached" |
| RC5 | RTL line reversal in `_locate_bbox` | Verified empirically: `single_page_ar` / `multi_page_ar` / scanned-UR cases locate correctly (region matches stored box ±1pt); reversal retained unchanged |

## Fixes (F1–F7)

- **F1** `extractor.py`: every word stamped `geom="real"|"synthetic"`.
- **F2** `chunker.py::_compute_page_bboxes`: only real-geometry words produce boxes; synthetic-only pages get none (`bbox=None`).
- **F3** `ask.py::_build_source`: gates EVERY `page_bboxes` entry + top-level bbox, logs drops.
- **F4** `agent.py`: citations unified to PHYSICAL space (markers, Source lines, `_claim_page` fallbacks). *Breaking*: answer page numbers are now physical indices; printed footer numbers remain on payloads but are never displayed.
- **F5** `chunker.py::_compute_page_offsets`: `end_char` exclusive uniformly.
- **F6** `highlight.py`: pure `render_highlight()` returning `{mode: bbox|located|none|full}`; loud logging on every degradation; `none` renders are NOT cached.
- **F7** `KitabViewer.tsx`: dev-mode `console.warn` on malformed `page_bboxes`/`page_offsets`; offsets contract documented as half-open.

## Verification summary

- Real-geometry renders are pixel-region-identical before/after on all 6 text-layer cases + tesseract locate case (host and container) — no regressions.
- AFTER: synthetic chunks carry zero geometry end-to-end; viewer falls back to text locate; unmatched synthetic text draws nothing (correct — the text isn't on that page).
- 146 backend unit tests pass on host (PyMuPDF 1.27) AND in container (pinned PyMuPDF 1.24.3).
- Frontend `tsc --noEmit` clean.

## Known caveats

- `citation_probe.resolved_citation_page` can differ from the expected page when fixture sentences/tokens repeat across pages (templated fixtures share vocabulary); content-based resolution cannot distinguish identical text. Marker-space unification (the actual fix) is proven by `markers_emitted == physical_pages_of_slices`; exact-resolution semantics are covered by `tests/unit/test_printed_citations.py`.
- `SourceItem.page` remains `page_start` for multi-page chunks (viewer badge shows the chunk's start page; per-page navigation uses `page_offsets`). Changing badge semantics is UX work outside Phase 0 scope.

## Artifact map (per capture)

```
<capture>/<case>/
  stage_a_words.json     extractor output (geom stamps visible)
  stage_b_chunk.json     chunk payload (bbox/page_bboxes/page_offsets)
  stage_c_payload.json   RAG passage payload
  stage_d_source.json    _build_source gating result + viewer query params
  citation_probe.json    marker/citation-space probe (multi-page case)
  minio/                 storage-stub cache state after the render call
  clean.png              pristine page raster (dpi=150)
  endpoint.png           what GET /highlight returned
  stage_e_render.json    measured drawn region (pixel diff) + render meta
summary.json             machine-readable rollup
```
