"""Price scrapers for retail product pages.

Strategy for every page (first hit wins):
  1. A custom CSS ``selector`` from products.yaml (if you gave one)
  2. Store-specific logic (Amazon, Walmart, Target, Best Buy ...)
  3. schema.org JSON-LD ``Product`` data (used by most big retailers)
  4. Generic price meta tags / microdata

Requests impersonate a real Chrome browser (curl_cffi) because most retailers
reject plain Python HTTP clients.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests

TIMEOUT = 25
RETRIES = 2

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

BLOCK_MARKERS = (
    "enter the characters you see below",      # Amazon captcha
    "to discuss automated access to amazon",    # Amazon captcha
    "robot or human?",                          # Walmart
    "px-captcha",                               # PerimeterX (Walmart, Home Depot)
    "access denied</title>",                    # Akamai (Lowe's, Costco, Best Buy)
    "pardon our interruption",                  # Distil / Imperva
    "are you a human",
)

STORE_DOMAINS = {
    "amazon": "Amazon",
    "walmart": "Walmart",
    "target": "Target",
    "bestbuy": "Best Buy",
    "costco": "Costco",
    "homedepot": "Home Depot",
    "lowes": "Lowe's",
    "walgreens": "Walgreens",
}


@dataclass
class PriceResult:
    store: str
    url: str
    price: float | None = None
    in_stock: bool | None = None
    title: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.price is not None


class BlockedError(Exception):
    pass


# --------------------------------------------------------------------------- helpers

def store_name(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for key, name in STORE_DOMAINS.items():
        if key in host:
            return name
    return host.removeprefix("www.")


def parse_price(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = str(value).replace(",", "").replace(" ", " ")
    match = re.search(r"\d+(?:\.\d{1,2})?", text)
    if not match:
        return None
    price = float(match.group())
    return price if price > 0 else None


def _stock_from_text(value) -> bool | None:
    if value is None:
        return None
    text = str(value).lower()
    if any(w in text for w in ("outofstock", "out_of_stock", "soldout", "discontinued", "unavailable")):
        return False
    if any(w in text for w in ("instock", "in_stock", "limitedavailability", "onlineonly", "preorder")):
        return True
    return None


def fetch(url: str, *, json_response: bool = False):
    last_exc: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = requests.get(
                url,
                headers=HEADERS,
                impersonate="chrome",
                timeout=TIMEOUT,
                allow_redirects=True,
            )
            if resp.status_code in (403, 429, 503):
                raise BlockedError(f"HTTP {resp.status_code} (bot protection)")
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}")
            if json_response:
                return resp.json()
            html = resp.text
            head = html[:20000].lower()
            if any(m in head for m in BLOCK_MARKERS):
                raise BlockedError("captcha / bot-check page")
            return html
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < RETRIES:
                time.sleep(2 + random.random() * 3)
    raise last_exc  # type: ignore[misc]


# --------------------------------------------------------------------------- generic extractors

def _iter_jsonld(soup: BeautifulSoup):
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                for key in ("@graph", "hasVariant", "mainEntity", "itemListElement"):
                    if key in node:
                        stack.append(node[key])


def _is_product(node: dict) -> bool:
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(str(x).lower() in ("product", "productgroup", "individualproduct") for x in types)


def from_jsonld(soup: BeautifulSoup):
    for node in _iter_jsonld(soup):
        if not _is_product(node):
            continue
        offers = node.get("offers")
        offer_list = offers if isinstance(offers, list) else [offers] if offers else []
        prices, stock = [], None
        for offer in offer_list:
            if not isinstance(offer, dict):
                continue
            nested = offer.get("offers")
            if isinstance(nested, list):
                offer_list.extend(nested)
            p = parse_price(offer.get("price")) or parse_price(offer.get("lowPrice"))
            spec = offer.get("priceSpecification")
            if p is None and isinstance(spec, dict):
                p = parse_price(spec.get("price"))
            if p is None and isinstance(spec, list) and spec and isinstance(spec[0], dict):
                p = parse_price(spec[0].get("price"))
            if p is not None:
                prices.append(p)
            s = _stock_from_text(offer.get("availability"))
            if s is not None:
                stock = s if stock is None else (stock or s)
        if prices:
            return min(prices), stock, node.get("name")
    return None


def from_meta(soup: BeautifulSoup):
    for attrs in (
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
        {"itemprop": "price"},
        {"name": "twitter:data1"},
    ):
        tag = soup.find(attrs=attrs)
        if tag:
            p = parse_price(tag.get("content") or tag.get_text())
            if p:
                return p, None, None
    return None


def from_selector(soup: BeautifulSoup, selector: str):
    tag = soup.select_one(selector)
    if tag:
        p = parse_price(tag.get("content") or tag.get_text())
        if p:
            return p, None, None
    return None


def _page_title(soup: BeautifulSoup) -> str | None:
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    return soup.title.get_text(strip=True) if soup.title else None


# --------------------------------------------------------------------------- store-specific

def amazon(soup: BeautifulSoup, html: str):
    selectors = (
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#apex_desktop .a-price .a-offscreen",
        "#priceblock_dealprice",
        "#priceblock_ourprice",
        "#price_inside_buybox",
        "#tp_price_block_total_price_ww .a-offscreen",
    )
    price = None
    for sel in selectors:
        tag = soup.select_one(sel)
        if tag and (price := parse_price(tag.get_text())):
            break
    if price is None:
        whole = soup.select_one("#corePrice_feature_div .a-price-whole, .priceToPay .a-price-whole")
        frac = soup.select_one("#corePrice_feature_div .a-price-fraction, .priceToPay .a-price-fraction")
        if whole:
            price = parse_price(whole.get_text().strip(".") + "." + (frac.get_text() if frac else "00"))
    if price is None:
        return None
    avail = soup.select_one("#availability")
    stock = None
    if avail:
        text = avail.get_text(" ", strip=True).lower()
        stock = not ("unavailable" in text or "out of stock" in text)
    title_tag = soup.select_one("#productTitle")
    return price, stock, title_tag.get_text(strip=True) if title_tag else None


def _find_key(obj, key):
    """Depth-first search for the first dict value stored under ``key``."""
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if key in node and node[key] is not None:
                return node[key]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def walmart(soup: BeautifulSoup, html: str):
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag:
        return None
    try:
        data = json.loads(tag.string or "")
    except Exception:
        return None
    product = _find_key(data, "product") or {}
    price_info = _find_key(product, "priceInfo") or _find_key(data, "priceInfo") or {}
    current = price_info.get("currentPrice") if isinstance(price_info, dict) else None
    price = parse_price(current.get("price")) if isinstance(current, dict) else None
    if price is None:
        return None
    status = product.get("availabilityStatus") if isinstance(product, dict) else None
    stock = None if status is None else str(status).upper() == "IN_STOCK"
    return price, stock, product.get("name") if isinstance(product, dict) else None


TARGET_API = (
    "https://redsky.target.com/redsky_aggregations/v1/web/pdp_client_v1"
    "?key=9f36aeafbe60771e321a7cc95a78140772ab3e96&tcin={tcin}&pricing_store_id=3991"
)


def target_api(url: str):
    match = re.search(r"/A-(\d+)", url)
    if not match:
        return None
    data = fetch(TARGET_API.format(tcin=match.group(1)), json_response=True)
    product = (data.get("data") or {}).get("product") or {}
    price_block = product.get("price") or {}
    price = parse_price(price_block.get("current_retail")) or parse_price(
        price_block.get("formatted_current_price")
    )
    if price is None:
        return None
    title = ((product.get("item") or {}).get("product_description") or {}).get("title")
    return price, None, title


BESTBUY_API = (
    "https://api.bestbuy.com/v1/products/{sku}.json"
    "?apiKey={key}&show=sku,name,salePrice,regularPrice,onlineAvailability"
)


def bestbuy_api(url: str, sku: str | None):
    """Official Best Buy Products API (free key: https://developer.bestbuy.com)."""
    key = os.environ.get("BESTBUY_API_KEY")
    if not key:
        return None
    sku = sku or (m.group(1) if (m := re.search(r"(?:/sku/|skuId=|/)(\d{7})(?:\D|$)", url)) else None)
    if not sku:
        raise RuntimeError("Best Buy API needs the 7-digit SKU - add 'sku:' to this store in products.yaml")
    data = fetch(BESTBUY_API.format(sku=sku, key=key), json_response=True)
    price = parse_price(data.get("salePrice")) or parse_price(data.get("regularPrice"))
    if price is None:
        return None
    return price, data.get("onlineAvailability"), data.get("name")


def bestbuy(soup: BeautifulSoup, html: str):
    tag = soup.select_one(
        "[data-testid='customer-price'] span, .priceView-customer-price span[aria-hidden='true'], "
        ".priceView-hero-price span[aria-hidden='true']"
    )
    if tag and (price := parse_price(tag.get_text())):
        return price, None, None
    return None


def regex_fallback(html: str):
    for pattern in (
        r'"current_retail"\s*:\s*([\d.]+)',
        r'"currentPrice"\s*:\s*\{[^}]*?"price"\s*:\s*([\d.]+)',
        r'"salePrice"\s*:\s*"?([\d.]+)',
        r'"finalPrice"\s*:\s*"?([\d.]+)',
    ):
        match = re.search(pattern, html)
        if match and (price := parse_price(match.group(1))):
            return price, None, None
    return None


STORE_PARSERS = {
    "Amazon": amazon,
    "Walmart": walmart,
    "Best Buy": bestbuy,
}


# --------------------------------------------------------------------------- entry point

def get_price(
    url: str, selector: str | None = None, store: str | None = None, sku: str | None = None
) -> PriceResult:
    store = store or store_name(url)
    result = PriceResult(store=store, url=url)
    try:
        found = None
        if store == "Best Buy" and not selector:
            found = bestbuy_api(url, str(sku) if sku else None)
        if store == "Target" and not selector:
            try:
                found = target_api(url)
            except Exception:
                found = None  # fall back to the HTML page

        if found is None:
            html = fetch(url)
            soup = BeautifulSoup(html, "html.parser")
            if selector:
                found = from_selector(soup, selector)
            if found is None and store in STORE_PARSERS:
                found = STORE_PARSERS[store](soup, html)
            if found is None:
                found = from_jsonld(soup)
            if found is None:
                found = from_meta(soup)
            if found is None:
                found = regex_fallback(html)
            if found is not None and not found[2]:
                found = (found[0], found[1], _page_title(soup))

        if found is None:
            result.error = "price not found on page (add a 'selector' in products.yaml)"
        else:
            result.price, result.in_stock, result.title = found
    except BlockedError as exc:
        result.error = f"blocked: {exc}"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {str(exc)[:160]}"
    if result.error and store == "Best Buy" and not os.environ.get("BESTBUY_API_KEY"):
        result.error += " | tip: set BESTBUY_API_KEY to use the official Best Buy API"
    return result
