from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any


def ensure_dir(path: Path) -> None:
    """Create directory if it does not exist."""
    path.mkdir(parents=True, exist_ok=True)


def safe_filename(value: str) -> str:
    """Convert arbitrary text into a filesystem-safe filename."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "page"


def timestamp_to_text(value: Any) -> str:
    """Convert unix timestamp (seconds or milliseconds) to local datetime text."""
    if value in (None, ""):
        return ""
    number = int(value)
    if number > 10**12:
        date_value = dt.datetime.fromtimestamp(number / 1000)
    else:
        date_value = dt.datetime.fromtimestamp(number)
    return date_value.strftime("%Y-%m-%d %H:%M:%S")


def clean_text(text: str) -> str:
    """Collapse whitespace and trim text."""
    return re.sub(r"\s+", " ", (text or "")).strip()


def is_meaningful_comment(text: str) -> bool:
    """Filter out UI labels, counters, and time-only strings from comment text."""
    if not text:
        return False

    lowered = text.lower()
    blocked_exact = {
        "like",
        "reply",
        "share",
        "comment",
        "publish",
        "view more replies",
        "查看更多回复",
        "查看全部回复",
        "点赞",
        "回复",
        "分享",
        "评论",
        "发布",
    }
    if lowered in blocked_exact or text in blocked_exact:
        return False

    if re.fullmatch(r"[\d\s,.:/+\-]+", text):
        return False
    if re.fullmatch(r"\d+[smhdw]", lowered):
        return False
    if re.fullmatch(r"\d+[秒分钟小时天周月年]前", text):
        return False

    return True


def extract_first_string(node: Any, keys: list[str]) -> str:
    """Recursively extract the first non-empty string value by candidate keys."""
    if not isinstance(node, dict):
        return ""

    lowered_map = {str(key).lower(): value for key, value in node.items()}
    for key in keys:
        value = lowered_map.get(key.lower())
        if isinstance(value, str):
            text = clean_text(value)
            if text:
                return text

    for value in node.values():
        if isinstance(value, dict):
            text = extract_first_string(value, keys)
            if text:
                return text
    return ""


def extract_first_number(node: Any, keys: list[str]) -> int | str:
    """Recursively extract the first numeric value by candidate keys."""
    if not isinstance(node, dict):
        return ""

    lowered_map = {str(key).lower(): value for key, value in node.items()}
    for key in keys:
        value = lowered_map.get(key.lower())
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)

    for value in node.values():
        if isinstance(value, dict):
            nested = extract_first_number(value, keys)
            if nested != "":
                return nested
    return ""


def extract_richtext_text(body: Any) -> str:
    """Extract plain text from Binance RichText JSON format.

    The RichText format stores text in:
      body (JSON string) → hash → {block_id} → config → content → items
      where items have id="RichTextText" → config → content (actual text)
    """
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return clean_text(body)

    if not isinstance(body, dict):
        return clean_text(str(body)) if body else ""

    hash_data = body.get("hash")
    if not isinstance(hash_data, dict):
        return ""

    texts: list[str] = []
    for block in hash_data.values():
        if not isinstance(block, dict):
            continue
        config = block.get("config")
        if not isinstance(config, dict):
            continue
        content_list = config.get("content")
        if not isinstance(content_list, list):
            continue
        for item in content_list:
            if not isinstance(item, dict):
                continue
            if item.get("id") == "RichTextText":
                text = item.get("config", {}).get("content", "")
                if text and isinstance(text, str):
                    texts.append(text)

    return clean_text("".join(texts)) if texts else ""


def dump_page_content(page: Any, dump_dir: Path, post_id: str) -> None:
    """Save page HTML, text, and screenshot for debugging and offline parsing."""
    ensure_dir(dump_dir)
    base = safe_filename(post_id)
    html_path = dump_dir / f"{base}.html"
    text_path = dump_dir / f"{base}.txt"
    screenshot_path = dump_dir / f"{base}.png"

    try:
        html = page.content()
        html_path.write_text(html, encoding="utf-8")
    except Exception as exc:
        html_path.write_text(f"failed to dump html: {exc}", encoding="utf-8")

    try:
        body_text = page.locator("body").inner_text(timeout=3000)
        text_path.write_text(body_text, encoding="utf-8")
    except Exception as exc:
        text_path.write_text(f"failed to dump text: {exc}", encoding="utf-8")

    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception:
        pass
