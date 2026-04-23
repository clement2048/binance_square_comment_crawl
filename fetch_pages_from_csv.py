from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from crawler_util import ensure_dir

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


DEFAULT_CSV_PATH = Path("master_news_dataset.csv")
DEFAULT_OUTPUT_DIR = Path("update_news/csv_news_page_dump")
DEFAULT_USER_DATA_DIR = Path("tmp_chrome_profile")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download post HTML pages from csv. "
            "Default output is HTML-only to save disk space."
        )
    )
    parser.add_argument(
        "--csv-path",
        default=str(DEFAULT_CSV_PATH),
        help=f"CSV file path, default: {DEFAULT_CSV_PATH}",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Directory to store downloaded HTML files, default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument("--limit", type=int, default=0, help="Max number of posts to process, 0 means all")
    parser.add_argument("--offset", type=int, default=0, help="Offset when scanning DB rows")
    parser.add_argument("--pause-seconds", type=float, default=0.8, help="Pause between pages")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument(
        "--user-data-dir",
        default=str(DEFAULT_USER_DATA_DIR),
        help=f"Persistent Chromium profile dir, default: {DEFAULT_USER_DATA_DIR}",
    )
    parser.add_argument("--wait-for-login", action="store_true", help="Pause for manual login before downloading")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing HTML files")
    parser.add_argument("--save-screenshot", action="store_true", help="Also save PNG screenshot")
    parser.add_argument("--timeout-seconds", type=int, default=60, help="Page navigation timeout in seconds")
    parser.add_argument(
        "--min-comment-total",
        type=int,
        default=0,
        help="Only save posts with comment_total_num >= this threshold, 0 means disabled",
    )
    parser.add_argument(
        "--require-products",
        action="store_true",
        help="Only save posts with at least one extracted product symbol",
    )
    parser.add_argument(
        "--min-post-age-days",
        type=int,
        default=0,
        help="Only save posts that are at least N days old, 0 means disabled",
    )
    parser.add_argument("--check-only", action="store_true", help="Only verify DB and browser setup")
    return parser.parse_args()


def extract_app_data(html_content: str) -> dict[str, Any] | None:
    patterns = [
        r'<script id="__APP_DATA" type="application/json">(.*?)</script>',
        r"window\.__APP_DATA__\s*=\s*(\{.*?\});",
    ]
    for pattern in patterns:
        match = re.search(pattern, html_content, re.DOTALL)
        if not match:
            continue
        app_data_text = html.unescape(match.group(1)).strip()
        if not app_data_text:
            continue
        try:
            return json.loads(app_data_text)
        except Exception:
            continue
    return None


def find_post_data_in_app_data(app_data: dict[str, Any], post_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def walk(node: Any, depth: int = 0) -> bool:
        nonlocal result
        if depth > 16:
            return False
        if isinstance(node, dict):
            node_post_id = str(node.get("postId") or "")
            node_content_id = str(node.get("contentId") or "")
            node_id = str(node.get("id") or "")
            if node_post_id == post_id or node_content_id == post_id:
                result = node
                return True
            if node_id == post_id:
                result = node
                return True

            for value in node.values():
                if walk(value, depth + 1):
                    return True

        elif isinstance(node, list):
            for item in node:
                if walk(item, depth + 1):
                    return True
        return False

    walk(app_data)
    return result


def normalize_timestamp_to_ms(value: Any) -> int:
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0

    if text.isdigit():
        num = int(text)
        return num if num > 10**12 else num * 1000

    for parser in (
        lambda s: datetime.fromisoformat(s.replace("Z", "").replace("T", " ")).timestamp(),
        lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp(),
        lambda s: datetime.strptime(s, "%Y-%m-%d").timestamp(),
    ):
        try:
            return int(parser(re.sub(r"\.\d+", "", text)) * 1000)
        except Exception:
            continue
    return 0


def extract_comment_total_num(post_data: dict[str, Any]) -> int:
    direct_keys = ["commentCount", "commentNum", "commentTotal", "commentsCount"]
    for key in direct_keys:
        value = post_data.get(key)
        if value is None:
            continue
        try:
            num = int(value)
            if num >= 0:
                return num
        except Exception:
            continue

    max_count = 0

    def walk(node: Any, depth: int = 0) -> None:
        nonlocal max_count
        if depth > 10:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key in direct_keys:
                    try:
                        max_count = max(max_count, int(value))
                    except Exception:
                        pass
                walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(post_data)
    return max_count


def extract_products(post_data: dict[str, Any], html_content: str) -> list[str]:
    symbols: set[str] = set()
    key_whitelist = {"symbol", "baseAsset", "pair", "coinSymbol", "ticker", "asset"}

    def normalize_symbol(raw: Any) -> str:
        token = re.sub(r"[^A-Za-z0-9]", "", str(raw or "")).upper()
        if 2 <= len(token) <= 15 and any(c.isalpha() for c in token):
            return token
        return ""

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 10:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key in key_whitelist and isinstance(value, str):
                    symbol = normalize_symbol(value)
                    if symbol:
                        symbols.add(symbol)
                walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
        elif isinstance(node, str):
            for match in re.findall(r"\$([A-Z][A-Z0-9]{1,9})\b", node.upper()):
                symbol = normalize_symbol(match)
                if symbol:
                    symbols.add(symbol)

    walk(post_data)
    if symbols:
        return sorted(symbols)

    for match in re.findall(r"\$([A-Z][A-Z0-9]{1,9})\b", html_content.upper()):
        symbol = normalize_symbol(match)
        if symbol:
            symbols.add(symbol)
    return sorted(symbols)


def extract_post_time_raw(post_data: dict[str, Any]) -> str:
    time_keys = [
        "createTime",
        "publishTime",
        "postTime",
        "publishedDate",
        "createdTime",
        "publishDate",
        "publishAt",
        "time",
        "date",
    ]
    for key in time_keys:
        value = post_data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()

    found = ""

    def walk(node: Any, depth: int = 0) -> bool:
        nonlocal found
        if depth > 10:
            return False
        if isinstance(node, dict):
            for key in time_keys:
                value = node.get(key)
                if value is not None and str(value).strip():
                    found = str(value).strip()
                    return True
            for value in node.values():
                if walk(value, depth + 1):
                    return True
        elif isinstance(node, list):
            for item in node:
                if walk(item, depth + 1):
                    return True
        return False

    walk(post_data)
    return found


def extract_post_time_from_json_ld(html_content: str) -> str:
    pattern = r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'
    matches = re.findall(pattern, html_content, re.DOTALL | re.IGNORECASE)
    for json_ld in matches:
        text = html.unescape(json_ld).strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except Exception:
            continue

        candidates: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            candidates.append(payload)
            graph = payload.get("@graph")
            if isinstance(graph, list):
                candidates.extend([item for item in graph if isinstance(item, dict)])
        elif isinstance(payload, list):
            candidates.extend([item for item in payload if isinstance(item, dict)])

        for node in candidates:
            if not isinstance(node, dict):
                continue
            val = node.get("datePublished") or node.get("dateCreated")
            if val is not None and str(val).strip():
                return str(val).strip()
    return ""


def extract_post_time_from_meta(html_content: str) -> str:
    patterns = [
        r'<meta[^>]*property=["\']article:published_time["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]*name=["\']pubdate["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]*name=["\']date["\'][^>]*content=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_content, re.IGNORECASE)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def extract_post_metadata(html_content: str, post_id: str) -> dict[str, Any]:
    app_data = extract_app_data(html_content)
    post_data = find_post_data_in_app_data(app_data, post_id) if app_data else {}
    comment_total_num = extract_comment_total_num(post_data) if post_data else 0
    products = extract_products(post_data, html_content)
    post_time_raw = extract_post_time_raw(post_data) if post_data else ""
    if not post_time_raw:
        post_time_raw = extract_post_time_from_json_ld(html_content)
    if not post_time_raw:
        post_time_raw = extract_post_time_from_meta(html_content)
    post_time_ms = normalize_timestamp_to_ms(post_time_raw)

    return {
        "comment_total_num": comment_total_num,
        "products": products,
        "post_time_raw": post_time_raw,
        "post_time_ms": post_time_ms,
    }


def evaluate_filter_reasons(metadata: dict[str, Any], args: argparse.Namespace, now_ms: int) -> list[str]:
    reasons: list[str] = []

    if int(args.min_comment_total) > 0 and int(metadata.get("comment_total_num") or 0) < int(args.min_comment_total):
        reasons.append("low_comment_total")

    if args.require_products and not metadata.get("products"):
        reasons.append("no_products")

    if int(args.min_post_age_days) > 0:
        post_time_ms = int(metadata.get("post_time_ms") or 0)
        if post_time_ms <= 0:
            reasons.append("missing_post_time_for_age_filter")
        else:
            min_age_ms = int(args.min_post_age_days) * 24 * 3600 * 1000
            if (now_ms - post_time_ms) < min_age_ms:
                reasons.append("too_recent_for_label")

    return reasons


def create_browser_context(headless: bool, user_data_dir: str) -> tuple[Any, Any]:
    if sync_playwright is None:
        raise RuntimeError(
            "playwright is not installed. Run: pip install playwright && playwright install chromium"
        )

    playwright = sync_playwright().start()
    if user_data_dir:
        print(f"[browser] using persistent profile: {user_data_dir}")
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=headless,
            viewport={"width": 1366, "height": 1600},
        )
        return playwright, context

    print("[browser] using ephemeral profile")
    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context(viewport={"width": 1366, "height": 1600})
    return playwright, context


def safe_close_browser(playwright_obj: Any, context: Any) -> None:
    try:
        context.close()
    finally:
        playwright_obj.stop()


def load_posts(csv_path: Path, limit: int, offset: int) -> list[dict[str, str]]:
    with(open(csv_path, "r", encoding="utf-8")) as f:
        header = f.readline()
        if not header:
            return []
        columns = [col.strip() for col in header.split(",")]
        if "post_id" not in columns or "link" not in columns:
            raise ValueError("CSV must contain 'post_id' and 'link' columns")

    posts = []
    with(open(csv_path, "r", encoding="utf-8")) as f:
        for line in f:
            if line.strip():
                values = [v.strip() for v in line.split(",")]
                if len(values) >= 2:
                    posts.append({"post_id": values[0], "link": values[1]})

    if limit > 0:
        posts = posts[:limit]
    if offset > 0:
        posts = posts[offset:]

    return posts
    


def main() -> None:
    # 解析CSV文件路径和输出目录等参数
    args = parse_args()
    csv_path = Path(args.csv_path)
    output_dir = Path(args.output_dir)
    ensure_dir(output_dir)

    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    posts = load_posts(csv_path=csv_path, limit=args.limit, offset=args.offset)
    if not posts:
        print("[fetch-html] no posts found in CSV for given limit/offset")
        return

    print(f"[fetch-html] loaded posts from csv: {len(posts)}")


    # 创建浏览器上下文
    playwright_obj, context = create_browser_context(
        headless=args.headless,
        user_data_dir=args.user_data_dir,
    )

    ok_count = 0
    skipped_count = 0
    failed_count = 0
    filtered_count = 0
    failures: list[dict[str, str]] = []
    filtered_details: list[dict[str, Any]] = []
    filtered_reason_counter = {
        "low_comment_total": 0,
        "no_products": 0,
        "too_recent_for_label": 0,
        "missing_post_time_for_age_filter": 0,
    }

    try:
        page = context.new_page()
        if args.wait_for_login:
            page.goto("https://www.binance.com/en/square", wait_until="domcontentloaded", timeout=60000)
            input(
                "[login] Browser opened. Please finish login and then press Enter to continue downloading HTML..."
            )
            page.wait_for_timeout(1200)

        if args.check_only:
            first = posts[0]
            page.goto(first["link"], wait_until="domcontentloaded", timeout=args.timeout_seconds * 1000)
            page.wait_for_timeout(1200)
            print(f"[check] page open ok for post_id={first['post_id']}")
            return

        total = len(posts)
        now_ms = int(time.time() * 1000)
        for idx, row in enumerate(posts, start=1):
            post_id = row["post_id"]
            url = row["link"]
            html_path = output_dir / f"{post_id}.html"

            if html_path.exists() and not args.overwrite:
                skipped_count += 1
                if idx % 50 == 0 or idx == total:
                    print(
                        f"[fetch-html] progress {idx}/{total} ok={ok_count} skipped={skipped_count} filtered={filtered_count} failed={failed_count}"
                    )
                continue

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_seconds * 1000)
                page.wait_for_timeout(1500)
                html_content = page.content()

                metadata = extract_post_metadata(html_content=html_content, post_id=post_id)
                filter_reasons = evaluate_filter_reasons(metadata=metadata, args=args, now_ms=now_ms)
                if filter_reasons:
                    filtered_count += 1
                    for reason in sorted(set(filter_reasons)):
                        if reason in filtered_reason_counter:
                            filtered_reason_counter[reason] += 1
                    filtered_details.append(
                        {
                            "post_id": post_id,
                            "url": url,
                            "filter_reasons": sorted(set(filter_reasons)),
                            "comment_total_num": int(metadata.get("comment_total_num") or 0),
                            "products": metadata.get("products") or [],
                            "post_time_raw": str(metadata.get("post_time_raw") or ""),
                            "post_time_ms": int(metadata.get("post_time_ms") or 0),
                        }
                    )
                    continue

                html_path.write_text(html_content, encoding="utf-8")

                ok_count += 1
            except Exception as exc:
                failed_count += 1
                failures.append({"post_id": post_id, "url": url, "error": str(exc)})

            if idx % 20 == 0 or idx == total:
                print(
                    f"[fetch-html] progress {idx}/{total} ok={ok_count} skipped={skipped_count} filtered={filtered_count} failed={failed_count}"
                )

            time.sleep(max(0.0, float(args.pause_seconds)))

    finally:
        safe_close_browser(playwright_obj, context)

    summary = {
        "csv_path": str(csv_path),
        "output_dir": str(output_dir),
        "total_requested": len(posts),
        "ok": ok_count,
        "skipped": skipped_count,
        "filtered": filtered_count,
        "failed": failed_count,
        "filtered_reason_counter": filtered_reason_counter,
        "filters": {
            "min_comment_total": int(args.min_comment_total),
            "require_products": bool(args.require_products),
            "min_post_age_days": int(args.min_post_age_days),
        },
    }
    summary_path = output_dir / "fetch_pages_from_csv_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    if failures:
        failure_path = output_dir / "fetch_pages_from_csv_failures.json"
        failure_path.write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")

    if filtered_details:
        filtered_path = output_dir / "fetch_pages_from_csv_filtered.json"
        filtered_path.write_text(json.dumps(filtered_details, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"[fetch-html] done requested={len(posts)} ok={ok_count} skipped={skipped_count} filtered={filtered_count} failed={failed_count}"
    )
    print(f"[fetch-html] summary: {summary_path}")


if __name__ == "__main__":
    main()
