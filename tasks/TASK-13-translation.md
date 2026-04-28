# TASK-13 — Translation and Language Detection

**Feature:** Detect query language and translate for retrieval  
**Repo:** al-mawsuat-backend  
**Week:** 4  
**Depends on:** TASK-01

---

## Description

Write the translation server and the translation wrapper used by the RAG pipeline. The purpose is to translate user queries into Arabic before embedding, so that Urdu and English questions can effectively retrieve Arabic book content.

**File: `inference/translation_server.py`**

A standalone FastAPI server that loads the NLLB-200 model once on startup. Model: `facebook/nllb-200-distilled-600M` using HuggingFace `transformers` pipeline with task `translation`.

Two endpoints:

`POST /detect` — body: `{ "text": "..." }`. Returns `{ "language": "ur" }`. Use the `langdetect` library for detection.

`POST /translate` — body: `{ "text": "...", "source_lang": "ur", "target_lang": "ar" }`. Returns `{ "translated": "..." }`. Use NLLB-200 language codes: `urd_Arab` for Urdu, `arb_Arab` for Arabic, `eng_Latn` for English.

Add a `GET /health` endpoint.

Add this as a Docker service `translation_server` on internal port 8002.

**File: `app/core/translation.py`**

A wrapper used by the RAG pipeline:

`async def detect_language(text: str) -> str` — calls `POST /detect` on the translation server. Returns the 2-letter language code (`ar`, `ur`, `en`).

`async def translate_for_retrieval(text: str, source_lang: str) -> str` — if `source_lang` is `"ar"`, return the text unchanged. Otherwise call `POST /translate` to translate to Arabic. This is used on every user query before embedding, so Arabic book content is matched correctly regardless of what language the question was asked in.

---

## Acceptance criteria

- [ ] `POST /detect` with Urdu text returns `{ "language": "ur" }`
- [ ] `POST /detect` with Arabic text returns `{ "language": "ar" }`
- [ ] `POST /detect` with English text returns `{ "language": "en" }`
- [ ] `POST /translate` with a simple Urdu sentence returns a readable Arabic translation
- [ ] `translate_for_retrieval` with Arabic input returns the same text unchanged (no API call)
- [ ] `translate_for_retrieval` with English input returns an Arabic translation
- [ ] Server is reachable at `http://translation_server:8002` from other Docker services
- [ ] Server starts without error and logs that the NLLB-200 model is loaded
