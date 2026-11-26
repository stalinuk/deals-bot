"""Brand-wide discount watch: alert for ANY product of a brand (e.g. Apple) that is
at least N% off, even if it isn't on your products list.

It reads new Slickdeals posts and works out the discount from the post title, e.g.
  "Apple iPad 10th Gen $174 (Reg. $349)"        -> 50% off
  "Apple Pencil 2nd Gen 55% off $58"            -> 55% off
  "Apple AirTag 4-Pack $49, Save $50"           -> 50% off
Posts that say only "up to 50% off" are skipped (no real price for one product).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import slickdeals

last_scanned = 0  # posts read by the most recent find_* call (shown in the log)
from matching import CONDITION_WORDS
from scrapers import parse_price

APPLE_PRODUCTS = (
    "iphone", "ipad", "macbook", "imac", "mac mini", "mac studio", "mac pro", "airpods",
    "airpod", "apple watch", "apple tv", "apple pencil", "airtag", "homepod",
    "magic keyboard", "magic mouse", "magic trackpad", "apple vision", "studio display",
    "pro display", "apple magsafe", "apple usb-c", "apple 20w", "apple 35w", "apple 70w",
)
NOT_PRODUCTS = (  # the fruit, not the company
    "juice", "cider", "vinegar", "sauce", "pie", "chips", "snack", "fruit", "candle",
    "apple jacks", "big apple", "applebee",
)
THIRD_PARTY = re.compile(
    r"\b(for|compatible with|fits|works with)\s+(the\s+)?(apple|iphone|ipad|airpods?|macbook|mac|airtag)\b"
)

PRICE = r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)"
LIST_PRICE = re.compile(
    r"\b(?:reg(?:ular)?|list(?: price)?|was|msrp|orig(?:inal(?:ly)?)?|retail|normally|compare at)"
    r"\.?\s*:?\s*(?:price\s*:?\s*)?" + PRICE,
    re.I,
)
SAVE_AMOUNT = re.compile(r"\bsave\s+" + PRICE, re.I)
PERCENT_OFF = re.compile(r"\b(\d{2})\s?%\s*off\b", re.I)
UP_TO = re.compile(r"\bup to\s+\d{2}\s?%", re.I)


@dataclass
class DiscountDeal:
    id: str
    title: str
    link: str
    price: float | None
    discount: int  # percent off (100 for free)
    free: bool = False


# "FREE" as the price - not "Free Shipping", "+ Free Gift Card", "Sugar-Free", ...
FREE_WORD = re.compile(r"(?<![-\w])free\b", re.I)
NOT_FREE_AFTER = re.compile(
    r"^\W*(shipping|ship|s/h|s&h|delivery|returns?|store pickup|pickup|in-store pickup|trial|"
    r"\$|gift card|gc\b|w/|with)", re.I,
)
NOT_FREE_BEFORE = re.compile(
    r"(\+|w/|with|and|&|plus|gluten|sugar|hands|tax|fat|cord|bpa|worry|duty|hassle|interest|"
    r"fee|ad|scent|fragrance|toll|lactose|dairy|cruelty|caffeine|wire|cable|rust|drip)\s*$", re.I,
)


def is_free(title: str) -> bool:
    """True when the post is a freebie: "FREE Game on Epic", "Free Sample ...", "$0"."""
    if re.search(r"\btrial\b", title, re.I):  # "Free 3-month trial" is not a freebie
        return False
    if re.search(r"\$\s?0(?:\.00)?(?![\d.])", title):
        return True
    for m in FREE_WORD.finditer(title):
        if NOT_FREE_AFTER.search(title[m.end():m.end() + 20]):
            continue
        if NOT_FREE_BEFORE.search(title[max(0, m.start() - 12):m.start()]):
            continue
        return True
    return False


def is_brand_product(title: str) -> bool:
    t = title.lower()
    if any(re.search(rf"\b{re.escape(w)}\b", t) for w in CONDITION_WORDS):
        return False
    if THIRD_PARTY.search(t) or any(re.search(rf"\b{re.escape(w)}", t) for w in NOT_PRODUCTS):
        return False
    return any(w in t for w in APPLE_PRODUCTS) or t.startswith("apple ")


def discount_percent(title: str, price: float | None) -> int | None:
    """Percent off stated in or computable from a deal title, or None."""
    best = None
    if price:
        m = LIST_PRICE.search(title)
        if m:
            reg = parse_price(m.group(1))
            if reg and reg > price:
                best = round((1 - price / reg) * 100)
        if best is None:
            m = SAVE_AMOUNT.search(title)
            if m:
                save = parse_price(m.group(1))
                if save:
                    best = round(save / (price + save) * 100)
    for m in PERCENT_OFF.finditer(title):
        if UP_TO.search(title[max(0, m.start() - 7):m.end()]):
            continue
        pct = int(m.group(1))
        best = max(best or 0, pct)
    return best


def find_deals(cfg: dict) -> list[DiscountDeal]:
    min_pct = int(cfg.get("min_discount", 50))
    max_age_h = float(cfg.get("max_age_hours", 72))
    global last_scanned
    last_scanned = 0
    seen_ids = set()
    result = []
    for query in cfg.get("queries") or ["Apple"]:
        for d in slickdeals.search(query):
            if d.id in seen_ids:
                continue
            seen_ids.add(d.id)
            last_scanned += 1
            if d.posted and (datetime.now(timezone.utc) - d.posted).total_seconds() > max_age_h * 3600:
                continue
            if not is_brand_product(d.title):
                continue
            pct = discount_percent(d.title, d.price)
            if pct is not None and pct >= min_pct:
                result.append(DiscountDeal(d.id, d.title, d.link, d.price, pct))
    return result


def find_hot_deals(cfg: dict) -> list[DiscountDeal]:
    """Any new Slickdeals post (any product) at >= min_discount% off, or FREE."""
    min_pct = int(cfg.get("min_discount", 70))
    want_free = cfg.get("include_free", True)
    max_age_h = float(cfg.get("max_age_hours", 24))
    global last_scanned
    last_scanned = 0
    seen_ids = set()
    result = []
    for url in cfg.get("feeds") or slickdeals.ALL_DEALS_FEEDS:
        for d in slickdeals.read_feed(url):
            if d.id in seen_ids or d.title in seen_ids:
                continue
            seen_ids.update((d.id, d.title))
            last_scanned += 1
            if d.posted and (datetime.now(timezone.utc) - d.posted).total_seconds() > max_age_h * 3600:
                continue
            t = d.title.lower()
            if any(re.search(rf"\b{re.escape(w)}\b", t) for w in CONDITION_WORDS):
                continue
            if want_free and is_free(d.title):
                result.append(DiscountDeal(d.id, d.title, d.link, 0.0, 100, free=True))
                continue
            pct = discount_percent(d.title, d.price)
            if pct is not None and pct >= min_pct:
                result.append(DiscountDeal(d.id, d.title, d.link, d.price, pct))
    return result
