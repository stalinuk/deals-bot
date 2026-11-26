"""Watch Slickdeals for new deal posts about a product (via its public RSS search feed).

Slickdeals is not a store, so instead of reading one price it looks for *new deal
posts* whose title matches the product and mentions a price at or below the target.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import quote_plus

from matching import DEFAULT_EXCLUDE, keyword_patterns, title_matches
from scrapers import fetch, parse_price

FEED = (
    "https://slickdeals.net/newsearch.php?src=SearchBarV2&q={q}"
    "&searcharea=deals&searchin=first&rss=1"
)


@dataclass
class SlickDeal:
    id: str
    title: str
    link: str
    price: float | None
    posted: datetime | None


NOT_A_PRICE_AFTER = re.compile(
    r"^\s*(off|in\b|coupon|gift|credit|back|cash|reward|bonus|statement|store credit|promo|sc\b|gc\b)",
    re.I,
)
NOT_A_PRICE_BEFORE = re.compile(r"(save|get|extra|earn|plus|\+)\s*$", re.I)


def _price_in_title(title: str) -> float | None:
    """First dollar amount that is the deal price, e.g. "AirPods Pro 3 $129.99 at Amazon".

    Skips amounts like "$100 off", "+ $100 in store coupon", "$25 gift card".
    """
    for match in re.finditer(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)", title):
        after = title[match.end():match.end() + 20]
        before = title[max(0, match.start() - 8):match.start()]
        if NOT_A_PRICE_AFTER.search(after) or NOT_A_PRICE_BEFORE.search(before):
            continue
        return parse_price(match.group(1))
    return None


def search(query: str) -> list[SlickDeal]:
    xml = fetch(FEED.format(q=quote_plus(query)))
    root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    deals = []
    for item in root.iter("item"):
        title = unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        posted = None
        if item.findtext("pubDate"):
            try:
                posted = parsedate_to_datetime(item.findtext("pubDate"))
            except Exception:
                posted = None
        deals.append(SlickDeal(guid, title, link, _price_in_title(title), posted))
    return deals


def matching_deals(cfg: dict, product: dict) -> list[SlickDeal]:
    """Deal posts that match the product's keywords and are at/below target."""
    query = cfg.get("query") or product.get("search") or product["name"]
    keywords = keyword_patterns(cfg.get("keywords") or product.get("keywords") or query)
    exclude = [w.lower() for w in cfg.get("exclude") or product.get("exclude") or DEFAULT_EXCLUDE]
    max_age_h = float(cfg.get("max_age_hours", 72))
    target = float(product["target_price"])
    min_valid = product.get("min_valid_price")

    result = []
    for deal in search(query):
        t = deal.title.lower()
        if not title_matches(t, keywords, exclude):
            continue
        if deal.price is None or deal.price > target:
            continue
        if min_valid is not None and deal.price < float(min_valid):
            continue
        if deal.posted and (datetime.now(timezone.utc) - deal.posted).total_seconds() > max_age_h * 3600:
            continue
        result.append(deal)
    return result
