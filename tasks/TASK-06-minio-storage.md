# TASK-06 — MinIO Storage Client

**Feature:** File storage for PDFs and highlight images  
**Repo:** al-mawsuat-backend  
**Week:** 2  
**Depends on:** TASK-03

---

## Description

Write a MinIO client wrapper at `app/storage/minio_client.py`. This is the single module through which all file storage operations happen. No other part of the codebase should import the MinIO SDK directly — they all go through this wrapper.

All file paths follow the pattern `books/{tenant_id}/{book_id}/original.pdf` for uploaded books and `highlights/{tenant_id}/{book_id}/p{page}-{bbox}.png` for rendered highlight images. The `tenant_id` in the path is always `settings.DEFAULT_TENANT_ID` in Phase 1.

The wrapper must expose these async functions:

`upload_file(bucket, path, data: bytes, content_type: str) -> str` — uploads bytes to MinIO at the given path, returns the full path.

`get_file(bucket, path) -> bytes` — downloads and returns the file as bytes. Raises a clear exception if the file does not exist.

`get_file_safe(bucket, path) -> Optional[bytes]` — same as get_file but returns `None` instead of raising if the file does not exist. Used for cache checks.

`file_exists(bucket, path) -> bool` — returns True if the file exists at the path.

`delete_file(bucket, path) -> None` — deletes the file. Does not raise if the file does not exist.

On application startup, ensure both buckets exist. Create them if they do not. Buckets: `books` and `highlights`. This can be called in `app/main.py` startup event.

Use connection settings from `app/config.py` settings object. The MinIO client should be a module-level singleton — instantiated once when the module is imported, reused on every call.

---

## Acceptance criteria

- [ ] `upload_file` uploads a file and it appears in MinIO console at the expected path
- [ ] `get_file` retrieves the same bytes that were uploaded
- [ ] `get_file_safe` returns `None` for a path that does not exist (no exception)
- [ ] `file_exists` returns `True` for existing file, `False` for non-existing
- [ ] `delete_file` removes the file; calling it again on the same path does not raise
- [ ] Both `books` and `highlights` buckets are created automatically on startup
- [ ] File path format is `books/al-mawsuat-deobandiyyah/{book_id}/original.pdf`
