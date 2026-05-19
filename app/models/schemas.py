from pydantic import BaseModel


class SourceItem(BaseModel):
    rank: int
    book_id: str
    book_name: str
    author: str | None = None
    book_type: str | None = None
    chapter: str | None = None
    page: int | None = None
    original_text: str
    relevance_score: float
    bbox: list[float] | None = None
    highlight_url: str | None = None


class AnswerResponse(BaseModel):
    question: str
    answer: str
    was_cached: bool = False
    no_result: bool = False
    sources: list[SourceItem] = []
