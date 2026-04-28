# TASK-05 — Authentication (JWT + fastapi-users)

**Feature:** Login, logout, token refresh  
**Repo:** al-mawsuat-backend  
**Week:** 2  
**Depends on:** TASK-04

---

## Description

Implement the complete authentication system using fastapi-users. Every JWT must contain `tenant_id` and `role` in the payload — not just the user ID. This is what allows the rest of the system to scope all data operations to the correct tenant without extra database lookups.

**File: `app/core/auth.py`**

Configure fastapi-users with:
- SQLAlchemy async user database adapter using the `User` model from TASK-04
- Bcrypt password hashing
- JWT strategy with 30-minute access token lifetime
- Custom JWT payload that adds `tenant_id` and `role` fields alongside the default `sub` field

The access token payload must look like:
```json
{
  "sub": "user-uuid",
  "tenant_id": "al-mawsuat-deobandiyyah",
  "role": "admin",
  "exp": 1234567890
}
```

Export a `get_current_user` dependency that decodes the JWT and returns the user object. Any route using `Depends(get_current_user)` is protected.

**File: `app/core/rate_limit.py`**

Implement a Redis-based login rate limiter. After 5 failed login attempts from the same IP address within 15 minutes, return HTTP 429. Use the Redis client from settings. Key format: `rate:login:{ip_address}`.

**File: `app/api/auth.py`**

Three endpoints:

`POST /auth/login` — accepts `{ email, password }`. Validates credentials against the database scoped to `settings.DEFAULT_TENANT_ID`. On success: returns access token in response body + sets refresh token as HttpOnly cookie (never in response body). On failure: increments rate limit counter.

`POST /auth/refresh` — reads refresh token from HttpOnly cookie. Validates it exists in `refresh_tokens` table and is not expired. Returns new access token in response body.

`POST /auth/logout` — deletes the refresh token record from `refresh_tokens` table. Clears the HttpOnly cookie.

Create the first superadmin user as a database seed script at `app/core/seed.py`. Run it once manually. Do not create users through a public registration endpoint in Phase 1.

---

## Acceptance criteria

- [ ] `POST /auth/login` with correct credentials returns a JWT access token
- [ ] Decoding the JWT reveals `sub`, `tenant_id`, `role`, and `exp` fields
- [ ] `tenant_id` in the token equals `settings.DEFAULT_TENANT_ID`
- [ ] `POST /auth/login` with wrong password returns HTTP 401
- [ ] 6th wrong login attempt from same IP returns HTTP 429
- [ ] `GET /admin/books` (or any protected route) without token returns HTTP 401
- [ ] `GET /admin/books` with valid token returns HTTP 200
- [ ] `POST /auth/refresh` with valid cookie returns a new access token
- [ ] `POST /auth/logout` clears the cookie and invalidates the refresh token
- [ ] Refresh token is stored hashed in the database, never as plaintext
- [ ] Access token is never stored in a cookie — response body only
