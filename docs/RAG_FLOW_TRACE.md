# RAG Flow — Complete Step-by-Step Trace

Book: **احیاء العلوم عربی جلد** (7 pages, uploaded via admin)
Question traced: **"ما اسم صاحب كتاب الوقاية؟"** (What is the name of the author of al-Wiqaya?)

Every step below comes from real logs captured during an actual run on 2026-08-07, plus live data pulled from Qdrant/Meilisearch/Postgres. Log lines tagged `[TRACE]` were added to the codebase for this purpose.

---

## PART 1 — INGESTION PIPELINE (upload → searchable)

Files: `app/api/books.py` → `workers/celery_app.py` → `workers/processor.py` → `app/pipeline/*`

### Step 0 — Upload

`POST /admin/books/upload` (multipart form: file + title + author + language):

1. Validates the file is a real PDF (`%PDF` magic bytes).
2. Uploads the PDF to **MinIO** object storage: `books/default/<book_id>/original.pdf` (113,710 bytes for this book).
3. Inserts a `books` row + a `processing_jobs` row (status `queued`) in **Postgres**.
4. Enqueues a **Celery** task `process_book` on **Redis** (broker).

```log
[TRACE] phase=job_start book_id=daf67c12-873d-4690-b8be-c31efab7ae2a
tenant=default minio_path=books/default/daf67c12-873d-4690-b8be-c31efab7ae2a/original.pdf resume_phase=queued
[TRACE] phase=extract book_id=daf67c12-873d-4690-b8be-c31efab7ae2a pdf_bytes=113710
```

The worker writes progress checkpoints to the DB and heartbeats to Redis so a crashed job can be resumed or reaped.

### Step 1 — OCR / Text Extraction

File: `app/pipeline/extractor.py` · Engine: `OCR_ENGINE=gemini` (`.env`)

For each PDF page the worker:
1. Renders the page to PNG at `OCR_DPI=200` (`fitz.get_pixmap`).
2. Sends it to **Gemini vision API** (`app/core/ocr.py`, retries with exponential backoff on 429/503).
3. If Gemini returns empty (or fails after 10 retries), falls back to local **Tesseract** (`ara+eng`, TSV word mode).
4. Extracts a printed footer page number if present (`_extract_footer_page_number`, Arabic-Indic digits).
5. Keeps canonical `page_num = physical page index` (matches the viewer).

Real per-page log (7 pages, 2,024 words, avg 289 words/page):

```log
[TRACE] page physical=1 printed=None footer_extracted=False words=283
preview='وأنكر بعض العلماء هذه الألقاب : منهم: القرطبي في «شرح أسماء الله الحسنى»...'
[TRACE] page physical=2 printed=None footer_extracted=False words=317
preview='المبحث الثاني اسم صاحب (ائوقاية») اختلف العلماءٌ اختلافاً كبيراً...'
[TRACE] page physical=3 printed=None footer_extracted=False words=302
preview='الأول: أن يكون تاج الشريعة هو برهان الشريعة، فيكون اسمه محمودا...'
[TRACE] page physical=4 printed=None footer_extracted=False words=220
preview='والثاني: أن يكون تاج الشريعة هو الجد الصحيح لصدر الشريعة...'
[TRACE] page physical=5 printed=None footer_extracted=False words=339
preview='المبحث الثالث نسب صاحب «الوقاية» يتصل نسب صاحب «الوقاية»...'
[TRACE] page physical=6 printed=None footer_extracted=False words=275
preview='ما وقع من العلماء من الخلط 2 نسب صدر الشريعة إذ تقرّر ما سبق...'
[TRACE] page physical=7 printed=None footer_extracted=False words=288
preview='ومنهم: ابن الحنائي: إذ قال: جمال الدين المحبوبي عبد الله بن إبراهيم...'

[TRACE] phase=extract_done book_id=daf67c12-873d-4690-b8be-c31efab7ae2a
pages=7 words=2024 avg_words_per_page=289.1 footer_extracted=0/7
```

Notes from the actual runs:
- `OCR_ENGINE=gemini`, so every page's words come from `_text_to_words(Gemini OCR text)` — the pipeline never uses the fitz text layer for content in this mode (only for footer page-number extraction). The footer-region check found no printed page numbers (`footer_extracted=0/7`), so pages use physical indices 1–7.
- On an earlier run, Gemini hit `503` on pages 2, 5, 6 — the pipeline survived and used the Tesseract fallback:
  ```log
  WARNING OCR failed for physical page 2 after retries; keeping it empty
  WARNING Gemini OCR empty for physical page 2; used tesseract fallback (317 words)
  ```
- Each page's words carry synthetic/real bounding boxes (`bbox`) — later used by the highlight endpoint.

### Step 2 — Page-number validation

File: `app/pipeline/page_number_validator.py`

```log
Page-number validation passed: N/M pages had footer extraction, modal_offset=...
```
For this book it passed as a warning-tolerant check (0/7 footers → falls back to physical indices; processing continues).

### Step 3 — Chunking

File: `app/pipeline/chunker.py`

The chunker: flatten words → group lines → detect chapters → detect **strategy** → split into paragraphs → group into chunks (~420 target, 15% overlap, 900 hard ceiling) → merge short cross-page chunks.

Real run:

```log
[TRACE] chunk input pages=7 words=2024 lines=107 chapters=0 strategy=fallback
[TRACE] chunk done strategy=fallback chunks=6
[TRACE] chunk idx=0 pages=1..1 tokens=165 chapter=None preview='وأنكر بعض العلماء هذه الألقاب...'
[TRACE] chunk idx=1 pages=1..2 tokens=435 chapter=None preview='(ينظر: «الفوائد البهية» (ص...'
[TRACE] chunk idx=2 pages=3..4 tokens=403 chapter=None preview='الأول: أن يكون تاج الشريعة هو برهان الشريعة...'
[TRACE] chunk idx=3 pages=4..5 tokens=458 chapter=None preview='(وهو مصطفى بن عبد الله القسطنطيني الرومي الحنفي...'
[TRACE] chunk idx=4 pages=6..7 tokens=417 chapter=None preview='ما وقع من العلماء من الخلط 2 نسب صدر الشريعة...'
[TRACE] chunk idx=5 pages=7..7 tokens=146 chapter=None preview='المبحث الخامس أسرته العلمية وطلبه للعلم...'
```

Why these decisions:
- **strategy=fallback**: the book has no hadith patterns (`حديث رقم/باب رقم`), no ayah markers, and fewer than 3 colon-heading lines → generic paragraph-aware chunking (`_group_paragraphs` + `_build_chunks_from_paragraphs`).
- **chapters=0**: no line matched a chapter heading pattern (`باب/كتاب/الفصل` or all-caps Latin) — typical for an article/extract, so `chapter=None` on all chunks.
- **chunk 0 = 165 tokens** and **chunk 5 = 146 tokens**: they are tails kept intact rather than split.
- Chunks 1–4 hover near `TARGET_MAX=420` with the 15% overlap creating page-window chunks (`pages=1..2`, `3..4`, `4..5`, `6..7`) — overlapping chunk text is later de-duplicated in the reranker.

Each chunk dict carries: `text`, `page_start/page_end`, `physical_page_start/end`, `bbox`, `chapter`, `tenant_id`, `token_count` — plus `book_id`, `book_name`, `author`, `language`, `book_type`, `minio_path` added by the worker.

```log
[TRACE] phase=chunk_done book_id=daf67c12-873d-4690-b8be-c31efab7ae2a
chunks=6 tokens_total=2024 avg_tokens=337.3 max_tokens=458
```

#### The 6 chunks exactly as persisted (verbatim OCR text)

Chunk id = `uuid5(sha1_ns, text|page_start|page_end)` — deterministic, so re-running the same OCR text yields the same id (see dedup note in Part 3). Data pulled live from the Qdrant/Meilisearch payloads.

| idx | chunk id | pages | bbox (x0,y0,x1,y1) | tokens | chars |
|---|---|---|---|---|---|
| 0 | `01a58a7f-ff8c-5fc9-a333-0acd37dfe072` | 1..1 | 0,0,765,270 | 165 | 922 |
| 1 | `9b275127-7cc5-581a-94ab-bbcf443c4ef7` | 1..2 | 0,0,16200,522 | 435 | 2,468 |
| 2 | `a8be82b8-c59a-5046-a7aa-23a04d92b748` | 3..4 | 0,0,840,594 | 403 | 2,431 |
| 3 | `3cad8016-5e25-5305-a3cb-6f46e5a8eba4` | 4..5 | 0,0,16770,432 | 458 | 2,527 |
| 4 | `bdb98547-1427-5d6c-ada8-0d7792bccf4c` | 6..7 | 0,0,12720,216 | 417 | 2,179 |
| 5 | `d1e37b77-c2d5-5ec2-ad18-46cd49fb9001` | 7..7 | 0,270,880,594 | 146 | 833 |

**Chunk 0** (page 1, 922 chars):
```text
وأنكر بعض العلماء هذه الألقاب : منهم: القرطبي في «شرح أسماء الله الحسنى»، فقال : قد دل الكتاب والسنة على المنع من تزكية الإنسان نفسه، قال علماؤنا : ويجري هذا المجرى ما كثر في الديار المصرية وغيرها من بلاد العرب والعجم من نعتهم أنفسهم بالنعوت التي تقتضي التزكية والثناء كزكي الدين، ومحيي الدين، وعلم الدين وشبه ذلك(. ومنهم: ابن النحاس(في «تنبيه الغافلين» عند ذكر المنكرات : فمنها ما عمت به البلوى في الدين من الكذب الجاري على الألسن وهو ما ابتدعوه من الألقاب : كمحيي الدين، ونور الدين، وعضد الدين، وغياث الدين، ومعين الدين، وناصر الدين، ونحوها من الكذب الذي يتكرر على الألسن حال النداء والتعريف والحكاية، وكل ذلك بدعة في الدين ومنكر. انتهى(. ولكن اللكنوي(أجابهم بعد ذكر كلامهم بقوله : هذا إذا لم يكن مَن وُصِفَ به أهلاً له أو كان أهلاً وأراد به تزكية نفسه. انتهى(. ويؤيد هذا أن مَن لُقِّبَ بهذه الألقاب هم كبار العلماء والفقهاء العارفين بأحكام الدين، فلو لم يكن ذلك جائزاً شرعاً لَمَا ارتضوه، وأطلقوه على بعضهم. والله أعلم.
```

**Chunk 1** (pages 1–2, 2,468 chars):
```text
(ينظر: «الفوائد البهية» (ص . (وهو أحمد بن إبراهيم بن محمد الدمشقي الدمياطي، محيي الدين، المعروف بابن النحاس، قال السخاوي: كان حريصاً على أفعال الخير مؤثراً للخمول كثير المرابطة والجهاد. من مؤلفاته: «مشارع الأشواق إلى مصارع العشاق»، و«مثير الغرام إلى دار السلام»، و«المنكرات والبدع»، (ت ٨١٤هـ). ينظر: «الضوء اللامع» (١: ٢٠٣ - . «الطبقات السنية» (ص . (من «الفوائد البهية» (ص . (وهو محمد عبد الحي بن عبد الحليم اللكنوي الأنصاري الحنفي، وهو أحد مجددي المئة الثالثة عشرة الهجرية، له: «حاشية الهداية»، و«التعليق الممجد على موطأ محمد»، و«الرفع والتكميل في الجرح والتعديل»، (ت ١٣٠٤هـ). ينظر: «مقدمة التعليق» (١: ١٠٩ - . «الإمام عبد الحي» (ص ٥٥ . «المنهج الفقهي» (ص ٢٩ - . (من «الفوائد البهية» (ص . المبحث الثاني اسم صاحب (ائوقاية») اختلف العلماءٌ اختلافاً كبيراً في اسم صاحب «الوقاية» بعدما pial‏ | على أنه لصدر الشريعة الأصغر dee‏ الله بن مسعودء وابن لصدر الشريعة SW‏ وأن لقبه برهان الشريعة» وأن جد صدر الشريعة الصحيح هو تاج وهو شارح «البداية»'''» وهذا ما نص عليه صدر الشريعة في ديباجة «النقاية» إذ قال: ويعد ؛ فإنّ العبد المتوسّل إلى الله بأقوى الذريعة عبيد الله صدر الشريعة بن مسعود بن تاج الشريعة سعد جدهء يقول: قد ألف جدّي ومولاي العالم الرباني» والعامل الصمدانيء Olay‏ الشريعة والحقّ والدين: محمود بن صدر الشريعة جزاه الله عنّي وعن سائر المسلمين Se‏ الجزاء ؛ لأجل حفظي كتاب «وقاية الرواية في مسائل البداية»... OB‏ وقال في ديباجة «التوضيح»: ويعد : فإن العبد المتوسّل إلى الله تعالى بأقوى الذريعة عبيد الله بن مسعود ابن تاج الشريعة سعد جده وأنضجح جده. انتهى'". ومثله في ديباجة «شرح الوقاية». فعبارة صدر الشريعة تنص على أن alee‏ الصحيح هو تاج الشريعة؛ وأن له جدا آخر ad‏ برهان الشريعة ألف له «الوقاية»؛ واسمه محمودء فكلامه يحتمل وجهين: )1( كون تاج الشريعة هو شارح «الجداية» لم ينص عليه صدر الشريعة pall Lely‏ عليه علماء alll‏ الحنفي الذي أكثروا من النقل عنه في كتبهم » والاستفادة من تحقيقاته؛ منهم: العيني في مواضع كثيرة جداً من «البناية»: ومنهم ابن البمام في (V‏ مواضع في «فتح القدير» منها(4: VE‏ ومنهم قاضي زاده في (OP)‏ موضعاً في «نتائج الأفكار»منها(١1:‏ :ومنهم ابن نجيم في (موضع في«البحر» (OFT tA) gue‏ وملهم : ملا خسرو في )0( مواضع في«درر الحكام)(١‏ : Tages (TOY‏ شيخ زاده في () مواضع في «تجمع الأنهر» CEVA Wie‏ ومنهم: PALA‏ في (TA)‏ موضعا o‏ «الشرتبلالية)(؟ : CPV‏ ومنهم : مؤلفو «الفتاوى الجندية»(؟: 4 ومنهم : الخادمي في )1( مواضع في dy‏ محمودية»منها ف Ved‏ ومنهم : ابن عابدين في song Lace CVV)‏ الممتار»70: افد موضعين في «العقود الدرية» منهما(؟: COTY‏ وفي OV‏ مواضع 3 «منحة الخالق» منها ( زفة انتهى من pee‏ الوقاية)) المسمى ب«التقاية»اص AT‏ (؟) من «التوضيح)(١:‏ 4 -.
```

**Chunk 2** (pages 3–4, 2,431 chars) — this is the chunk the traced answer cites as [P2]/Page 3:
```text
الأول: أن يكون تاج الشريعة هو برهان الشريعة، فيكون اسمه محمودا، ويكون هو شارح «الهداية»؛ لأن كلمة علماء الأحناف اتفقت على أن تاج الشريعة هو شارح «الهداية» كما سبق، وهذا ما اختاره الكفوي(في «كتائب أعلام الأخيار»، ومشى عليه في ترجمة تاج الشريعة(، وفي ترجمة صدر الشريعة(، وابن قُطْلُوبُغَا(، وابن الحنائي(، وطاشكبرى زاده(، والزركلي(، وكذا رأى مرتضى الزبيدي(نسبه في «تاريخ بخارا»(. (وهو محمود بن سليمان الكَفَوي الرومي الحنفي، من مؤلفاته: «كتائب أعلام الأخيار من فقهاء مذهب النعمان المختار»، و«شرح آداب البحث»، (ت نحو ٩٩٠هـ). ينظر: «التعليقات السنية» (ص. «الأعلام» (٨: . (ينظر: «كتائب أعلام الأخيار» (ق٢٦٥/أ). (ينظر: «كتائب أعلام الأخيار» (ق٢٨٧/أ). (في «تاج التراجم» (ص. وابن قطلوبغا هو قاسم بن قُطْلُوبُغَا بن عبد الله السودوني المصري الحنفي، أبو العدل، زين الدين، من مؤلفاته: «تحفة الأحياء بتخريج أحاديث الإحياء»، «الأصل في بيان الفصل والوصل»، و«الترجيح والتصحيح على القدوري»، (٨٠٢ - ٨٧٩هـ). ينظر: «الضوء اللامع» (٥: ١٨٤ - . «البدر الطالع» (٤٥ - . (في «طبقات الحنفية» (ق١٢٦/أ). وابن الحنائي هو علي بن أمر الله بن عبد القادر الحميدي الرومي، الشهير بقنالي زاده، سيف الدين، له: «حاشية على الدرر»، و«طبقات الحنفية»، و«حاشية على البيضاوي»، (٩١٨ - ٩٧٩هـ). ينظر: «الكشف» (٢: . «مجلة المورد» العددان (٣ - صح ١٩٨١، (ص ٤٨٦ - . (في «مفتاح السعادة» (٢: ١٧٠، . وطاشكبرى زاده هو أحمد بن مصطفى، أبو الخير، عصام الدين، من مؤلفاته: «الشقائق النعمانية في علماء الدولة العثمانية»، و«مفتاح السعادة ومصباح السيادة»، «حواشي على البيضاوي»، (٩٠١ - ٩٦٨هـ). ينظر: «التعليقات السنية» (ص١٢٣ - . «الشقائق» (ص ٣٢٥ - . (في «الأعلام» (٤: . (وهو محمد بن محمد بن محمد بن عبد الرزاق، الشهير بمرتضى الحسيني الهندي الأصل، الزبيدي المصري الحنفي. من مؤلفاته: «عقد الجواهر المنيفة في أدلة مذهب أبي حنيفة»، و«تاج العروس شرح القاموس»، و«إتحاف السادة المتقين في شرح إحياء العلوم»، (١١٤٥ - ١٢٠٥هـ). ينظر: «معجم المؤلفين» (٣: . «القول الجازم» (ص. (ينظر: «دفع الغواية» (١: . والثاني: أن يكون تاج الشريعة هو الجد الصحيح لصدر الشريعة، وهو شارح «الهداية»، ويكون برهان الشريعة هو جده الفاسد، واسمه محمود، وتاج الشريعة وبرهان الشريعة، ابنان لصدر الشريعة الأكبر. وهو الذي اختاره لما ذكره صاحب «الكشف»(أثناء ذكر شروح «الهداية» نقلاً عن تاج الشريعة في «شرح الهداية» في آخر (كتاب الأيمان) أنه قال: أتم تحرير فوائد كتاب الأيمان أبو عبد الله عمر بن صدر الشريعة في آخر شعبان سنة ثلاث وسبعين وستمئة. انتهى(. فهذه عبارة صريحة من تاج الشريعة على أن اسمه عمر، وليس محموداً، وأنه ابن لصدر الشريعة الأكبر، وهذا ما اختاره القُهستاني(، وحاجي خليفة(، واللَّكنوي(، وإسماعيل باشا(، وعمر كحالة(، وغيرهم.
```

**Chunk 3** (pages 4–5, 2,527 chars):
```text
(وهو مصطفى بن عبد الله القسطنطيني الرومي الحنفي، الشهير بالملا كاتب الجلبي، والمعروف بحاجي خليفة، من مؤلفاته: «تحفة الكبار في أسفار البحار»، و«تقويم التواريخ»، و«ميزان الوصول إلى طبقات الفحول»، (١٠١٧ - ١٠٦٧هـ). ينظر: «مقالات الكوثري» (ص . «الأعلام» (٨: ١٣٨ - . «معجم المؤلفين» (٣: ٨٧٠ - . (من «كشف الظنون» (٢: . (في «جامع الرموز في شرح النقاية» (١: . والقهستاني هو مُحَمَّدُ الخُراساني القُهستاني، شمس الدين، المفتي ببخارا، من مؤلفاته: «جامع الرموز في شرح النقاية»، (ت: نحو: ٩٥٣هـ). ينظر: «غيث الغمام» (ص . «الكشف» (٢: . «تذكرة الراشد» (ص . (في «الكشف» (٢: . (في «الفوائد» (ص ، و«مقدمة عمدة الرعاية» (١: ، و«دفع الغواية» (١: . (في «هدية العارفين» (١: ٧٨٧، . («معجم المؤلفين» (١: . المبحث الثالث نسب صاحب «الوقاية» يتصل نسب صاحب «الوقاية» بعغبادة بن الصامت الصحابي الجليل Be‏ وممن ذكر نسبه والتميمي”' والكفوي”" ٠ ووقع بينهما اختلاف في أسماء بعض فكان على TON we‏ الأولى: هو ابن صدر الشريعة الأكبر أحمد بن عبيد الله ين إبراهيم بن أحمد بن عبد الملك بن عمر بن عبد العزيز بن محمد بن جعفر بن مروان بن محمد بن أحمد بن want‏ بن الوليد بن عبادة بن الصامت العبادي المحبوبي (aid GIN‏ هكذا ذكره القرشي والتميمي: والكفوي أوصله إلى عبد العزيز بن محمد. والثانية: هو ابن صدر الشريعة الأكبر أحمد بن جمال الدين أبي المكارم عبيد الله ابن إبراهيم بن أحمد بن عبد الملك بن عمير بن عبد العزيز بن LAF‏ بن جعفر بن خلف ابن هارون بن محمّد بن محمد بن محْبُوبٍ بن الوليد بن BILE‏ بن الصامت الصحابي الأنصاري «ip aye‏ قاله Le‏ المولى الدّمياطي في «تعاليق الأنوار على الدرَّ المختار»: هكذا Gl‏ في مسلسلات شيخنا ASI‏ مرتضى الحسيني» قال شيخنا: WS‏ رأيت نسبه في «تاريخ Oley‏ فالعيادي بضم العين نسبة إلى عبادة بن الصامت be‏ Sole أحد أجداده على‎ oye نسبة إلى‎ Gly )١(‏ في «الجواهر المضية)(1 .)44١0 FT VAT:‏ والقرشي هو محمد بن عبد القادر بن محمد القرشي الحنفي» له : «الجواهر المضية في طبقات الحنفية», و«البستان في مناقب إمامنا التعمان», و«الدرر المثيغة في الود على ابن أبي شيبة عن الإمام أبي حنيفة», (197 -هلالاه). ينظر: «الجواهر»(؟: MOA~ ٠١‏ «الفوائد)( 2 NVA‏ -, (؟) في «الطبقات (PVT : Vcd cell‏ والتميمي هو تقي الدين بن عبد القادر التميمي الداري الغزي المصري gel‏ + من مؤلفاته : «الطبقات السنية ف تراجم و«السيف البراق في عنق الولد SLM‏ و«مختصر يتيمة الدذهر», (ت8١٠١١ه).‏ ينظر: «الخلاصة»(١‏ : 41/9 -. «الطبقات السنية»(1 : ع -و). ATV GX LEY في «كتانب أعلام‎ )( (وهو عبد المولى بن عبد الله بن عبد القادر Ul‏ المغربي الحنفي تلميذ الطحطاوي ٠‏ من مؤلفاته: «تعاليق الأنوار على الدر المختار» ؛ وصفها الإمام اللكنوي بأنها حاشية نفيسة .فرغ منها GANTT ينظر : «التعليقات السنية»(ص.‏ «مقدمة : AVA‏ )0( هكذا في poet in‏ 186 -, و«دفع الغواية»(1 : CY‏ و«امقدمة عمدة الرعاية»(1 : .```

**Chunk 4** (pages 6–7, 2,179 chars):
```text
ما وقع من العلماء من الخلط 2 نسب صدر الشريعة إذ تقرّر ما سبق من أن نسب صدر الشريعة هو: : عبيد الله بن مسعود بن عمر تاج الشريعة بن أحمد صدر الشريعة الأكبر بن عبيد الله جمال الدين أبي المكارم بن إبراهيم ابن أحمد... إلى أن يصل إلى عبادة بن الصامت نه فإنّه وقع اضطراب وخلط كبير بين المترجمين لصدر الشريعة : .١‏ منهم: قاسم بن Wyld‏ إذ قال: محمود بن عبيد الله بن محمود. انتنهى”". فجعل عبيد الله والدا aged‏ مع أنه elie‏ ووالده هو أحمد؛ وجعل والد عبيد الله حموداً مع أن والده اسمه إبراهيم. .١‏ ومنهم: طاشكبرى val‏ قال كما قال ابن فُطْلُويُغا ويبدو أنه اعتمدَ عليه» وجوابه كجوابه. وقال أيضاً: صدر الشريعة عبيد الله بن محمود بن محمد البرهاني. OP gg‏ وفيه أن محمود بن محمد والد لعبيد الله والصحيح أن والده مسعود بن عمرء وفيه أيضاً: أنه نسبه «sla‏ وهذه التسبة لم تعرف له» وإنما عرف بها علماء آخرون. gta ‘eet‏ إذ قال: عمر بن صدر الشريعة عبيد الله بن محمود بن حمد. انتهى”'“. وفيه أن عبيد الله والد عمر؛ والصحيح أنه جدّهء وأيضا: أن محمود بن محمد والد عبيد cdl‏ والصواب أن والد عبيد الله هو إبراهيم بن أحمد. وقال أيضاً: حمود بن صدر الشريعة عبيد الله بن محمود بن محمد. انتهى"". وفيه أن صدر الشريعة الأكبر هو عبيد الله وهو والد تحمودء والصحيح أن صدر الشريعة الأكبر هو أحمدء وأن عبيد الله هوجد محمودء وأيضاً: محمود بن محمد والد عبيد الله والصواب أن إبراهيم بن أحمد هو والد عبيد الله. )١(‏ من clin‏ التراجم)»اص”. )1( في «مفتاح السعادة)(؟ : 1 OF)‏ من «الشقائق النعمانية)»0اص.‏ (من «جامع VK yo Bl‏ ؟). )0( من ««جامع Ayes Vj ya pl‏ ومنهم: ابن الحنائي: إذ قال: جمال الدين المحبوبي عبد الله بن إبراهيم. انتهى(. والصواب أنه عبيد الله لا عبد الله. ومنهم: القاري(إذ قال في حرف العين: عبيد الله بن مسعود تاج الشريعة. انتهى(. وفيه أن مسعودا هو تاج الشريعة والصحيح ان تاج الشريعة هو والد مسعود. وقال في حرف الميم: مسعود بن أحمد بن برهان الدين، صدر الشريعة. انتهى(. وفيه أن صدر الشريعة مسعود، والصواب أن صدر الشريعة عبيد الله بن مسعود، وأيضاً: أن أحمد بن برهان الدين والد مسعود، والصحيح هو عمر بن أحمد هو والد مسعود. ومنهم: اللكنوي إذ قال: عبيد الله بن أحمد بن عبد الملك. انتهى(. وفيه أن أحمد والد عبيد الله، والصواب أن والد عبيد الله هو إبراهيم. ومنهم: الزركلي إذ قال: صدر الشريعة الأصغر ابن صدر الشريعة الأكبر. انتهى(. وخطؤه بين فصدر الشريعة الأصغر هو ابن مسعود بن عمر تاج الشريعة بن أحمد صدر الشريعة الأكبر.```

**Chunk 5** (page 7, 833 chars):
```text
المبحث الخامس أسرته العلمية وطلبه للعلم وشيوخه ومن تفقه عليهم نشأ صدر الشريعة في أسرة عريقة النسب على ما مرّ، ولها مكانتها العلمية المرموقة كما سيأتي بعد قليل عند ترجمة أجداده، ووجد عناية كبيرة منهم ولا سيما من جده مؤلف «الوقاية»، إذ ألفها من أجله لكي يحفظها كما صرّح في ديباجتها، وذلك بعد أن أتم دراسة بعض العلوم الأخرى فقال: إنَّ الولد الأعز عبيد الله صرف الله أيامه (من «طبقات ابن الحنائي»، (ق١/٢٥أ). (وهو علي بن سلطان محمد الهروي القاري الحنفي، أبو الحسن، نور الدين، له: «فتح باب العناية بشرح النقاية»، و«مرقاة المفاتيح شرح مشكاة المصابيح»، و«الأثمار الجنية في طبقات الحنفية»، و«شرح مسند الإمام»، (٩٣٠ - ١٠١٤هـ) . ينظر: «الكواكب السائرة»، (١: ٤٤٥ - . «طرب الأماثل»، (ص ٥١٥ - . «الإمام علي القاري»، (ص . (من «الأثمار الجنية في طبقات الحنفية»، (ق١/٣٦أ). (من «الأثمار الجنية»، (ق٥٠/ب). (من «النافع الكبير»، (ص . (من «الأعلام»، (٤: .
```

Note: this book's PDF is an image scan, so every page's words come from Gemini OCR (`_extract_gemini` → `_text_to_words`); the embedded text layer is only used for footer page-number extraction. Pages 2, 5, 6 show garbled Latin run-ins (`pial‏`, `SW‏`, `TON we‏`) — these are Gemini OCR misreads of the scan, not text-layer content. Page-level OCR text itself is NOT persisted anywhere — only these 6 chunk texts (Qdrant + Meilisearch) retain the actual OCRed content.

### Step 4 — Embedding

File: `app/core/embedder.py` · Model: `GEMINI_EMBEDDING_MODEL=gemini-embedding-001`

Chunk texts are sent to Gemini `batchEmbedContents` in batches of 10 (`EMBED_BATCH_SIZE`), 10 concurrent batches, retry/backoff on 429/503.

```log
[TRACE] embed texts=6 batches=1 batch_size=10 model=gemini-embedding-001
[TRACE] embed done vectors=6 dim=3072
[TRACE] phase=embed_done book_id=daf67c12-873d-4690-b8be-c31efab7ae2a vectors=6 dim=3072
```

Each chunk → one 3,072-dimension vector (dense semantic representation of the chunk text).

### Step 5 — Indexing

File: `app/pipeline/indexer.py` — two stores, same 6 documents:

**A. Qdrant (vector DB)** — `documents` collection, COSINE distance, 3072 dims. Payload keeps every metadata field; point id = UUID5 of `text|page_start|page_end` (deterministic).

```log
[TRACE] qdrant index start chunks=6 vectors=6 vector_size=3072 collection=documents
[TRACE] qdrant upsert done points=6 collection=documents
```

**B. Meilisearch (keyword search)** — `documents` index, live settings: `searchableAttributes=["*"]`, `filterableAttributes=[language, tenant_id, book_id, book_type]`, default ranking rules (words, typo, proximity, attributeRank, sort, wordPosition, exactness).

```log
[TRACE] meilisearch index start docs=6 index=documents
[TRACE] meilisearch add_documents done docs=6 task_uid=56
[TRACE] phase=index_done book_id=daf67c12-873d-4690-b8be-c31efab7ae2a chunks_indexed=6
```

**C. Postgres** — book row updated: `status='ready', total_pages=7, total_chunks=6`, job `completed/100%`.

---

## PART 2 — RETRIEVAL + ANSWER (question → answer)

Files: `app/api/ask.py` → `app/rag/agent.py` (LangGraph) → `app/rag/retriever.py` → `app/rag/reranker.py`

Graph: `retrieve → [quality_gate] → generate | retry → [quality_gate] → generate | no_result`

### Step 6 — Ask request

```log
[TRACE] ask start tenant=default book_id=daf67c12-873d-4690-b8be-c31efab7ae2a question='ما اسم صاحب كتاب الوقاية؟'
```

- Cache check (Redis, 5s bounded read) — miss on first run.
- Graph invoked with initial state (question, tenant, book_id, retry_count=0).

### Step 7 — Query preparation (retrieve_node)

1. **Language detection** (`detect_language`): `ar`.
2. **Translation for retrieval** (`translate_for_retrieval`) → Arabic leg.
3. **Translation to English** (`translate_to_english`) → English leg (indexed text may be English).
4. **Transliteration normalisation** (ā/ṣ/ʿ → plain ASCII).
5. **Keyword query** = content terms of the English leg (stopwords removed).

```log
[TRACE] retrieve_queries lang=ar
arabic='ما اسم صاحب كتاب الوقاية؟'
english='What is the name of the author of the book Al-Wiqaya?'
keyword='name author book al-wiqaya'
embed_query='ما اسم صاحب كتاب الوقاية؟ What is the name of the author of the book Al-Wiqaya?'
```

The **embedding query** is the concatenation `arabic + normalised + english` so the vector search fires for both scripts.

### Step 8 — Query embedding

```log
[TRACE] embed texts=1 batches=1 batch_size=10 model=gemini-embedding-001
[TRACE] embed done vectors=1 dim=3072
```
(Results are cached in Redis key `embed:<tenant>:<sha256(query)>` for 7 days.)

### Step 9 — Hybrid search (two legs in parallel, top_k=20 each)

**Leg 1 — Qdrant vector search** (cosine similarity, filtered `tenant_id` + `book_id`):

```log
Qdrant vector_search: collection=documents
filter=[{"key": "tenant_id", "match": "default"}, {"key": "book_id", "match": "daf67c12-873d-4690-b8be-c31efab7ae2a"}]
limit=20
Qdrant vector_search: raw_count=7
results=[{"score": 0.7122, "book": "احیاء العلوم عربی جلد", "page": 1},
         {"score": 0.6588, "book": "احیاء العلوم عربی جلد", "page": 4},
         {"score": 0.6585, "book": "احیاء العلوم عربی جلد", "page": 7},
         {"score": 0.5753, "book": "احیاء العلوم عربی جلد", "page": 3},
         {"score": 0.5743, "book": "احیاء العلوم عربی جلد", "page": 3},
         {"score": 0.5510, "book": "احیاء العلوم عربی جلد", "page": 1},
         {"score": 0.5350, "book": "احیاء العلوم عربی جلد", "page": 6}]
```

The page-1 chunk (which contains "المبحث الثاني اسم صاحب «الوقاية»" and the dibaja quotes) is the semantic top hit at **0.7122**.

**Leg 2 — Meilisearch keyword search** (`matchingStrategy=frequency`):

```log
Meilisearch keyword_search: filter=['tenant_id = default', 'book_id = daf67c12-873d-4690-b8be-c31efab7ae2a']
query='name author book al-wiqaya' raw_count=0
```

0 hits — the chunk text is Arabic, the keyword terms are English; for this Arabic book the **vector leg carries retrieval** while keyword search helps English-text books. (Both legs run concurrently with `asyncio.gather`.)

### Step 10 — Reranking (RRF + lexical overlap + entity boost)

File: `app/rag/reranker.py`. Combines both legs:
- **RRF**: `1/(rank+60)` per leg, summed for docs present in both.
- **Lexical overlap**: fraction of question terms (normalised, stopword-stripped, Arabic diacritics removed, alef/hamza normalised) found as substrings in chunk text; for Arabic questions the English translation is also tried.
- **Entity boost**: named entities (e.g. «ابن حجر», «تاج الشريعة») matching the chunk add up to +0.15.
- Final: `score = rrf + 0.5 * overlap + entity`, then duplicate overlapping chunks are suppressed.

Real score breakdown (only top results shown; the `0.75` on page 3 = 3/4 query terms `اسم`+`صاحب`+`كتاب` found in that chunk — e.g. "صاحب «الكشف»"):

```log
[TRACE] rerank_component rrf=0.0167 overlap=1.0000 entity=0.0000 final=0.5167 page=1   ← اسم صاحب كتاب الوقاية all match
[TRACE] rerank_component rrf=0.0159 overlap=0.7500 entity=0.0000 final=0.3909 page=3
[TRACE] rerank_component rrf=0.0164 overlap=0.5000 entity=0.0000 final=0.2664 page=4
[TRACE] rerank_component rrf=0.0161 overlap=0.2500 entity=0.0000 final=0.1411 page=7
[TRACE] rerank_component rrf=0.0154 overlap=0.2500 entity=0.0000 final=0.1404 page=1
[TRACE] rerank_component rrf=0.0152 overlap=0.2500 entity=0.0000 final=0.1402 page=6
[TRACE] rerank_results count=6
```

### Step 11 — Quality gate (generate / retry / no-result)

File: `app/rag/agent.py` `quality_check_node`. Generate only if **best_score ≥ 0.30 (RAG_MIN_CONFIDENCE_SCORE)** OR **top raw vector cosine ≥ 0.65 (RAG_VECTOR_MIN_CONFIDENCE)**; otherwise retry once (LLM rephrases the question), then refuse.

```log
quality_gate: best_score=0.5167 threshold=0.30 top_vector_score=0.7122 vector_min=0.65 passages=6 -> generate
```

Both gates passed: lexical overlap is high AND the raw vector hit is semantically close.

**Real negative example** — the same book, garbled question (sent as `???`, translated to "Where is the Al-Aqsa Mosque located?"):
```log
quality_gate: best_score=0.0167 threshold=0.30 top_vector_score=0.4929 vector_min=0.65 passages=6 -> retry
quality_gate: best_score=0.0167 threshold=0.30 top_vector_score=0.4929 vector_min=0.65 passages=6 -> no_result
ask done tenant=default elapsed_ms=2741 no_result=True sources=0 answer='لا توجد معلومات ذات صلة في المصادر المقدمة. Try rephrasing your question.'
```
Unrelated content scores ~0.49 cosine and ~0.016 rerank → refused instead of hallucinating. This is the safety mechanism working.

### Step 12 — Generation (grounded answer)

File: `app/rag/agent.py` `generate_node`. The 6 reranked passages are tagged **[P1]–[P6]** with `Source: <book>, Page <n>` and placed into the grounding system prompt:

```log
[TRACE] generate prompt for question='ما اسم صاحب كتاب الوقاية؟' language=Arabic
[TRACE] generate full prompt: ...
```

```text
You are an Islamic knowledge assistant specialising in the Deobandi tradition.
Answer ONLY using the passages provided below. Do not use your own knowledge.

Rules:
1. If the passages do not answer the question, respond with exactly:
   No relevant information found in the provided sources.
2. CITATION FORMAT — STRICT: cite every claim with [Pn, Page X]. Never invent a page number.
3. CITATION SCOPING — cite each claim in the sentence it appears.
4. CONTRADICTIONS — if passages disagree, state each passage's claim separately and note the disagreement.
5. SPECIFICITY — reproduce the specific reasoning present in the source.
6. Your ENTIRE answer must be written in Arabic.

PASSAGES:
[P1] (ينظر: «الفوائد البهية» ... المبحث الثاني اسم صاحب (الوقاية) اختلف العلماء اختلافاً كبيراً ...
    Source: احیاء العلوم عربی جلد, Page 1
[P2] الأول: أن يكون تاج الشريعة هو برهان الشريعة، فيكون اسمه محمودا ...
    Source: احیاء العلوم عربی جلد, Page 3
[P3] (وهو مصطفى بن عبد الله القسطنطيني الرومي الحنفي ...
    Source: احیاء العلوم عربی جلد, Page 4
... (up to [P6], each ≤ 800 tokens)
```

Post-processing after the LLM call:
1. **Citation validation** — re-generate with a stricter prompt if `[Pn]` tags are out of range.
2. **Consistency check** — since ≥2 passages were cited, an LLM verdict (AGREE/CONTRADICT) was asked; it answered CONTRADICT → a note is prepended:
   > ⚠️ Note: the source passages contain a disagreement on this point. Each passage's claim is stated separately below.
3. **Citation resolution** — `[P1, Page 2]` tags are replaced by the real `[Book, Page]` from chunk metadata (never from LLM memory).
4. **Language enforcement** — answer is Arabic; detected script already matches.

```log
[TRACE] generate answer='⚠️ Note: the source passages contain a disagreement on this point. ...
اختلف العلماء في اسم صاحب "الوقاية" [احیاء العلوم عربی جلد, Page 1]، وذكر بعضهم أن اسم صاحب "الوقاية"
هو محمود بن صدر الشريعة [احیاء العلوم عربی جلد, Page 3]، في حين ذكر آخرون أن اسم صاحب "الوقاية"
هو برهان الشريعة [احیاء العلوم عربی جلد, Page 3]، وآخرون قالوا أن اسم صاحب "الوقاية" هو تاج الشريعة
[احیاء العلوم عربی جلد, Page 3]، وهناك خلاف في اسم صاحب "الوقاية" بين العلماء.'
```

### Step 13 — Response assembly

- 6 sources, each with rank, book, page, relevance score, chunk text, bbox, and a **highlight_url** (`/highlight?book_id=...&page=...&bbox=...&text=...`) used by the frontend to highlight the passage on the page image.
- Answer cached in Redis (so the same question within TTL is served from cache — `was_cached: true`).
- Stats recorded (queries, cache hits/misses, response time).

```log
[TRACE] ask done tenant=default elapsed_ms=4089 no_result=False cached=False sources=6
```

### Final answer returned by the API

```json
{
  "question": "ما اسم صاحب كتاب الوقاية؟",
  "answer": "⚠️ Note: the source passages contain a disagreement on this point. Each passage's claim is stated separately below.\n\nاختلف العلماء في اسم صاحب \"الوقاية\" [احیاء العلوم عربی جلد, Page 1]، وذكر بعضهم أن اسم صاحب \"الوقاية\" هو محمود بن صدر الشريعة [احیاء العلوم عربی جلد, Page 3]، في حين ذكر آخرون أن اسم صاحب \"الوقاية\" هو برهان الشريعة [احیاء العلوم عربی جلد, Page 3]، وآخرون قالوا أن اسم صاحب \"الوقاية\" هو تاج الشريعة [احیاء العلوم عربی جلد, Page 3]، وهناك خلاف في اسم صاحب \"الوقاية\" بين العلماء.",
  "was_cached": false,
  "no_result": false,
  "sources": [
    { "rank": 1, "book_name": "احیاء العلوم عربی جلد", "page": 1, "relevance_score": 0.5167, "highlight_url": "/highlight?book_id=daf67c12-...&page=1&..." },
    { "rank": 2, "book_name": "احیاء العلوم عربی جلد", "page": 3, "relevance_score": 0.3909, "bbox": [0,0,840,594] },
    { "rank": 3, "book_name": "احیاء العلوم عربی جلد", "page": 4, "relevance_score": 0.2664 },
    { "rank": 4, "book_name": "احیاء العلوم عربی جلد", "page": 7, "relevance_score": 0.1411, "bbox": [0,270,880,594] },
    { "rank": 5, "book_name": "احیاء العلوم عربی جلد", "page": 1, "relevance_score": 0.1404, "bbox": [0,0,765,270] },
    { "rank": 6, "book_name": "احیاء العلوم عربی جلد", "page": 6, "relevance_score": 0.1402 }
  ]
}
```

---

## PART 3 — LIVE STATE REFERENCE (pulled from the running system on 2026-08-07)

### Postgres

`books`:

| id | title | language | status | total_pages | total_chunks | created_at |
|---|---|---|---|---|---|---|
| `1ef3d955-0a27-4892-a08f-5a4e26885732` | How to love prophet | en | ready | 8 | 7 | 2026-08-02 09:11:06+00 |
| `daf67c12-873d-4690-b8be-c31efab7ae2a` | احیاء العلوم عربی جلد | ar | ready | 7 | 6 | 2026-08-07 14:46:49+00 |

`processing_jobs` (both completed):

| id | book_id | status | progress_pct | current_step | retry_count | error_msg |
|---|---|---|---|---|---|---|
| `a995124a-bd39-4bf3-b6f3-81f7d3680955` | `1ef3d955-…` | completed | 100 | completed | 0 | (none) |
| `378c40f0-008a-4556-8ed9-dae58011017d` | `daf67c12-…` | completed | 100 | completed | 0 | (none) |

### Qdrant

Collection `documents` (live `/collections/documents`):

```json
{"result": {"status": "green",
  "config": {"params": {"vectors": {"size": 3072, "distance": "Cosine"},
    "shard_number": 1, "replication_factor": 1, "write_consistency_factor": 1,
    "on_disk_payload": true},
    "hnsw_config": {"m": 16, "ef_construct": 100, "full_scan_threshold": 10000,
    "max_indexing_threads": 0, "on_disk": false},
    "optimizer_config": {"deleted_threshold": 0.2, "vacuum_min_vector_number": 1000,
    "indexing_threshold": 10000, "flush_interval_sec": 5},
    "wal_config": {"wal_capacity_mb": 32, "wal_segments_ahead": 0},
    "quantization_config": null}, "payload_schema": {}}}
```

Points for this book: **6** (verified via `/points/count` with the `book_id` filter).

One point's payload schema (all 6 share it; `id` = the deterministic uuid5):

```json
{"id": "a8be82b8-c59a-5046-a7aa-23a04d92b748", "vector": [0.0123, ... 3072 floats ...],
 "payload": {"tenant_id": "default", "book_id": "daf67c12-873d-4690-b8be-c31efab7ae2a",
   "book_name": "احیاء العلوم عربی جلد", "author": "", "language": "ar", "book_type": "",
   "chapter": null, "page_start": 3, "page_end": 4,
   "physical_page_start": 3, "physical_page_end": 4,
   "text": "<chunk 2 verbatim text>",
   "bbox": [0.0, 0.0, 840.0, 594.0],
   "minio_path": "books/default/daf67c12-873d-4690-b8be-c31efab7ae2a/original.pdf"}}
```

### Meilisearch

Index `documents` (live `/indexes/documents/settings`): `searchableAttributes=["*"]`, `filterableAttributes=["language","tenant_id","book_id","book_type"]`, `distinctAttribute=null`, `pagination.maxTotalHits=1000`, no embedders. Doc count for this book: **6** (verified via the documents endpoint with the `book_id` filter).

The Meilisearch document = the Qdrant payload plus the deterministic `id` (same uuid5), e.g. `{"id": "a8be82b8-…", "text": "...", "tenant_id": "default", ...}`.

### Dedup / idempotency note (observed during tracing)

Chunk ids are deterministic, so re-ingesting the same OCR output is idempotent. During tracing a stale duplicate appeared: an earlier run's Gemini OCR returned a diacritic variant of the page-3 chunk (الكفوي vs الكَفَوي), producing a different uuid5 (`8c0a3400-…` vs `a8be82b8-…`) — both were indexed, so the book briefly had 7 points/7 docs. At retrieval time the reranker's duplicate-suppression dropped one of the two overlapping page-3..4 chunks. Cleanup: removed `8c0a3400-…` from both Qdrant (point delete, op 50) and Meilisearch (`POST /indexes/documents/documents/delete-batch` with body `["8c0a3400-…"]`, task 59) — both stores now hold exactly the 6 chunks in Part 1. A future fix would be to make the id independent of OCR diacritics (e.g. hash of diacritic-stripped text).

**Repair incident (post-tracing)**: restoring the `a8be82b8-…` point's payload from a PowerShell-captured curl dump corrupted the Arabic (console code-page round-trip produced mojibake: `╪º┘ä╪ú┘ê┘ä:` instead of `الأول:`). Fixed via Qdrant's `set_payload` (op 52, `POST /collections/documents/points/payload`, `wait=true`, body `{"payload":{...},"points":["a8be82b8-…"]}`) executed from inside the worker container with Python — this keeps the already-computed 3072-dim vector untouched. Lesson for tooling: when reading Arabic JSON from curl, never pipe stdout through a PowerShell variable (`$x = & curl.exe …` re-decodes bytes via the console code page); write raw with `curl.exe -o file` and read the file with `[System.IO.File]::ReadAllText(..., UTF8)`, or use `docker exec` + Python.

### Key settings (`.env` / `app/config.py`)

| Setting | Value | Used by |
|---|---|---|
| `OCR_ENGINE` | `gemini` | extractor (tesseract fallback) |
| `OCR_DPI` | 200 | page render |
| `LOG_LEVEL` | DEBUG | worker/fastapi |
| `DEFAULT_TENANT_ID` | `default` | tenant scoping |
| `GEMINI_EMBEDDING_MODEL` | `gemini-embedding-001` | embedder |
| `EMBED_BATCH_SIZE` | 10 | embedder batching |
| `QDRANT_COLLECTION` | `documents` | indexer/retriever |
| `MEILISEARCH_INDEX` | `documents` | indexer/retriever |
| `VECTOR_SIZE` | 3072 | indexer/retriever |
| `TARGET_MAX` (chunker) | 420 tokens | chunker |
| `OVERLAP_FRACTION` | 0.15 | chunker |
| `HARD_CEILING` | 900 tokens | chunker |
| `RAG_MIN_CONFIDENCE_SCORE` | 0.30 | quality gate |
| `RAG_VECTOR_MIN_CONFIDENCE` | 0.65 | quality gate |
| top_k (vector / keyword) | 20 / 20 | retriever |
| top_n after rerank | 8 (6 used) | reranker → prompt |
| RRF constant k | 60 | reranker |
| overlap weight | 0.5 | reranker |
| entity boost cap | 0.15 | reranker |

---

## End-to-end flow diagram

```
UPLOAD (API)                 WORKER (Celery)                    STORES
┌─────────────┐   PDF   ┌───────────────────────┐   ┌────────────┐
│ admin/books │ ──────► │ MinIO (original.pdf)  │   │ Postgres   │ books/jobs
│   /upload   │         │ 1. OCR (Gemini+Tess)  │   ├────────────┤
└─────────────┘         │ 2. Page# validation   │   │ Qdrant     │ 6 vectors × 3072d
   enqueue task         │ 3. Chunk (6 chunks)   │   ├────────────┤
   (Redis broker)       │ 4. Embed (6 × 3072d)  │   │ Meilisearch│ 6 docs keyword
                        │ 5. Index Qdrant+Meili │   │ Redis      │ jobs/checkpoints
                        └───────────────────────┘   └────────────┘

ASK (API)                          RAG GRAPH (LangGraph)
┌──────────┐  question  ┌──────────────────────────────────────────────┐
│  /ask    │ ─────────► │ retrieve: detect lang → translate → embed q  │
└──────────┘            │   → hybrid search (Qdrant + Meilisearch)      │
                        │   → rerank (RRF + overlap + entity) → top 8   │
                        │        │                                      │
                        │   quality_gate: best≥0.30 OR vector≥0.65      │
                        │        │                              ┌───────┴────┐
                        │   ┌────┴────┐   retry once      ┌────► no_result  │
                        │   │ generate│ ──────────────────►│   (refusal)    │
                        │   └────┬────┘   (rephrase q)     └───────────────┘
                        │   citations → consistency → resolve → language   │
                        │   answer + sources (highlight URLs)              │
                        └──────────────────────────────────────────────────┘
```

## Where the trace lives in code

| Stage | File | Key functions |
|---|---|---|
| Upload/queue | `app/api/books.py:76`, `workers/celery_app.py:110` | `upload_book`, `process_book` |
| OCR | `app/pipeline/extractor.py` | `extract`, `_extract_gemini`, `_extract_tesseract` |
| Page numbers | `app/pipeline/page_number_validator.py` | `validate_ingestion_page_numbers` |
| Chunking | `app/pipeline/chunker.py` | `chunk`, `_detect_strategy`, `_group_paragraphs` |
| Embedding | `app/core/embedder.py` | `embed_texts`, `embed_query` |
| Indexing | `app/pipeline/indexer.py` | `index_to_qdrant`, `index_to_meilisearch` |
| Retrieval | `app/rag/retriever.py` | `vector_search`, `keyword_search` |
| Rerank | `app/rag/reranker.py` | `rerank`, `_term_overlap`, `_entity_boost` |
| Agent graph | `app/rag/agent.py` | `retrieve_node`, `quality_check_node`, `generate_node` |
| Ask API | `app/api/ask.py` | `ask_json`, `ask_stream`, `_build_source` |

## How to watch it live

```bash
docker logs -f al-mawsuat-backend-worker-1   | grep TRACE   # ingestion
docker logs -f al-mawsuat-backend-fastapi-1  | grep TRACE   # retrieval/answer
```
