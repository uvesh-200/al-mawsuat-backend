# TASK-20 — Production Hardening

**Feature:** Rate limiting, monitoring, health checks, SSL  
**Repo:** al-mawsuat-backend  
**Week:** 8  
**Depends on:** All previous tasks

---

## Description

Prepare the system for real production use. This task adds the safety, observability, and reliability layers that make the system trustworthy under real load.

**Sentry error tracking**

Add `sentry_sdk.init()` in `app/main.py` using `settings.SENTRY_DSN`. Use `sentry_sdk.integrations.fastapi.FastApiIntegration()` and `sentry_sdk.integrations.sqlalchemy.SqlalchemyIntegration()`. Set `traces_sample_rate=0.1` (10% of requests traced, enough for performance insight without cost). After setup, any unhandled Python exception in a request will appear in Sentry with full stack trace, request details, and user context.

**Nginx rate limiting**

Update `infra/nginx/nginx.conf` to add rate limit zones and apply them per endpoint:
- `/ask` and `/ask/stream`: 30 requests per minute per IP
- `/auth/login`: 10 requests per minute per IP
- `/admin/books/upload`: 5 requests per minute per IP
- All other endpoints: 120 requests per minute per IP

Return HTTP 429 with a JSON body `{"detail": "Rate limit exceeded"}` when limit is hit.

**Health check endpoint**

Update `GET /health` in `app/main.py` to actively check all dependencies:
- Execute `SELECT 1` on PostgreSQL
- Call `qdrant.get_collection("documents")`
- Call `redis_client.ping()`
- Return `{"status": "ok", "checks": {"postgres": "ok", "qdrant": "ok", "redis": "ok"}}`
- If any check fails, return HTTP 503 with `{"status": "degraded", "checks": {...}}`

**Structured logging**

Replace all `print()` and basic logging with structured JSON logging using Python's `logging` module configured to output JSON. Every log entry must include: `timestamp`, `level`, `endpoint`, `tenant_id`, `duration_ms`, `status_code`. Use FastAPI middleware to measure request duration and log on every response.

**SSL setup on Oracle Cloud VM**

On the production server:
- Install Certbot: `sudo apt install certbot python3-certbot-nginx`
- Obtain certificate: `sudo certbot --nginx -d yourdomain.com`
- Certbot auto-renews via systemd timer — verify with `sudo certbot renew --dry-run`
- Update Nginx config to redirect HTTP to HTTPS

**PgBouncer connection pooling**

Add a `pgbouncer` Docker service in front of PostgreSQL. Configure with `pool_mode=transaction`, `max_client_conn=1000`, `default_pool_size=20`. Update `DATABASE_URL` in `.env` to point to `pgbouncer:5432` instead of `postgres:5432` directly.

---

## Acceptance criteria

- [ ] Triggering an intentional error in a route causes it to appear in Sentry within 30 seconds
- [ ] `curl -X POST http://localhost/auth/login` 11 times rapidly — 11th returns HTTP 429
- [ ] `curl -X POST http://localhost/ask` 31 times rapidly — 31st returns HTTP 429
- [ ] `GET /health` returns `{"status": "ok"}` when all services are running
- [ ] `GET /health` returns HTTP 503 when PostgreSQL is stopped (verify by stopping postgres container)
- [ ] `docker logs fastapi` shows JSON-formatted log entries with `duration_ms` field
- [ ] `https://yourdomain.com` loads with valid SSL certificate
- [ ] `http://yourdomain.com` redirects to `https://`
- [ ] PgBouncer is in the connection path: `DATABASE_URL` points to pgbouncer, pgbouncer forwards to postgres
