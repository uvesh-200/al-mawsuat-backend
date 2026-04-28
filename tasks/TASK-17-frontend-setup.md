# TASK-17 — Frontend Project Setup

**Feature:** Next.js project scaffold and API client  
**Repo:** al-mawsuat-frontend  
**Week:** 7  
**Depends on:** TASK-15 (backend must be running for API calls to work)

---

## Description

Create and configure the Next.js 14 frontend project. This task is only setup and infrastructure — no UI components yet. After this task the project runs, connects to the backend, and has the correct folder structure.

**Project creation**

Use `pnpm create next-app@14` with: TypeScript enabled, Tailwind CSS enabled, App Router enabled, no `src/` directory, import alias `@/*`.

**Required packages to install:**
- `react-pdf@7` — render PDF pages in the browser
- `swr` — data fetching with caching and polling
- `zustand` — lightweight client state

**Folder structure to create:**
```
al-mawsuat-frontend/
├── app/
│   ├── layout.tsx       ← root layout, sets font and base styles
│   ├── page.tsx         ← main chat page (empty for now)
│   └── admin/
│       └── page.tsx     ← admin panel (empty for now)
├── components/          ← empty folder, components added in later tasks
├── lib/
│   ├── api.ts           ← API client
│   └── sse.ts           ← SSE streaming hook
├── .env.local           ← NEXT_PUBLIC_API_URL=http://localhost:8000
└── .gitignore           ← includes .env.local and .next/
```

**File: `lib/api.ts`**

Write a typed API client with:
- A base `request` function that reads `NEXT_PUBLIC_API_URL` from env, attaches the JWT from memory (not localStorage) to `Authorization: Bearer` header, and handles 401 by attempting a token refresh then retrying once
- `login(email, password)` — calls `POST /auth/login`, stores returned token in a module-level variable (memory only)
- `logout()` — calls `POST /auth/logout`, clears the in-memory token
- `ask(question)` — calls `POST /ask`, returns `AnswerResponse`
- `getBooks()` — calls `GET /admin/books`
- `uploadBook(formData)` — calls `POST /admin/books/upload`
- `deleteBook(id)` — calls `DELETE /admin/books/{id}`
- `getJob(id)` — calls `GET /admin/jobs/{id}`

**File: `lib/sse.ts`**

Write a React hook `useAskStream(question: string | null)` that:
- Returns `{ tokens: string, sources: SourceItem[], done: boolean, error: string | null }`
- When `question` is not null, opens an `EventSource` to `GET /ask/stream?question=...` with the JWT in the URL or a custom header
- Accumulates token events into `tokens` string
- Sets `sources` when a sources event arrives
- Sets `done: true` when done event arrives
- Closes the EventSource on component unmount

**RTL configuration in `app/layout.tsx`**

The root layout must NOT set a global `dir` — direction is set per-element based on detected language. Import the Noto Naskh Arabic font from Google Fonts for Arabic text rendering.

---

## Acceptance criteria

- [ ] `pnpm dev` starts without errors and `http://localhost:3000` loads a blank page
- [ ] `pnpm build` completes without TypeScript errors
- [ ] `lib/api.ts` exports all listed functions with correct TypeScript types
- [ ] `lib/sse.ts` exports `useAskStream` hook
- [ ] `.env.local` is in `.gitignore` and not committed
- [ ] `api.login()` called from browser console successfully logs in and stores the token in memory
- [ ] `api.getBooks()` called after login returns the books list from the backend
