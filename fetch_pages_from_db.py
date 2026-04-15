from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from crawler_util import ensure_dir

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


DEFAULT_DB_PATH = Path("update_news_v2/square_posts_v2.db")
DEFAULT_OUTPUT_DIR = Path("update_news/binance_square_page_dump")
DEFAULT_USER_DATA_DIR = Path("tmp_chrome_profile")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download post HTML pages from crawler_v2 SQLite DB. "
            "Default output is HTML-only to save disk space."
        )
    )
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help=f"SQLite file path, default: {DEFAULT_DB_PATH}",
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
    parser.add_argument("--check-only", action="store_true", help="Only verify DB and browser setup")
    return parser.parse_args()


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


def load_posts(db_path: Path, limit: int, offset: int) -> list[dict[str, str]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        query = "SELECT post_id, link FROM posts ORDER BY first_seen_at ASC"
        params: list[Any] = []
        if limit > 0:
            query += " LIMIT ? OFFSET ?"
            params.extend([int(limit), int(offset)])
        elif offset > 0:
            # SQLite requires LIMIT when OFFSET is used.
            query += " LIMIT -1 OFFSET ?"
            params.append(int(offset))

        rows = conn.execute(query, params).fetchall()
        return [{"post_id": str(r["post_id"]), "link": str(r["link"])} for r in rows]
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    db_path = Path(args.db_path)
    output_dir = Path(args.output_dir)
    ensure_dir(output_dir)

    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")

    posts = load_posts(db_path=db_path, limit=args.limit, offset=args.offset)
    if not posts:
        print("[fetch-html] no posts found in DB for given limit/offset")
        return

    print(f"[fetch-html] loaded posts from db: {len(posts)}")

    playwright_obj, context = create_browser_context(
        headless=args.headless,
        user_data_dir=args.user_data_dir,
    )

    ok_count = 0
    skipped_count = 0
    failed_count = 0
    failures: list[dict[str, str]] = []

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
        for idx, row in enumerate(posts, start=1):
            post_id = row["post_id"]
            url = row["link"]
            html_path = output_dir / f"{post_id}.html"
            png_path = output_dir / f"{post_id}.png"

            if html_path.exists() and not args.overwrite:
                skipped_count += 1
                if idx % 50 == 0 or idx == total:
                    print(
                        f"[fetch-html] progress {idx}/{total} ok={ok_count} skipped={skipped_count} failed={failed_count}"
                    )
                continue

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_seconds * 1000)
                page.wait_for_timeout(1500)
                html_content = page.content()
                html_path.write_text(html_content, encoding="utf-8")

                if args.save_screenshot:
                    try:
                        page.screenshot(path=str(png_path), full_page=True)
                    except Exception:
                        pass

                ok_count += 1
            except Exception as exc:
                failed_count += 1
                failures.append({"post_id": post_id, "url": url, "error": str(exc)})

            if idx % 20 == 0 or idx == total:
                print(
                    f"[fetch-html] progress {idx}/{total} ok={ok_count} skipped={skipped_count} failed={failed_count}"
                )

            time.sleep(max(0.0, float(args.pause_seconds)))

    finally:
        safe_close_browser(playwright_obj, context)

    summary = {
        "db_path": str(db_path),
        "output_dir": str(output_dir),
        "total_requested": len(posts),
        "ok": ok_count,
        "skipped": skipped_count,
        "failed": failed_count,
    }
    summary_path = output_dir / "fetch_pages_from_db_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    if failures:
        failure_path = output_dir / "fetch_pages_from_db_failures.json"
        failure_path.write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"[fetch-html] done requested={len(posts)} ok={ok_count} skipped={skipped_count} failed={failed_count}"
    )
    print(f"[fetch-html] summary: {summary_path}")


if __name__ == "__main__":
    main()
