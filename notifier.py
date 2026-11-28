"""Send push notifications through ntfy (https://ntfy.sh).

Uses ntfy's JSON publishing so titles can contain any characters and each
notification can carry up to 3 tap-to-open buttons (an ntfy limit).
"""
from __future__ import annotations

import os

from curl_cffi import requests

MAX_BUTTONS = 3  # ntfy allows at most 3 action buttons per notification
PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5, "max": 5}


def send(
    title: str,
    message: str,
    *,
    click_url: str | None = None,
    buttons: list[tuple[str, str]] | None = None,
    priority: str = "high",
    tags: str = "moneybag",
    server: str | None = None,
    topic: str | None = None,
    token: str | None = None,
) -> bool:
    """``buttons`` is a list of (label, url); defaults to one "Open deal" button."""
    server = (server or os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    topic = (topic or os.environ.get("NTFY_TOPIC") or "").strip().strip("/")
    token = (token or os.environ.get("NTFY_TOKEN") or "").strip()
    if not topic:
        print("  [ntfy] NTFY_TOPIC is not set - notification skipped:")
        print(f"         {title}: {message}")
        return False

    if buttons is None and click_url:
        buttons = [("Open deal", click_url)]
    payload = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": PRIORITIES.get(str(priority).lower(), 4),
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
    }
    if click_url:
        payload["click"] = click_url
    if buttons:
        payload["actions"] = [
            {"action": "view", "label": label[:40], "url": url, "clear": False}
            for label, url in buttons[:MAX_BUTTONS]
        ]
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.post(server, json=payload, headers=headers, timeout=20)
        if resp.status_code >= 300:
            print(f"  [ntfy] failed: HTTP {resp.status_code} {resp.text[:200]}")
            return False
        return True
    except Exception as exc:  # network errors must never crash the bot
        print(f"  [ntfy] failed: {exc}")
        return False
