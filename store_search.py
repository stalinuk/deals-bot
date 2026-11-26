"""Find a product by NAME on store sites and return the lowest matching price.

Only stores whose search results can be read without a browser are included:
  - Walmart  (search page data)
  - Target   (Target's own product-search API used by target.com)
Amazon, Home Depot, Lowe's, Walgreens and Costco block or hide search results from
non-browser clients; for those, use a direct product link (see products.yaml) or rely
on Slickdeals.
"""
from __future__ import annotations

import json
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from matching import DEFAULT_EXCLUDE, keyword_patterns, title_matches
from scrapers import PriceResult, fetch, parse_price

TARGET_SEARCH = (
    "https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2"
    "?key=9f36aeafbe60771e321a7cc95a78140772ab3e96&channel=WEB&count=24"
    "&default_purchasability_filter=true&keyword={q}&offset=0&page=%2Fs%2F{q}"
    "&pricing_store_id=3991&visitor_id=0190000000000000000000000000000"
)


def _find_all(obj, key, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                out.append(v)
            _find_all(v, key, out)
    elif isinstance(obj, list):
        for v in obj:
            _find_all(v, key, out)
    return out


def walmart_listings(query: str) -> list[tuple[str, float, str, bool | None]]:
    html = fetch(f"https://www.walmart.com/search?q={quote_plus(query)}")
    tag = BeautifulSoup(html, "html.parser").find("script", id="__NEXT_DATA__")
    if not tag:
        raise RuntimeError("search results not found on page")
    data = json.loads(tag.string or "{}")
    listings = []
    for stack in _find_all(data, "itemStacks", []):
        for group in stack if isinstance(stack, list) else [stack]:
            for item in (group or {}).get("items", []) or []:
                if item.get("__typename") != "Product" or not item.get("name"):
                    continue
                price = parse_price(item.get("price"))
                if price is None:
                    lines = ((item.get("priceInfo") or {}).get("priceDetails") or {}).get("priceLines") or []
                    for line in lines:
                        if line.get("lineType") in ("DISCOUNTED_PRICE", "PRICE", "CURRENT_PRICE"):
                            price = parse_price((line.get("values") or [{}])[0].get("value"))
                            break
                if price is None:
                    continue
                url = "https://www.walmart.com" + (item.get("canonicalUrl") or "").split("?")[0]
                status = (item.get("availabilityStatusV2") or {}).get("value")
                stock = None if status is None else status == "IN_STOCK"
                listings.append((item["name"], price, url, stock))
    return listings


def target_listings(query: str) -> list[tuple[str, float, str, bool | None]]:
    data = fetch(TARGET_SEARCH.format(q=quote_plus(query)), json_response=True)
    products = (((data.get("data") or {}).get("search") or {}).get("products")) or []
    listings = []
    for p in products:
        item = p.get("item") or {}
        title = ((item.get("product_description") or {}).get("title") or "")
        title = BeautifulSoup(title, "html.parser").get_text()  # titles contain &#8482; etc.
        price_block = p.get("price") or {}
        price = parse_price(price_block.get("current_retail")) or parse_price(
            price_block.get("current_retail_min")
        ) or parse_price(price_block.get("formatted_current_price"))
        url = (item.get("enrichment") or {}).get("buy_url") or (
            f"https://www.target.com/p/-/A-{p.get('tcin')}" if p.get("tcin") else None
        )
        if title and price and url:
            listings.append((title, price, url, None))
    return listings


SEARCHERS = {
    "Walmart": walmart_listings,
    "Target": target_listings,
}


def search_store(store: str, product: dict) -> PriceResult:
    """Lowest price for the product at ``store``, matched by product name."""
    query = product.get("search") or product["name"]
    keywords = keyword_patterns(product.get("keywords") or query)
    exclude = product.get("exclude") or DEFAULT_EXCLUDE
    min_valid = float(product.get("min_valid_price") or 0)
    result = PriceResult(store=store, url="")
    try:
        listings = SEARCHERS[store](query)
        matches = [
            l for l in listings
            if l[1] >= min_valid and title_matches(l[0], keywords, exclude)
        ]
        if not matches:
            result.error = f"no matching listing ({len(listings)} results checked)"
            return result
        title, price, url, stock = min(matches, key=lambda l: l[1])
        result.title, result.price, result.url, result.in_stock = title, price, url, stock
    except Exception as exc:
        msg = str(exc)
        result.error = ("blocked: " if "bot" in msg.lower() or "captcha" in msg.lower() else "") + \
            f"{type(exc).__name__}: {msg[:140]}"
    return result
