# TASK-04 — Database Models and Tables

**Feature:** PostgreSQL schema  
**Repo:** al-mawsuat-backend  
**Week:** 1  
**Depends on:** TASK-03

---

## Description

Write the SQLAlchemy async database setup and all table definitions. Also configure Alembic for migrations. Every table must have a `tenant_id` column — even in Phase 1 where it is always `settings.DEFAULT_TENANT_ID`. This makes the SaaS migration later a small additive change.

**File: `app/models/db.py`**

Set up the async SQLAlchemy engine and session factory using `DATABASE_URL` from settings. Export:
- `engine` — the async engine
- `AsyncSessionLocal` — the session factory
- `Base` — the declarative base for all models
- `get_db` — an async dependency function that yields a session and closes it after the request

**File: `app/models/tables.py`**

Define four ORM models using `Base`:

`Book` table — columns:
- `id`: UUID primary key, auto-generated
- `tenant_id`: String, not null, default `settings.DEFAULT_TENANT_ID`, indexed
- `title`: String 500, not null
- `author`: String 300, nullable
- `language`: String 10, not null (values: ar, ur, en)
- `book_type`: String 50, nullable (values: tafsir, hadith, fiqh, fatwa, other)
- `total_pages`: Integer, nullable
- `total_chunks`: Integer, default 0
- `minio_path`: String 1000, nullable
- `status`: String 20, default "pending" (pending, processing, ready, failed)
- `uploaded_by`: UUID, foreign key to users.id, nullable
- `created_at`: DateTime with timezone, default now

`ProcessingJob` table — columns:
- `id`: UUID primary key, auto-generated
- `book_id`: UUID, foreign key to books.id, cascade delete, not null
- `tenant_id`: String, not null, default `settings.DEFAULT_TENANT_ID`
- `status`: String 20, default "queued" (queued, extracting, chunking, embedding, indexing, completed, failed)
- `progress_pct`: Integer, default 0
- `current_step`: String 100, nullable
- `error_msg`: Text, nullable
- `started_at`: DateTime with timezone, nullable
- `finished_at`: DateTime with timezone, nullable
- `created_at`: DateTime with timezone, default now

`User` table — columns:
- `id`: UUID primary key, auto-generated
- `tenant_id`: String, not null, default `settings.DEFAULT_TENANT_ID`, indexed
- `email`: String 300, unique, not null
- `hashed_pw`: String 500, not null
- `role`: String 20, default "user" (user, admin, superadmin)
- `is_active`: Boolean, default True
- `created_at`: DateTime with timezone, default now

`RefreshToken` table — columns:
- `id`: UUID primary key, auto-generated
- `user_id`: UUID, foreign key to users.id, cascade delete, not null
- `token_hash`: String 500, not null
- `expires_at`: DateTime with timezone, not null
- `created_at`: DateTime with timezone, default now

**Alembic setup:**

Run `alembic init alembic` in the repo root. Configure `alembic/env.py` to use the async engine and import `Base` from `app.models.tables`. Generate and apply the first migration.

---

## Acceptance criteria

- [ ] `alembic upgrade head` runs without error
- [ ] All four tables exist in PostgreSQL: `books`, `processing_jobs`, `users`, `refresh_tokens`
- [ ] Every table has a `tenant_id` column
- [ ] `books` and `users` tables have an index on `tenant_id`
- [ ] `get_db` dependency can be used in a FastAPI route without error
- [ ] Dropping and recreating via `alembic downgrade base && alembic upgrade head` works cleanly
