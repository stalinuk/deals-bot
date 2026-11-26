"""Find direct store product links for a product NAME (backend, automatic).

Uses the Brave Search API (free plan: https://brave.com/search/api/) to look up
"<product name> site:<store>" once per store, keeps product-page links whose title
really matches the product, and remembers them for a week. Needs BRAVE_API_KEY;
without it the bot just uses product_links.yaml + name search + Slickdeals.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from curl_cffi import requests

from matching import DEFAULT_EXCLUDE, keyword_patterns, title_matches

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
REFRESH_DAYS = 7
LINKS_PER_STORE = 2

# store domain -> regex for a PRODUCT page (not search/category pages)
PRODUCT_PAGE = {
    "amazon.com": re.compile(r"amazon\.com/(?:[^?#]*/)?(?:dp|gp/product)/([A-Z0-9]{10})"),
    "costco.com": re.compile(r"costco\.com/(?:[^?#]*\.product\.\d+\.html|p/[^?#]*/\d+)"),
    "bestbuy.com": re.compile(r"bestbuy\.com/(?:site/[^?#]*\.p\?skuId=\d+|product/[^?#]+)"),
    "homedepot.com": re.compile(r"homedepot\.com/p/[^?#]+/\d+"),
    "lowes.com": re.compile(r"lowes\.com/pd/[^?#]+/\d+"),
    "walgreens.com": re.compile(r"walgreens\.com/store/c/[^?#]*ID=\d+-product"),
    "walmart.com": re.compile(r"walmart\.com/ip/[^?#]*\d+"),
    "target.com": re.compile(r"target\.com/p/[^?#]*/A-\d+"),
}
DISCOVER_STORES = ["amazon.com", "costco.com", "bestbuy.com", "homedepot.com", "lowes.com", "walgreens.com"]


def load_seed_links(path: Path) -> dict[str, list]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k).strip().lower(): (v or []) for k, v in data.items()}


def _normalize(domain: str, url: str) -> str:
    if domain == "amazon.com":
        m = PRODUCT_PAGE[domain].search(url)
        return f"https://www.amazon.com/dp/{m.group(1)}"
    return url.split("#")[0]


def _brave(query: str, key: str) -> list[dict]:
    resp = requests.get(
        BRAVE_URL,
        params={"q": query, "count": 10, "country": "us"},
        headers={"Accept": "application/json", "X-Subscription-Token": key},
        timeout=20,
    )
    if resp.status_code == 429:
        time.sleep(2)
        resp = requests.get(BRAVE_URL, params={"q": query, "count": 10, "country": "us"},
                            headers={"Accept": "application/json", "X-Subscription-Token": key}, timeout=20)
    resp.raise_for_status()
    return ((resp.json().get("web") or {}).get("results")) or []


def discover(product: dict, key: str) -> list[str]:
    name = product.get("search") or product["name"]
    keywords = keyword_patterns(product.get("keywords") or name)
    exclude = product.get("exclude") or DEFAULT_EXCLUDE
    found: list[str] = []
    for domain in DISCOVER_STORES:
        try:
            results = _brave(f"{name} site:{domain}", key)
        except Exception as exc:
            print(f"  [links] {domain}: search failed ({exc})")
            continue
        picked = 0
        for r in results:
            url, title = r.get("url") or "", r.get("title") or ""
            if picked >= LINKS_PER_STORE or not PRODUCT_PAGE[domain].search(url):
                continue
            if not title_matches(title, keywords, exclude):
                continue
            link = _normalize(domain, url)
            if link not in found:
                found.append(link)
                picked += 1
        time.sleep(1.1)  # free plan: 1 request / second
    return found


def discovered_links(product: dict, state: dict) -> list[str]:
    """Links found automatically (cached in state for REFRESH_DAYS)."""
    cache = state.setdefault("links", {})
    entry = cache.get(product["name"])
    key = os.environ.get("BRAVE_API_KEY")
    fresh = False
    if entry:
        age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(entry["found_at"])).days
        fresh = age_days < REFRESH_DAYS
    if key and not fresh:
        print(f"  [links] looking up store links for {product['name']}...")
        urls = discover(product, key)
        entry = {"found_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "urls": urls}
        cache[product["name"]] = entry
        print(f"  [links] found {len(urls)} link(s)")
    return list((entry or {}).get("urls", []))
