# TASK-18 — Chat Interface Components

**Feature:** Main user-facing question and answer UI  
**Repo:** al-mawsuat-frontend  
**Week:** 7  
**Depends on:** TASK-17

---

## Description

Build the four components that make up the main chat interface and assemble them into the main page. The interface allows a user to type a question, see the answer stream in word by word, and click source cards to view the highlighted passage in the original kitab.

**File: `components/ChatInput.tsx`**

A text input area with a submit button. Props: `onSubmit: (question: string) => void`, `disabled: boolean`. When the user presses Enter (without Shift) or clicks the button, call `onSubmit` with the trimmed text. Clear the input after submission. Show a subtle loading indicator when `disabled` is true. The input must support Arabic/Urdu right-to-left text — use `dir="auto"` on the textarea so direction adjusts automatically based on what the user types.

**File: `components/AnswerCard.tsx`**

Displays the streaming answer. Props: `tokens: string`, `done: boolean`, `noResult: boolean`. Renders the accumulated token string. Detects if the text is Arabic/Urdu by checking if it contains Arabic Unicode characters (range `\u0600-\u06FF`) — if so, wrap in `<div dir="rtl" className="text-right font-arabic">`, otherwise `<div dir="ltr">`. Show a blinking cursor at the end while `done` is false. When `noResult` is true, show the message in a distinct style (e.g. muted colour, italic).

**File: `components/SourceCard.tsx`**

Displays a single source reference. Props: `source: SourceItem`, `onView: (source: SourceItem) => void`, `active: boolean`. Shows: book name (bold), author, page number, chapter (if available), relevance score as a percentage, and the first 80 characters of `original_text`. Has a "View in kitab →" button that calls `onView`. Highlight the card border when `active` is true (the KitabViewer is showing this source).

**File: `components/KitabViewer.tsx`**

Shows the highlighted PDF page. Props: `source: SourceItem | null`, `onClose: () => void`. When `source` is null, renders nothing. When `source` is provided: calls `GET /highlight` by constructing the URL from `source.highlight_url` and prefixing with `NEXT_PUBLIC_API_URL`. Renders the returned PNG as an `<img>` tag. Shows prev/next page buttons that call the highlight endpoint with `page ± 1` while keeping the same `book_id` and `bbox`. Shows a close button that calls `onClose`. While the image is loading, show a skeleton placeholder.

**File: `app/page.tsx`**

Assemble all components:
- `ChatInput` at the top
- `AnswerCard` below it (hidden until first question is asked)
- Row of `SourceCard` components (hidden until sources arrive)
- `KitabViewer` panel (hidden until a source card is clicked)

State: `question`, `activeSource`, wire `useAskStream` hook to the question state.

---

## Acceptance criteria

- [ ] Typing a question and pressing Enter triggers the ask stream
- [ ] Answer tokens appear word by word in real time
- [ ] Arabic answer text renders right-to-left
- [ ] English answer text renders left-to-right
- [ ] Source cards appear after streaming completes
- [ ] Clicking a source card opens KitabViewer and highlights that card's border
- [ ] KitabViewer shows the PDF page image with yellow highlight visible
- [ ] Prev/next buttons in KitabViewer load adjacent pages
- [ ] Close button hides the KitabViewer and deactivates the source card
- [ ] While waiting for the answer, the ChatInput submit button is disabled
