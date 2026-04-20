from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from crawler_util import ensure_dir

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


DEFAULT_OUTPUT_DIR = Path("update_news_v2")         # 输出目录，包含SQLite数据库和导出的CSV/JSON文件
DEFAULT_USER_DATA_DIR = Path("tmp_chrome_profile")  # 用于持久化登录状态的Chromium用户数据目录
DEFAULT_DB_NAME = "square_posts_v2.db"              # SQLite数据库文件名
SQUARE_HOME_URL_TEMPLATE = "https://www.binance.com/{lang}/square"


# 解析命令行参数，支持配置爬虫行为和输出选项
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Incremental Binance Square crawler (v2): persistent dedupe with SQLite, "
            "target-driven collection, and resumable runs."
        )
    )
    parser.add_argument("--lang", default="en", help="Square language, e.g. en / zh-CN")
    parser.add_argument("--target-posts", type=int, default=5000, help="Stop when unique posts in DB reach this count")
    parser.add_argument("--max-scroll-rounds", type=int, default=3000, help="Max scroll rounds for this run")
    parser.add_argument("--idle-stop-rounds", type=int, default=50, help="Stop if no new post appears for N rounds")
    parser.add_argument("--pause-seconds", type=float, default=1.0, help="Pause between rounds")
    parser.add_argument("--scroll-pixels", type=int, default=2600, help="Wheel scroll pixels per round")
    parser.add_argument("--max-runtime-minutes", type=float, default=0.0, help="0 means unlimited runtime")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--wait-for-login", action="store_true", help="Pause for manual login before collecting")
    parser.add_argument(
        "--user-data-dir",
        default=str(DEFAULT_USER_DATA_DIR),
        help=f"Persistent Chromium profile dir, default: {DEFAULT_USER_DATA_DIR}",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Output directory, default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--db-path",
        default="",
        help="SQLite path; defaults to <output-dir>/square_posts_v2.db",
    )
    parser.add_argument(
        "--export-limit",
        type=int,
        default=0,
        help="Export first N rows to CSV/JSON; 0 means export all stored rows",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=20,
        help="Print progress every N rounds",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only open Square page and verify browser workflow",
    )
    return parser.parse_args()


# 获取当前时间的文本表示
def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_post_url(url: str) -> str:
    try:
        parts = urlsplit((url or "").strip())
    except Exception:
        return ""

    path = (parts.path or "").rstrip("/")
    if "/square/post/" not in path:
        return ""

    normalized = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return normalized


def post_id_from_url(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    return path.split("/")[-1] if path else ""


# 创建Playwright浏览器上下文，支持持久化用户数据目录以保持登录状态
def create_browser_context(headless: bool, user_data_dir: str) -> tuple[Any, Any]:
    if sync_playwright is None:
        raise RuntimeError(
            "playwright is not installed. Run: pip install playwright && playwright install chromium"
        )

    # 优先使用持久化上下文，保持登录状态；如果未指定，则使用无状态上下文
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


# 创建一个新的SQLite数据库连接，并初始化post表结构，用来存储爬取到的帖子数据和爬虫运行日志
# 关键参数：post_id（从帖子URL中提取的唯一ID），link（帖子URL），first_seen_at（首次发现时间），last_seen_at（最后一次发现时间），seen_count（被发现的次数）
def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
            post_id TEXT PRIMARY KEY,
            link TEXT NOT NULL UNIQUE,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    # runs表用来记录每次爬虫运行的元数据和统计信息，便于分析和调试
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            lang TEXT NOT NULL,
            target_posts INTEGER NOT NULL,
            max_scroll_rounds INTEGER NOT NULL,
            idle_stop_rounds INTEGER NOT NULL,
            rounds_done INTEGER NOT NULL DEFAULT 0,
            new_added INTEGER NOT NULL DEFAULT 0,
            total_posts_after INTEGER NOT NULL DEFAULT 0,
            stop_reason TEXT NOT NULL DEFAULT ''
        )
        """
    )
    # 创建索引
    # TODO: 为什么这里是这两个索引？需要分析一下
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_first_seen ON posts(first_seen_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_last_seen ON posts(last_seen_at)")
    conn.commit()


def count_posts(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(1) FROM posts").fetchone()
    return int(row[0]) if row else 0


def insert_or_touch_posts(conn: sqlite3.Connection, urls: list[str], seen_at: str) -> int:
    new_count = 0
    for url in urls:
        post_id = post_id_from_url(url)
        if not post_id:
            continue

        existing = conn.execute("SELECT post_id FROM posts WHERE post_id=?", (post_id,)).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO posts(post_id, link, first_seen_at, last_seen_at, seen_count)
                VALUES (?, ?, ?, ?, 1)
                """,
                (post_id, url, seen_at, seen_at),
            )
            new_count += 1
        else:
            conn.execute(
                """
                UPDATE posts
                SET last_seen_at=?, seen_count=seen_count+1
                WHERE post_id=?
                """,
                (seen_at, post_id),
            )

    conn.commit()
    return new_count


# 获取页面上可见的帖子URL列表
def fetch_visible_post_urls(page: Any) -> list[str]:
    try:
        hrefs = page.locator("a[href*='/square/post/']").evaluate_all(
            "(els) => els.map(el => el.href).filter(Boolean)"
        )
    except Exception:
        hrefs = []

    result: list[str] = []
    seen: set[str] = set()
    for href in hrefs:
        normalized = normalize_post_url(str(href))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def write_posts_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "post_id",
        "time",
        "title",
        "subtitle",
        "content",
        "author",
        "author_username",
        "like_count",
        "comment_count",
        "view_count",
        "share_count",
        "related_symbols",
        "link",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def export_posts(conn: sqlite3.Connection, csv_path: Path, json_path: Path, export_limit: int) -> int:
    query = (
        "SELECT post_id, link, first_seen_at, last_seen_at, seen_count "
        "FROM posts ORDER BY first_seen_at ASC"
    )
    if export_limit > 0:
        query += f" LIMIT {int(export_limit)}"

    records = conn.execute(query).fetchall()
    rows: list[dict[str, Any]] = []
    raw: list[dict[str, Any]] = []

    for post_id, link, first_seen_at, last_seen_at, seen_count in records:
        rows.append(
            {
                "post_id": str(post_id),
                "time": str(first_seen_at),
                "title": "",
                "subtitle": "",
                "content": "",
                "author": "",
                "author_username": "",
                "like_count": 0,
                "comment_count": 0,
                "view_count": 0,
                "share_count": 0,
                "related_symbols": "",
                "link": str(link),
            }
        )
        raw.append(
            {
                "post_id": str(post_id),
                "link": str(link),
                "first_seen_at": str(first_seen_at),
                "last_seen_at": str(last_seen_at),
                "seen_count": int(seen_count),
            }
        )

    write_posts_csv(csv_path, rows)
    json_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(rows)


def update_run_end(
    conn: sqlite3.Connection,
    run_id: int,
    ended_at: str,
    rounds_done: int,
    new_added: int,
    total_posts_after: int,
    stop_reason: str,
) -> None:
    conn.execute(
        """
        UPDATE runs
        SET ended_at=?, rounds_done=?, new_added=?, total_posts_after=?, stop_reason=?
        WHERE id=?
        """,
        (ended_at, rounds_done, new_added, total_posts_after, stop_reason, run_id),
    )
    conn.commit()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    ensure_dir(output_dir)

    db_path = Path(args.db_path) if args.db_path else output_dir / DEFAULT_DB_NAME
    csv_path = output_dir / "binance_square_posts.csv"
    raw_json_path = output_dir / "binance_square_posts_raw.json"
    run_summary_path = output_dir / "crawler_v2_last_run.json"

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    init_db(conn)

    started_at = now_text()
    run_row = conn.execute(
        """
        INSERT INTO runs(started_at, lang, target_posts, max_scroll_rounds, idle_stop_rounds)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            started_at,
            args.lang,
            int(args.target_posts),
            int(args.max_scroll_rounds),
            int(args.idle_stop_rounds),
        ),
    )
    conn.commit()
    run_id = int(run_row.lastrowid)

    existing_before = count_posts(conn)
    print(f"[v2] existing unique posts in db={existing_before}")

    if args.check_only:
        playwright_obj, context = create_browser_context(
            headless=args.headless,
            user_data_dir=args.user_data_dir,
        )
        try:
            page = context.new_page()
            square_url = SQUARE_HOME_URL_TEMPLATE.format(lang=args.lang)
            print(f"[check] open {square_url}")
            page.goto(square_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            print("[check] crawler_v2 browser flow is ready")
            update_run_end(
                conn=conn,
                run_id=run_id,
                ended_at=now_text(),
                rounds_done=0,
                new_added=0,
                total_posts_after=existing_before,
                stop_reason="check_only",
            )
            return
        finally:
            safe_close_browser(playwright_obj, context)
            conn.close()

    if existing_before >= args.target_posts:
        exported = export_posts(conn, csv_path, raw_json_path, args.export_limit)
        summary = {
            "started_at": started_at,
            "ended_at": now_text(),
            "target_posts": int(args.target_posts),
            "existing_before": int(existing_before),
            "new_added": 0,
            "total_posts_after": int(existing_before),
            "rounds_done": 0,
            "stop_reason": "target_already_reached",
            "db_path": str(db_path),
            "exported_rows": int(exported),
            "csv": str(csv_path),
            "raw_json": str(raw_json_path),
        }
        run_summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        update_run_end(
            conn=conn,
            run_id=run_id,
            ended_at=summary["ended_at"],
            rounds_done=0,
            new_added=0,
            total_posts_after=existing_before,
            stop_reason="target_already_reached",
        )
        print("[v2] target already reached in existing DB, exported snapshot and exited")
        conn.close()
        return

    playwright_obj, context = create_browser_context(
        headless=args.headless,
        user_data_dir=args.user_data_dir,
    )

    rounds_done = 0
    new_added_total = 0
    idle_rounds = 0
    stop_reason = "max_scroll_rounds_reached"
    start_epoch = time.time()

    try:
        page = context.new_page()
        square_url = SQUARE_HOME_URL_TEMPLATE.format(lang=args.lang)
        print(f"[square-home-v2] open {square_url}")
        page.goto(square_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)

        if args.wait_for_login:
            input(
                "[login] Square opened. Complete login in browser, then press Enter to start incremental collection..."
            )
            page.wait_for_timeout(1500)

        for round_idx in range(1, int(args.max_scroll_rounds) + 1):
            rounds_done = round_idx
            round_urls = fetch_visible_post_urls(page)
            new_added = insert_or_touch_posts(conn, round_urls, now_text())
            total_now = count_posts(conn)

            new_added_total += new_added
            if new_added == 0:
                idle_rounds += 1
            else:
                idle_rounds = 0

            if (
                round_idx == 1
                or round_idx % max(1, int(args.checkpoint_every)) == 0
                or new_added > 0
            ):
                print(
                    f"[square-home-v2] round={round_idx}/{args.max_scroll_rounds} "
                    f"visible={len(round_urls)} new_added={new_added} "
                    f"idle_rounds={idle_rounds}/{args.idle_stop_rounds} total_unique={total_now}/{args.target_posts}"
                )

            if total_now >= args.target_posts:
                stop_reason = "target_reached"
                break

            if idle_rounds >= int(args.idle_stop_rounds):
                stop_reason = "idle_stop_reached"
                break

            if args.max_runtime_minutes > 0:
                elapsed_minutes = (time.time() - start_epoch) / 60.0
                if elapsed_minutes >= float(args.max_runtime_minutes):
                    stop_reason = "runtime_limit_reached"
                    break

            page.mouse.wheel(0, int(args.scroll_pixels))
            page.wait_for_timeout(int(max(0.3, float(args.pause_seconds)) * 1000))

    finally:
        safe_close_browser(playwright_obj, context)

    total_after = count_posts(conn)
    exported = export_posts(conn, csv_path, raw_json_path, args.export_limit)
    ended_at = now_text()

    summary = {
        "started_at": started_at,
        "ended_at": ended_at,
        "target_posts": int(args.target_posts),
        "existing_before": int(existing_before),
        "new_added": int(new_added_total),
        "total_posts_after": int(total_after),
        "rounds_done": int(rounds_done),
        "stop_reason": stop_reason,
        "db_path": str(db_path),
        "exported_rows": int(exported),
        "csv": str(csv_path),
        "raw_json": str(raw_json_path),
    }
    run_summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    update_run_end(
        conn=conn,
        run_id=run_id,
        ended_at=ended_at,
        rounds_done=rounds_done,
        new_added=new_added_total,
        total_posts_after=total_after,
        stop_reason=stop_reason,
    )
    conn.close()

    print(
        f"[v2] done: stop_reason={stop_reason} new_added={new_added_total} "
        f"total_unique={total_after} exported={exported}"
    )


if __name__ == "__main__":
    main()
