"""G2 parsing — pure functions, no I/O, so it can be tested against saved fixtures with no network."""
from __future__ import annotations

import json
import re
from datetime import date, datetime

from bs4 import BeautifulSoup

from ..base import PageResult, Review

CARD_SELECTOR = 'article[id*="review-"]'

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DECIMAL = re.compile(r"^\d(?:\.\d)?$")
US_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
BOILERPLATE = re.compile(r"Review collected by and hosted on G2\.com\.?")
QUOTED_TITLE = re.compile(r"[“\"]([^”\"]{5,200})[”\"]")
TRAILING_ID = re.compile(r"(\d+)\s*$")


def parse_reviews_page(html: str, page: int = 1) -> PageResult:
    soup = BeautifulSoup(html, "lxml")
    reviews: list[Review] = []

    for card in soup.select(CARD_SELECTOR):
        text = " ".join(card.get_text(" ").split())
        meta = _from_meta_by_shape(card)
        fallback = _from_visible_text(card, text)

        title_match = QUOTED_TITLE.search(text)
        title = title_match.group(1).strip() if title_match else None

        sections = [
            " ".join(BOILERPLATE.sub(" ", section.get_text(" ")).split())
            for section in card.find_all("section")
        ]
        body = "\n\n".join(s for s in sections if s) or BOILERPLATE.sub(" ", text).strip()
        comment = " — ".join(p for p in (title, body) if p) or None

        id_match = TRAILING_ID.search(card.get("id") or "")

        reviews.append(Review(
            rating=meta["rating"] if meta["rating"] is not None else fallback["rating"],
            review_date=meta["date"] or fallback["date"],
            reviewer_name=meta["author"] or fallback["author"],
            comment=comment,
            review_id=id_match.group(1) if id_match else None,
        ))

    return PageResult(
        reviews=reviews,
        has_more=_has_next_page(soup, page),
        total_count=_total_count(soup),
    )


def _from_meta_by_shape(card) -> dict:
    """Match the bare <meta content> values by shape rather than position."""
    out: dict = {"rating": None, "author": None, "date": None}
    for meta in card.find_all("meta"):
        value = (meta.get("content") or "").strip()
        if not value:
            continue
        if out["date"] is None and ISO_DATE.match(value):
            out["date"] = _parse_iso(value)
        elif DECIMAL.match(value):
            # bestRating (5) and worstRating (0) are decimals too; the real rating is the one
            # written
            # with a decimal point. Integers are the scale bounds.
            if "." in value:
                out["rating"] = float(value)
        elif out["author"] is None and re.search(r"[A-Za-z]", value) and len(value) <= 80:
            out["author"] = value
    return out


def _from_visible_text(card, text: str) -> dict:
    out: dict = {"rating": None, "author": None, "date": None}

    m = US_DATE.search(text)
    if m:
        out["date"] = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))

    m = re.search(r"(\d(?:\.\d)?)\s*/\s*5", text)
    if m:
        out["rating"] = float(m.group(1))

    link = card.select_one('a[href*="/users/"]')
    if link:
        name = " ".join(link.get_text().split())
        if name and len(name) <= 80:
            out["author"] = name

    return out


def _parse_iso(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _total_count(soup) -> int | None:
    """Authoritative total from JSON-LD; falls back to prose."""
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            parsed = json.loads(script.get_text() or "{}")
        except json.JSONDecodeError:
            continue
        for node in parsed if isinstance(parsed, list) else [parsed]:
            if not isinstance(node, dict):
                continue
            for holder in (node.get("mainEntity") or {}, node):
                agg = holder.get("aggregateRating") if isinstance(holder, dict) else None
                if isinstance(agg, dict) and isinstance(agg.get("reviewCount"), (int, float)):
                    return int(agg["reviewCount"])

    m = re.search(r"([\d,]+)\s+reviews?", soup.get_text(" "), re.IGNORECASE)
    return int(m.group(1).replace(",", "")) if m else None


def _has_next_page(soup, current_page: int) -> bool:
    """A next page exists if the paginator links to any number above the current one."""
    for anchor in soup.select('a[href*="page="]'):
        m = re.search(r"[?&]page=(\d+)", anchor.get("href") or "")
        if m and int(m.group(1)) > current_page:
            return True
    return False
