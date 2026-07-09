"""Shared parsing for HarvestAPI LinkedIn actors (harvestapi/*).

All HarvestAPI post actors (profile-posts, company-posts, post-search) emit the
same item shape: {type, id, linkedinUrl, content, author{name, info,
publicIdentifier, linkedinUrl}, postedAt{timestamp, date}, engagement{likes,
comments, shares, reactions[]}, postImages[], repostedBy, ...}. These helpers
map that shape onto the repo's post contracts so the workflows never see a
vendor change. (Swapped from apimaestro 2026-07-09 — 60% cheaper per post.)
"""

from typing import Optional


def clean_url(url: Optional[str]) -> str:
    """Drop tracking query strings (miniProfileUrn etc.) from LinkedIn URLs."""
    return (url or "").split("?")[0]


def parse_author(author: dict) -> dict:
    return {
        "name": author.get("name") or "",
        "headline": author.get("info"),
        "username": author.get("publicIdentifier") or author.get("universalName"),
        "profile_url": clean_url(author.get("linkedinUrl")),
    }


def parse_stats(item: dict) -> dict:
    eng = item.get("engagement") or {}
    reactions = eng.get("reactions") or []
    total = sum((r.get("count") or 0) for r in reactions) or (eng.get("likes") or 0)
    return {
        "total_reactions": total,
        "likes": eng.get("likes") or 0,
        "comments": eng.get("comments") or 0,
        "reposts": eng.get("shares") or 0,
    }


def parse_media(item: dict) -> Optional[dict]:
    images = item.get("postImages") or []
    if not images:
        return None
    return {
        "type": "images",
        "url": None,
        "images": [{"url": i.get("url"), "width": i.get("width"),
                    "height": i.get("height")} for i in images],
    }


def _to_int(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_post(item: dict) -> dict:
    """HarvestAPI post item → the repo's profile/company post shape."""
    posted = item.get("postedAt") or {}
    return {
        "urn": item.get("id"),
        "url": clean_url(item.get("linkedinUrl")),
        "post_type": "repost" if item.get("repostedBy") else (item.get("type") or "post"),
        "posted_at": posted.get("date"),
        # Workflows do arithmetic on this — force int (the actor's JSON types
        # aren't guaranteed stable).
        "timestamp_ms": _to_int(posted.get("timestamp")),
        "text": item.get("content"),
        "author": parse_author(item.get("author") or {}),
        "stats": parse_stats(item),
        "media": parse_media(item),
        # Reposts are excluded server-side (includeReposts=False), so there is
        # no reshared payload to carry through.
        "reshared_post": None,
    }


def is_post_item(item: dict) -> bool:
    """True for real post items; False for no-result/error sentinel items."""
    return item.get("type") == "post" and bool(item.get("id"))
