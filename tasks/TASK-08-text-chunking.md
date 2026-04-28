# TASK-08 — Text Chunking

**Feature:** Split extracted text into searchable segments  
**Repo:** al-mawsuat-backend  
**Week:** 3  
**Depends on:** TASK-07

---

## Description

Write `app/pipeline/chunker.py`. This module takes the raw word list from the extractor and produces a list of text chunks ready for embedding. Each chunk is a meaningful segment of the book — not just an arbitrary slice of text.

The chunker receives the output of `extractor.extract()` (list of page dicts with words and bboxes) and must produce chunks with these fields:

```python
{
  "text": "full text of the chunk as a single string",
  "page_start": 12,
  "page_end": 12,
  "bbox": [x0, y0, x1, y1],   # bounding box covering all words in this chunk
  "chapter": "Kitab al-Taharah",  # detected chapter heading, or None
  "tenant_id": "al-mawsuat-deobandiyyah",
  "token_count": 347
}
```

Chunking rules (apply in this order of priority):

1. **Hadith books** — detect individual hadith by looking for hadith number patterns (`حديث رقم`, numbered Arabic patterns). Each hadith is its own chunk regardless of token count.

2. **Quran/Tafsir books** — detect ayah markers (`﴿` or `﴾` brackets, or verse numbers). Each ayah or short group of ayahs is a chunk.

3. **Fiqh/Fatwa books** — detect masail headings (lines that are significantly shorter than body text and end with a colon or are in bold). Start a new chunk at each heading.

4. **Fallback** — if no structure is detected, chunk at sentence boundaries (Arabic full stop `۔` or `.`) targeting 500–800 tokens per chunk. Never split mid-sentence.

The `bbox` for a chunk is the bounding box that covers all words in the chunk: `[min(all x0), min(all y0), max(all x1), max(all y1)]`.

Chapter detection: if a line is very short (under 50 characters), all caps, or starts with "باب" (chapter) or "كتاب" (book), treat it as a chapter heading and attach it as the `chapter` field to all subsequent chunks until the next heading.

Token count: use a simple word-count approximation — `len(text.split())` is sufficient. Do not import a full tokeniser for this.

Main function signature: `def chunk(pages: list[dict], tenant_id: str) -> list[dict]`

---

## Acceptance criteria

- [ ] Output chunks all have `text`, `page_start`, `page_end`, `bbox`, `chapter`, `tenant_id`, `token_count`
- [ ] No chunk has `token_count` above 900 (hard ceiling)
- [ ] No chunk splits mid-sentence (last character of `text` is a sentence-ending punctuation or the text is a complete hadith/ayah)
- [ ] `bbox` covers all words in the chunk — not just the first word
- [ ] `tenant_id` on every chunk equals `settings.DEFAULT_TENANT_ID`
- [ ] A Hadith collection produces chunks that each contain exactly one hadith
- [ ] A Fiqh book produces chunks that start at each masail heading
- [ ] `chapter` field is correctly inherited from the nearest preceding heading
- [ ] An empty pages list returns an empty list without error
