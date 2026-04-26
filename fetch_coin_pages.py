#!/usr/bin/env python3
"""
fetch_coin_pages.py — 为 crawler_coin.py 采集的帖子下载 HTML 页面。

专门读取 coin_posts.db，支持按币种过滤、评论数过滤等。
与 fetch_pages_from_db.py 分开，保持原有代码不变。

用法:
  # 下载所有帖子
  python fetch_coin_pages.py --db-path crawler_coin_output/coin_posts.db

  # 只下载 BTC 相关的帖子
  python fetch_coin_pages.py --symbol BTC --limit 50

  # 有浏览器窗口的调试模式
  python fetch_coin_pages.py --symbol ETH --headless false

  # 检查连通性
  python fetch_coin_pages.py --check-only
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from crawler_util import ensure_dir
from crawler_util import extract_richtext_text

# playwright 惰性导入
sync_playwright = None

DEFAULT_USER_DATA_DIR = "tmp_chrome_profile"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="fetch_coin_pages — 下载币种相关帖子的 HTML 页面"
    )
    parser.add_argument(
        "--db-path",
        default="crawler_coin_output/coin_posts.db",
        help="coin_posts.db 文件路径，默认 crawler_coin_output/coin_posts.db",
    )
    parser.add_argument(
        "--output-dir",
        default="crawler_coin_output/html_pages",
        help="HTML 文件输出目录，默认 crawler_coin_output/html_pages",
    )
    parser.add_argument(
        "--symbol",
        help="过滤币种，如 BTC（留空则不过滤）",
    )
    parser.add_argument(
        "--min-comment-count",
        type=int,
        default=0,
        help="最低评论数，默认 0（不过滤）",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="最多采集帖子数，0=全部"
    )
    parser.add_argument(
        "--offset", type=int, default=0, help="从第几条开始（配合 limit 实现分页）"
    )
    parser.add_argument(
        "--pause-seconds", type=float, default=1.0, help="页面间等待秒数"
    )
    parser.add_argument(
        "--headless",
        default=True,
        action="store_true",
        help="浏览器无头模式（默认）",
    )
    parser.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        help="浏览器可见（调试模式）",
    )
    parser.add_argument(
        "--user-data-dir",
        default=DEFAULT_USER_DATA_DIR,
        help=f"持久化 Chrome 用户数据目录，默认 {DEFAULT_USER_DATA_DIR}",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖已存在的 HTML 文件",
    )
    parser.add_argument(
        "--timeout-seconds", type=int, default=30, help="页面超时秒数"
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="仅检查 DB 和浏览器连通性",
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="随机打乱顺序采集（避免模式被检测）",
    )
    return parser.parse_args()


def create_browser_context(
    headless: bool = True, user_data_dir: str = DEFAULT_USER_DATA_DIR
) -> tuple[Any, Any]:
    """创建 Playwright Chromium 浏览器上下文。"""
    global sync_playwright
    if sync_playwright is None:
        try:
            from playwright.sync_api import sync_playwright as sp
            sync_playwright = sp
        except ImportError:
            raise RuntimeError(
                "playwright is not installed. Run commands:\n"
                "  pip install playwright\n"
                "  playwright install chromium"
            )

    playwright = sync_playwright().start()
    if user_data_dir:
        print(f"[browser] 使用持久化用户数据: {user_data_dir}")
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=headless,
            viewport={"width": 1280, "height": 1600},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        return playwright, context

    print("[browser] 使用临时用户数据")
    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context(
        viewport={"width": 1280, "height": 1600},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
    )
    return playwright, context


def safe_close_browser(playwright_obj: Any, context: Any) -> None:
    """安全关闭浏览器。"""
    try:
        context.close()
    finally:
        playwright_obj.stop()


def load_coin_posts(
    db_path: Path,
    symbol: str | None,
    min_comment_count: int,
    limit: int,
    offset: int,
    shuffle: bool = False,
) -> list[dict[str, str]]:
    """从 coin_posts 表加载帖子。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conditions = ["1=1"]
        params: list[Any] = []

        if symbol:
            conditions.append("(related_symbols LIKE ? OR related_symbols LIKE ?)")
            params.extend([f"%{symbol}%", f"%{symbol}]%"])

        if min_comment_count > 0:
            conditions.append("comment_count >= ?")
            params.append(int(min_comment_count))

        where_clause = " AND ".join(conditions)

        order_clause = (
            "RANDOM()" if shuffle else "first_seen_at ASC"
        )

        query = (
            f"SELECT post_id, link FROM coin_posts "
            f"WHERE {where_clause} "
            f"ORDER BY {order_clause}"
        )

        if limit > 0:
            query += " LIMIT ? OFFSET ?"
            params.extend([int(limit), int(offset)])
        elif offset > 0:
            # SQLite requires LIMIT when OFFSET is used.
            query += " LIMIT -1 OFFSET ?"
            params.append(int(offset))

        rows = conn.execute(query, params).fetchall()
        return [
            {
                "post_id": str(r["post_id"]),
                "link": str(r["link"]),
            }
            for r in rows
        ]
    finally:
        conn.close()


def check_db(db_path: Path) -> int:
    """检查数据库连接和表结构。"""
    if not db_path.exists():
        raise FileNotFoundError(f"数据库文件不存在: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # 检查表是否存在
        tables = conn.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='coin_posts'
        """).fetchall()
        if not tables:
            raise ValueError("数据库中没有 coin_posts 表（可能是 crawler_coin.py 的数据库吗？）")
        # 检查列
        count = conn.execute("SELECT COUNT(1) FROM coin_posts").fetchone()[0]
        print(f"[check] coin_posts 表中有 {count} 条记录")
        return int(count)
    finally:
        conn.close()


def sanitize_filename(post_id: str) -> str:
    """清洗文件名，防止不合法字符。"""
    # 保留字母数字、下划线、点、连字符
    safe = re.sub(r"[^\w\-.]", "_", post_id)
    # 去掉连续的 _
    safe = re.sub(r"_+", "_", safe)
    # 去掉首尾的 .
    safe = safe.strip(".")
    return safe or "post"


def download_page(
    context: Any,
    post: dict[str, str],
    output_dir: Path,
    overwrite: bool,
    timeout: int,
) -> tuple[bool, str | None]:
    """下载单个帖子的 HTML，同时通过 response 拦截捕获评论 API 数据。"""

    post_id = post["post_id"]
    link = post["link"]
    safe_id = sanitize_filename(post_id)
    filename = safe_id + ".html"
    file_path = output_dir / filename
    comments_path = output_dir / f"{safe_id}_comments.json"

    if file_path.exists() and not overwrite:
        print(f"  [{post_id}] 文件已存在: {filename}")
        return True, str(file_path)

    if not link or "binance.com" not in link:
        print(f"  [{post_id}] 链接无效或不是币安链接: {link}")
        return False, None

    try:
        page = context.new_page()
        page.set_default_timeout(timeout * 1000)

        # 拦截所有 bapi/gateway-api 响应，保存原始数据用于分析
        api_raw_payloads: list[dict[str, Any]] = []

        def on_response(response: Any) -> None:
            u = response.url.lower()
            if "/bapi/" not in u and "/gateway-api/" not in u:
                return
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    api_raw_payloads.append({"url": response.url, "status": response.status, "data": payload})
                    print(f"    [api] {response.status} {u[:150]}")
            except Exception:
                print(f"    [api] non-json {response.status} {u[:150]}")

        page.on("response", on_response)

        print(f"  [{post_id}] 访问: {link}")
        page.goto(link, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)

        # 点击 "Replies" tab 触发评论加载
        try:
            replies_tab = page.locator("div[role='tab']:has-text('Replies')")
            if replies_tab.count() > 0:
                print(f"  [{post_id}] 点击 Replies tab")
                replies_tab.first.click(timeout=3000)
                page.wait_for_timeout(3000)
            else:
                print(f"  [{post_id}] 未找到 Replies tab，可能无评论")
        except Exception as e:
            print(f"  [{post_id}] 点击 Replies tab 失败: {e}")

        html = page.content()
        file_path.write_text(html, encoding="utf-8")
        print(f"  [{post_id}] 已保存: {filename} ({len(html)} bytes)")

        # 保存所有捕获到的原始 API 数据到 debug 文件
        if api_raw_payloads:
            debug_path = output_dir / f"{safe_id}_api_raw.json"
            debug_path.write_text(
                json.dumps(api_raw_payloads, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"  [{post_id}] 已保存 {len(api_raw_payloads)} 个原始 API 响应: {debug_path.name}")

            # 从 /replyPost/list 响应中提取用户评论
            comments = []
            for api_resp in api_raw_payloads:
                if "/replyPost/list" not in api_resp["url"]:
                    continue
                if api_resp["status"] != 200:
                    continue
                items = api_resp.get("data", {}).get("data")
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    # 只提取用户回复 (sourceType=5)，过滤媒体转发 (sourceType=4)
                    if item.get("sourceType") != 5:
                        continue
                    body_raw = item.get("body", "")
                    text = extract_richtext_text(body_raw)
                    if not text:
                        continue
                    author = item.get("displayName") or item.get("username") or ""
                    comment_id = str(item.get("id", ""))
                    time_ms = item.get("firstReleaseTime") or item.get("createTime") or 0
                    comments.append({
                        "comment_id": comment_id,
                        "comment_author": author,
                        "comment_text": text,
                        "comment_time": str(time_ms),
                    })
            if comments:
                comments_path.write_text(
                    json.dumps(comments, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"  [{post_id}] 已保存 {len(comments)} 条用户评论: {comments_path.name}")
            else:
                print(f"  [{post_id}] 未从 replyPost/list 中提取到用户评论")
        else:
            print(f"  [{post_id}] 未捕获到任何 bapi 响应")

        return True, str(file_path)

    except Exception as exc:
        print(f"  [{post_id}] 下载失败: {exc}")
        return False, None


def main() -> None:
    args = parse_args()
    db_path = Path(args.db_path)
    output_dir = Path(args.output_dir)
    ensure_dir(output_dir)

    # 检查数据库
    total = check_db(db_path)
    if args.check_only:
        print("[check] DB 正常，退出检查模式")
        return

    # 加载帖子
    posts = load_coin_posts(
        db_path=db_path,
        symbol=args.symbol,
        min_comment_count=args.min_comment_count,
        limit=args.limit,
        offset=args.offset,
        shuffle=args.shuffle,
    )
    if not posts:
        print("[fetch-coin] 没有符合条件的帖子")
        return

    print(f"[fetch-coin] 准备下载 {len(posts)} 个帖子")
    if args.symbol:
        print(f"[fetch-coin] 币种过滤: {args.symbol}")
    if args.min_comment_count > 0:
        print(f"[fetch-coin] 最低评论数: {args.min_comment_count}")

    # 启动浏览器
    playwright_obj, context = create_browser_context(
        headless=args.headless,
        user_data_dir=args.user_data_dir,
    )

    try:
        ok_count = 0
        failed_count = 0

        for i, post in enumerate(posts, start=1):
            success, _ = download_page(
                context=context,
                post=post,
                output_dir=output_dir,
                overwrite=args.overwrite,
                timeout=args.timeout_seconds,
            )
            if success:
                ok_count += 1
            else:
                failed_count += 1

            # 进度显示
            if i % 5 == 0 or i == len(posts):
                print(f"[fetch-coin] 进度: {i}/{len(posts)} 成功={ok_count} 失败={failed_count}")

            # 请求间隔（除了最后一条）
            if i < len(posts):
                time.sleep(max(0.1, args.pause_seconds))

        print(f"[fetch-coin] 完成: 总 {len(posts)} 条, 成功 {ok_count} 条, 失败 {failed_count} 条")

    except KeyboardInterrupt:
        print("[fetch-coin] 用户中断")
    except Exception as exc:
        print(f"[fetch-coin] 发生错误: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        safe_close_browser(playwright_obj, context)


if __name__ == "__main__":
    main()