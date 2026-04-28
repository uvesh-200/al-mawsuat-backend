# TASK-19 — Admin Panel

**Feature:** Book management interface for admins  
**Repo:** al-mawsuat-frontend  
**Week:** 7  
**Depends on:** TASK-17, TASK-11

---

## Description

Build the admin panel at `/admin`. This page is only accessible after login. It allows admins to upload new kitab, monitor processing progress, view all indexed books, and delete books.

The page has three sections:

**Upload section**

A form with these fields:
- Title (text input, required)
- Author (text input, optional)
- Language (dropdown: Arabic, Urdu, English)
- Book type (dropdown: Tafsir, Hadith, Fiqh, Fatwa, Other)
- File picker (PDF only, `accept=".pdf"`)
- Submit button labelled "Upload Kitab"

On submit: call `api.uploadBook(formData)`. After a successful response, add the new job to the active jobs list and start polling it.

**Active jobs section**

Show in-progress and recently completed processing jobs. For each active job, show:
- Book title
- Current step (extracting / chunking / embedding / indexing)
- Progress bar showing `progress_pct` percent
- Status badge with colour: queued=grey, processing=blue, completed=green, failed=red

Poll each active job every 3 seconds using SWR's `refreshInterval`. Stop polling when status is `completed` or `failed`.

When a job fails, show the `error_msg` in red below the progress bar.

**Books table section**

A table showing all indexed books. Columns: Title, Author, Language, Type, Chunks, Status, Uploaded date, Actions.

The delete action (trash icon button) must show a confirmation prompt before calling `api.deleteBook(id)`. After deletion, remove the row from the table.

**Login gate**

The `/admin` page must check if the user is logged in. If not, show a login form (email + password). On successful login via `api.login()`, show the admin panel content. No redirect — keep everything on the same page.

---

## Acceptance criteria

- [ ] Visiting `/admin` while not logged in shows a login form
- [ ] Logging in with correct admin credentials shows the admin panel
- [ ] Uploading a PDF file triggers the pipeline and shows the job in the active jobs section
- [ ] Progress bar advances as the job progresses (queued → extracting → chunking → embedding → indexing → completed)
- [ ] Completed job moves from active jobs to the books table
- [ ] Books table shows correct chunk count and status for each book
- [ ] Clicking delete shows a confirmation and removes the book from the table
- [ ] Uploading a non-PDF file shows a client-side validation error before submission
- [ ] A failed job shows the error message in red
