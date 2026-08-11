"""HTTP boundary. Translates query params into a service call and the result into the contract."""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Query, Request

from ..core.errors import ValidationError
from ..sources.registry import supported_sources
from .schemas import AggregateResponse, MetaOut, ReviewOut

router = APIRouter()


def _parse_filter_date(raw: str | None) -> date | None:
    if raw in (None, ""):
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        raise ValidationError(f'"filter_date" is not a valid ISO date: {raw}') from None


@router.get("/reviews/aggregate", response_model=AggregateResponse)
def aggregate_reviews(
    request: Request,
    url: str = Query(..., description="Profile/reviews URL to aggregate"),
    source_name: str | None = Query(None, description=f"One of: {', '.join(supported_sources())}"),
    filter_date: str | None = Query(None, description="Only reviews with review_date >= this (YYYY-MM-DD)"),
    max_pages: int | None = Query(None, ge=1, description="Safety cap override"),
):
    service = request.app.state.service
    outcome = service.aggregate_reviews(
        url=url,
        source_name=source_name,
        filter_date=_parse_filter_date(filter_date),
        max_pages=max_pages,
    )
    result = outcome.result

    return AggregateResponse(
        review_count=result.site_total or len(result.reviews),
        aggregated_reviews=[
            ReviewOut(
                rating=r.rating,
                review_date=r.review_date,
                reviewer_name=r.reviewer_name,
                comment=r.comment,
            )
            for r in result.reviews
        ],
        review_aggregated_count=len(result.reviews),
        response_code=200,
        meta=MetaOut(
            source=outcome.source,
            pages_fetched=result.pages_fetched,
            avg_ms_per_page=result.avg_ms_per_page(),
            stopped_early=result.stopped_early,
            profile_id=outcome.profile_id,
            session_minted=outcome.minted,
            profile_attempts=outcome.attempts,
        ),
    )


@router.get("/health")
def health(request: Request):
    return {"status": "ok", "sources": supported_sources()}


@router.get("/profiles")
def profiles(request: Request):
    """Pool diagnostics: which identities exist, their health, and what is cooling down."""
    return request.app.state.service.pool_status()
