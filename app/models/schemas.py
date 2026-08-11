from pydantic import BaseModel


class SourceItem(BaseModel):
    rank: int
    book_id: str
    book_name: str
    author: str | None = None
    book_type: str | None = None
    chapter: str | None = None
    page: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    page_offsets: list[dict] | None = None
    relevance_score: float
    text: str | None = None
    bbox: list[float] | None = None
    page_bboxes: list[dict] | None = None
    minio_path: str | None = None
    highlight_url: str | None = None


class AnswerResponse(BaseModel):
    question: str
    answer: str
    was_cached: bool = False
    no_result: bool = False
    sources: list[SourceItem] = []
