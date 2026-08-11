"""API response shapes."""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class ReviewOut(BaseModel):
    rating: float | None
    review_date: date | None
    reviewer_name: str | None
    comment: str | None


class MetaOut(BaseModel):
    """Diagnostics. Outside the required contract, but this is what makes the system operable."""
    source: str
    pages_fetched: int
    avg_ms_per_page: int | None = None
    stopped_early: bool = False
    profile_id: str | None = None
    session_minted: bool = False
    profile_attempts: int = 1


class AggregateResponse(BaseModel):
    # Total the source itself advertises. Differs from review_aggregated_count whenever filter_date
    # is applied — conflating them would make a filtered response look like data loss.
    review_count: int
    aggregated_reviews: list[ReviewOut]
    review_aggregated_count: int
    response_code: int = 200
    meta: MetaOut | None = None


class ErrorBody(BaseModel):
    message: str
    code: str


class ErrorResponse(BaseModel):
    response_code: int
    error: ErrorBody
