"""DealsBot - checks product prices and pushes an ntfy alert when a target price is hit.

Usage:
  python bot.py                 # one check of every product (what GitHub Actions runs)
  python bot.py --dry-run       # check prices, print them, send nothing, save nothing
  python bot.py --test-notify   # send a test notification to your phone
  python bot.py --loop 15       # keep running locally, checking every 15 minutes
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import yaml

import link_finder
import notifier
from matching import DEFAULT_EXCLUDE, keyword_patterns, title_matches
import slickdeals
import store_search
from scrapers import PriceResult, get_price, store_name

ROOT = Path(__file__).resolve().parent
DEFAULT_SETTINGS = {
    "ntfy_server": "https://ntfy.sh",
    "realert_hours": 12,      # repeat the alert for an unchanged deal after N hours (0 = never)
    "notify_errors": True,    # warn (low priority) when a store keeps failing
    "error_alert_after": 12,  # consecutive failed checks before warning (12 x 15 min = 3 h)
    "max_workers": 8,
    "search_stores": ["Walmart", "Target"],  # stores searched by product name
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def load_config(path: Path) -> tuple[dict, list[dict]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    settings = {**DEFAULT_SETTINGS, **(data.get("settings") or {})}
    raw = data.get("products") or []
    if isinstance(raw, dict):  # simple form:  "Apple AirPods Pro 3: 129"
        raw = [{"name": str(k), "target_price": v} for k, v in raw.items()]
    products = []
    for p in raw:
        if isinstance(p, dict) and len(p) == 1 and "name" not in p:  # "- Apple AirPods Pro 3: 129"
            (k, v), = p.items()
            p = {"name": str(k), "target_price": v}
        if not p or p.get("enabled", True) is False:
            continue
        if "name" not in p or "target_price" not in p:
            print(f"Skipping product without name/target_price: {p}")
            continue
        stores = []
        for s in p.get("stores") or []:
            s = {"url": s} if isinstance(s, str) else dict(s)
            url = (s.get("url") or "").strip()
            if url.startswith("http") and s.get("enabled", True) is not False:
                s["url"] = url
                stores.append(s)
        p["stores"] = stores
        p["target_price"] = float(str(p["target_price"]).replace("$", "").replace(",", ""))
        p.setdefault("min_valid_price", round(p["target_price"] * 0.5, 2))  # ignore cases/accessories
        p.setdefault("slickdeals", True)  # search deals by product name
        products.append(p)
    return settings, products


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            print("state.json is corrupt - starting fresh")
    return {"items": {}}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def hours_since(iso: str | None) -> float:
    if not iso:
        return float("inf")
    return (now() - datetime.fromisoformat(iso)).total_seconds() / 3600


def money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def handle_result(
    product: dict, result: PriceResult, state: dict, settings: dict, dry_run: bool, key: str | None = None
) -> str:
    key = key or f"{product['name']}|{result.url}"
    item = state["items"].setdefault(key, {})
    target = float(product["target_price"])
    item["checked_at"] = now().isoformat(timespec="seconds")

    if not result.ok and (result.error or "").startswith("no matching listing"):
        item["fails"] = 0  # store simply doesn't sell it right now - not an error
        return "not found at this store"

    if not result.ok:
        item["fails"] = item.get("fails", 0) + 1
        if (
            settings["notify_errors"]
            and item["fails"] == int(settings["error_alert_after"])
            and not dry_run
        ):
            notifier.send(
                f"DealsBot: {result.store} check failing",
                f"{product['name']} at {result.store} has failed {item['fails']} checks in a row.\n"
                f"Last error: {result.error}",
                click_url=result.url,
                priority="low",
                tags="warning",
                server=settings["ntfy_server"],
            )
        return f"ERROR  {result.error}"

    item["fails"] = 0
    if result.title and not title_matches(
        result.title,
        keyword_patterns(product.get("keywords") or product.get("search") or product["name"]),
        product.get("exclude") or DEFAULT_EXCLUDE,
    ):
        return f"{money(result.price)}  skipped (listing is not new / not this product: {result.title[:60]})"
    previous = item.get("price")
    item["price"] = result.price
    item["store"] = result.store
    if result.title:
        item["title"] = result.title[:150]

    min_valid = product.get("min_valid_price")
    if min_valid is not None and result.price < float(min_valid):
        return f"{money(result.price)}  ignored (below min_valid_price {money(float(min_valid))})"

    if result.price > target:
        item.pop("alerted_price", None)
        item.pop("alerted_at", None)
        return f"{money(result.price)}  (target {money(target)})"

    if product.get("require_in_stock") and result.in_stock is False:
        return f"{money(result.price)}  DEAL but out of stock - no alert"

    alerted_price = item.get("alerted_price")
    realert = float(settings["realert_hours"])
    should_alert = (
        alerted_price is None
        or result.price < alerted_price - 0.009
        or (realert > 0 and hours_since(item.get("alerted_at")) >= realert)
    )
    if not should_alert:
        return f"{money(result.price)}  DEAL (already alerted)"

    stock = {True: "In stock", False: "Out of stock", None: "Stock unknown"}[result.in_stock]
    lines = [
        f"{money(result.price)} at {result.store} - your target is {money(target)}.",
        f"Previous price: {money(previous)}" if previous and previous != result.price else None,
        stock,
        "Tap to open the deal.",
    ]
    title = f"Price drop: {product['name']} {money(result.price)}"
    if dry_run:
        return f"{money(result.price)}  DEAL (dry run - would notify)"
    sent = notifier.send(
        title,
        "\n".join(l for l in lines if l),
        click_url=result.url,
        priority=product.get("priority", "urgent"),
        tags="moneybag,tada",
        server=settings["ntfy_server"],
    )
    if sent:
        item["alerted_price"] = result.price
        item["alerted_at"] = now().isoformat(timespec="seconds")
        return f"{money(result.price)}  DEAL -> notification sent"
    return f"{money(result.price)}  DEAL -> notification FAILED"


def check_slickdeals(product: dict, state: dict, settings: dict, dry_run: bool) -> None:
    cfg = product["slickdeals"] if isinstance(product["slickdeals"], dict) else {}
    seen = state.setdefault("slickdeals", {}).setdefault(product["name"], [])
    label = f"{'Slickdeals':<12}"
    try:
        deals = slickdeals.matching_deals(cfg, product)
    except Exception as exc:
        print(f"\n{product['name']}\n  {label} ERROR  {type(exc).__name__}: {str(exc)[:160]}")
        return
    new = [d for d in deals if d.id not in seen]
    print(f"\n{product['name']}  (Slickdeals posts <= {money(float(product['target_price']))})")
    if not new:
        print(f"  {label} {len(deals)} matching post(s), none new")
        return
    new.sort(key=lambda d: d.price or 0)
    best = new[0]
    target = money(float(product["target_price"]))
    for deal in new:
        print(f"  {label} {money(deal.price)}  NEW POST: {deal.title[:90]}")
    if dry_run:
        print(f"  {label} (dry run - would send 1 notification for {len(new)} post(s))")
        return

    if len(new) == 1:
        title = f"Slickdeals: {product['name']} {money(best.price)}"
        body = f"{best.title}\nYour target is {target}.\nTap to open the deal post."
    else:
        title = f"Slickdeals: {len(new)} deals on {product['name']} from {money(best.price)}"
        shown = new[:8]
        body = "\n".join(f"- {money(d.price)}: {d.title[:80]}" for d in shown)
        if len(new) > len(shown):
            body += f"\n...and {len(new) - len(shown)} more"
        body += f"\nYour target is {target}. Tap to open the cheapest post."
    sent = notifier.send(
        title,
        body,
        click_url=best.link,
        priority=product.get("priority", "urgent"),
        tags="fire,moneybag",
        server=settings["ntfy_server"],
    )
    print(f"  {label} 1 notification for {len(new)} post(s) -> {'sent' if sent else 'FAILED'}")
    if sent:
        seen.extend(d.id for d in new)
    del seen[:-300]  # keep the list small


def run_once(config_path: Path, state_path: Path, dry_run: bool) -> int:
    settings, products = load_config(config_path)
    state = load_state(state_path)
    if not products:
        print("No products configured in products.yaml")
        return 0

    # add backend links: product_links.yaml + automatically discovered links
    seed = link_finder.load_seed_links(ROOT / "product_links.yaml")
    for p in products:
        have = {s["url"] for s in p["stores"]}
        extra = list(seed.get(p["name"].strip().lower(), []))
        extra += link_finder.discovered_links(p, state)
        for s in extra:
            s = {"url": s} if isinstance(s, str) else dict(s)
            if s.get("url", "").startswith("http") and s["url"] not in have:
                have.add(s["url"])
                p["stores"].append(s)
    names = {p["name"] for p in products}
    state["links"] = {k: v for k, v in state.get("links", {}).items() if k in names}

    # (product, state key, function, args) - direct store links + name searches
    jobs = []
    for p in products:
        linked_stores = {s.get("store") or store_name(s["url"]) for s in p["stores"]}
        for s in p["stores"]:
            jobs.append((p, f"{p['name']}|{s['url']}", get_price,
                         (s["url"], s.get("selector"), s.get("store"), s.get("sku"))))
        if p.get("search_stores", True):
            for store in settings["search_stores"]:
                if store in store_search.SEARCHERS and store not in linked_stores:
                    jobs.append((p, f"{p['name']}|search:{store}", store_search.search_store, (store, p)))

    print(f"[{now():%Y-%m-%d %H:%M:%S} UTC] checking {len(products)} product(s), {len(jobs)} store check(s)...")
    with ThreadPoolExecutor(max_workers=int(settings["max_workers"])) as pool:
        futures = [pool.submit(fn, *args) for _, _, fn, args in jobs]
        results = [f.result() for f in futures]

    current = None
    for (product, key, _, _), result in zip(jobs, results):
        if product is not current:
            current = product
            print(f"\n{product['name']}  (alert at <= {money(float(product['target_price']))})")
        status = handle_result(product, result, state, settings, dry_run, key)
        print(f"  {result.store:<12} {status}")

    for product in products:
        if product.get("slickdeals"):
            check_slickdeals(product, state, settings, dry_run)

    # forget state for products/links that were removed from the config
    live = {key for _, key, _, _ in jobs}
    state["items"] = {k: v for k, v in state["items"].items() if k in live}
    if not dry_run:
        save_state(state_path, state)
    if results:
        errors = sum(1 for r in results if not r.ok and not (r.error or "").startswith("no matching"))
        print(f"\n{sum(r.ok for r in results)} price(s) found, {errors} store error(s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(ROOT / "products.yaml"))
    parser.add_argument("--state", default=str(ROOT / "state.json"))
    parser.add_argument("--dry-run", action="store_true", help="check prices only; no alerts, no state saved")
    parser.add_argument("--test-notify", action="store_true", help="send a test push notification and exit")
    parser.add_argument("--loop", type=float, metavar="MINUTES", help="run forever, checking every N minutes")
    args = parser.parse_args()

    if args.test_notify:
        settings, _ = load_config(Path(args.config))
        ok = notifier.send(
            "DealsBot test",
            "If you can read this on your phone, notifications work!",
            tags="white_check_mark",
            server=settings["ntfy_server"],
        )
        print("Test notification sent." if ok else "Test notification FAILED.")
        return 0 if ok else 1

    if args.loop:
        while True:
            try:
                run_once(Path(args.config), Path(args.state), args.dry_run)
            except Exception as exc:
                print(f"Run failed: {exc}")
            time.sleep(args.loop * 60)

    return run_once(Path(args.config), Path(args.state), args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
