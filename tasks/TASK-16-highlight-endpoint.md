# TASK-16 — Source Highlighting Endpoint

**Feature:** Render PDF page with passage highlighted in yellow  
**Repo:** al-mawsuat-backend  
**Week:** 6  
**Depends on:** TASK-06, TASK-15

---

## Description

Write `app/api/highlight.py`. This endpoint receives a book ID, page number, and bounding box coordinates, then returns a PNG image of that PDF page with a yellow semi-transparent rectangle drawn over the specified passage. This is what the frontend shows when a user clicks "View in kitab".

**`GET /highlight`**

Query parameters:
- `book_id: str` — UUID of the book
- `page: int` — page number (1-indexed)
- `bbox: str` — comma-separated coordinates: `"x0,y0,x1,y1"`

Steps:

1. **Security check** — query the database for the book. Verify `book.tenant_id == settings.DEFAULT_TENANT_ID`. If the book does not exist or tenant does not match, return HTTP 403. This prevents one tenant from accessing another's documents.

2. **Cache check** — check MinIO for a pre-rendered PNG at path `highlights/{tenant_id}/{book_id}/p{page}-{bbox}.png`. If it exists, return it directly as `image/png` response. Skip all rendering.

3. **Render** — fetch the original PDF from MinIO at `book.minio_path`. Open with PyMuPDF (`fitz.open(stream=pdf_bytes, filetype="pdf")`). Access the page at index `page - 1`. Parse bbox string into four floats. Create `fitz.Rect(x0, y0, x1, y1)`. Draw a filled rectangle with yellow colour `(1, 0.85, 0)` and `fill_opacity=0.45`. Render the page to PNG using `page.get_pixmap(dpi=150).tobytes("png")`.

4. **Cache rendered PNG** — upload the PNG to MinIO at the highlights path so future requests skip rendering entirely.

5. **Return** — `Response(content=img_bytes, media_type="image/png")`

This endpoint does NOT require authentication. The highlight URL is included in the API response and the frontend calls it directly. Security is enforced by the `book_id` + `tenant_id` check — a valid book_id from one tenant cannot retrieve another tenant's book because the tenant check fails.

---

## Acceptance criteria

- [ ] `GET /highlight?book_id={id}&page=1&bbox=100,200,500,300` returns a PNG image
- [ ] Opening the PNG shows a yellow semi-transparent rectangle at approximately the correct position on the page
- [ ] Calling the same URL a second time returns in under 100ms (MinIO cache hit — no re-rendering)
- [ ] A `book_id` that does not belong to the current tenant returns HTTP 403
- [ ] A `page` number beyond the total pages of the book returns HTTP 400 with a clear message
- [ ] A malformed `bbox` (e.g. `"abc,def"`) returns HTTP 422
- [ ] The rendered PNG is stored in MinIO at `highlights/al-mawsuat-deobandiyyah/{book_id}/p{page}-{bbox}.png`
