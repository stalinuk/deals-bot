"""Send push notifications through ntfy (https://ntfy.sh)."""
from __future__ import annotations

import os

from curl_cffi import requests


def _ascii_header(value: str) -> str:
    # HTTP headers must be latin-1; strip anything else (emoji, smart quotes...)
    return value.encode("latin-1", "ignore").decode("latin-1")


def send(
    title: str,
    message: str,
    *,
    click_url: str | None = None,
    priority: str = "high",
    tags: str = "moneybag",
    server: str | None = None,
    topic: str | None = None,
    token: str | None = None,
) -> bool:
    server = (server or os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    topic = (topic or os.environ.get("NTFY_TOPIC") or "").strip().strip("/")
    token = (token or os.environ.get("NTFY_TOKEN") or "").strip()
    if not topic:
        print("  [ntfy] NTFY_TOPIC is not set - notification skipped:")
        print(f"         {title}: {message}")
        return False

    headers = {
        "Title": _ascii_header(title),
        "Priority": priority,
        "Tags": tags,
    }
    if click_url:
        headers["Click"] = click_url
        headers["Actions"] = f"view, Open deal, {click_url}"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.post(
            f"{server}/{topic}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=20,
        )
        if resp.status_code >= 300:
            print(f"  [ntfy] failed: HTTP {resp.status_code} {resp.text[:200]}")
            return False
        return True
    except Exception as exc:  # network errors must never crash the bot
        print(f"  [ntfy] failed: {exc}")
        return False
