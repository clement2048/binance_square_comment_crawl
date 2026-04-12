from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import time
from pathlib import Path
from typing import Any

import requests
from requests import RequestException

try:
    from playwright.sync_api import BrowserContext
    from playwright.sync_api import Page
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    BrowserContext = Any  # type: ignore[assignment]
    Page = Any  # type: ignore[assignment]
    sync_playwright = None


BINANCE_NEWS_API = "https://www.binance.com/bapi/composite/v4/friendly/pgc/feed/news/list"
DEFAULT_OUTPUT_DIR = Path("update_news")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取币安广场帖子列表以及评论，并导出 CSV/JSON 文件。"
    )
    parser.add_argument("--pages", type=int, default=3, help="抓取帖子列表的页数，默认 3")
    parser.add_argument("--page-size", type=int, default=20, help="每页帖子数，默认 20")
    parser.add_argument("--max-posts", type=int, default=50, help="最多处理多少条帖子，默认 50")
    parser.add_argument(
        "--max-comments",
        type=int,
        default=30,
        help="每条帖子最多抓取多少条评论，默认 30",
    )
    parser.add_argument(
        "--min-comment-count",
        type=int,
        default=1,
        help="只处理评论数不小于该值的帖子，默认 1",
    )
    parser.add_argument("--lang", default="zh-CN", help="页面语言，默认 zh-CN")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="使用无头浏览器抓取评论，默认关闭，便于调试",
    )
    parser.add_argument(
        "--skip-comments",
        action="store_true",
        help="只抓帖子，不抓评论",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"输出目录，默认 {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=20,
        help="HTTP 请求超时秒数，默认 20",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=1.2,
        help="抓取每条帖子评论之间的等待时间，默认 1.2 秒",
    )
    parser.add_argument(
        "--user-data-dir",
        default="",
        help="可选。传入 Chromium 用户目录，便于复用登录态",
    )
    parser.add_argument(
        "--trust-env-proxy",
        action="store_true",
        help="默认不读取系统代理环境变量；如果你确定代理可用，再显式开启",
    )
    return parser.parse_args()


def build_session(lang: str) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/135.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": f"{lang},{lang.split('-')[0]};q=0.9",
            "Origin": "https://www.binance.com",
            "Referer": f"https://www.binance.com/{lang}/square/news/all",
            "clienttype": "web",
            "lang": lang,
        }
    )
    return session


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def timestamp_to_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    number = int(value)
    if number > 10**12:
        date_value = dt.datetime.fromtimestamp(number / 1000)
    else:
        date_value = dt.datetime.fromtimestamp(number)
    return date_value.strftime("%Y-%m-%d %H:%M:%S")


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def is_meaningful_comment(text: str) -> bool:
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

    if re.fullmatch(r"[\d\s,.:/+-]+", text):
        return False
    if re.fullmatch(r"\d+[smhdw]", lowered):
        return False
    if re.fullmatch(r"\d+[秒分钟小时天周月年]前", text):
        return False

    return True


def fetch_posts(
    session: requests.Session,
    pages: int,
    page_size: int,
    timeout: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        params = {
            "pageIndex": page,
            "pageSize": page_size,
            "strategy": 6,
            "tagId": 0,
            "featured": "false",
        }
        response = session.get(BINANCE_NEWS_API, params=params, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        items = payload.get("data", {}).get("vos", [])
        print(f"[posts] page={page} items={len(items)}")
        if not items:
            break
        rows.extend(items)
    return rows


def normalize_post(item: dict[str, Any]) -> dict[str, Any]:
    title = clean_text(item.get("title", ""))
    subtitle = clean_text(item.get("subTitle", ""))
    content = clean_text(" ".join(part for part in [title, subtitle] if part))
    return {
        "post_id": str(item.get("id", "")),
        "time": timestamp_to_text(item.get("date")),
        "title": title,
        "subtitle": subtitle,
        "content": content,
        "author": clean_text(item.get("authorName", "")),
        "like_count": item.get("likeCount", 0),
        "comment_count": item.get("commentCount", 0),
        "view_count": item.get("viewCount", 0),
        "share_count": item.get("shareCount", 0),
        "link": item.get("webLink", ""),
    }


def dedupe_rows(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        value = str(row.get(key, ""))
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(row)
    return result


def extract_comment_texts_from_payload(payload: Any) -> list[str]:
    candidate_keys = {
        "content",
        "comment",
        "commentcontent",
        "commenttext",
        "text",
        "message",
        "body",
    }
    results: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and key.lower() in candidate_keys:
                    text = clean_text(value)
                    if is_meaningful_comment(text):
                        results.append(text)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return results


def create_browser_context(headless: bool, user_data_dir: str) -> tuple[Any, BrowserContext]:
    if sync_playwright is None:
        raise RuntimeError(
            "未安装 playwright。请先执行: pip install playwright && playwright install chromium"
        )

    playwright = sync_playwright().start()
    if user_data_dir:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=headless,
            viewport={"width": 1366, "height": 1600},
        )
        return playwright, context

    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context(viewport={"width": 1366, "height": 1600})
    return playwright, context


def safe_close_browser(playwright_obj: Any, context: BrowserContext) -> None:
    try:
        context.close()
    finally:
        playwright_obj.stop()


def click_comment_entry(page: Page) -> bool:
    selectors = [
        "button:has-text('评论')",
        "button:has-text('Comment')",
        "[data-testid*='comment']",
        "[class*='comment-btn']",
        "[class*='commentButton']",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                locator.click(timeout=2500)
                page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False


def extract_comments_from_dom(page: Page) -> list[str]:
    selectors = [
        "[data-testid*='comment-content']",
        "[class*='comment-content']",
        "[class*='CommentContent']",
        "[class*='comment-item']",
        "[class*='CommentItem']",
        "[class*='commentItem']",
        "[class*='comment']",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if locator.count() == 0:
                continue
            results: list[str] = []
            for block in locator.all_inner_texts():
                for line in block.splitlines():
                    text = clean_text(line)
                    if is_meaningful_comment(text):
                        results.append(text)
            if results:
                return results
        except Exception:
            continue
    return []


def fetch_comments_for_posts(
    posts: list[dict[str, Any]],
    max_comments: int,
    headless: bool,
    pause_seconds: float,
    user_data_dir: str,
) -> list[dict[str, Any]]:
    playwright_obj, context = create_browser_context(
        headless=headless,
        user_data_dir=user_data_dir,
    )
    page = context.new_page()
    rows: list[dict[str, Any]] = []

    try:
        for index, post in enumerate(posts, start=1):
            url = post["link"]
            post_id = post["post_id"]
            api_comments: list[str] = []
            api_hits = 0

            def on_response(response: Any) -> None:
                nonlocal api_hits
                if "comment" not in response.url.lower():
                    return
                try:
                    payload = response.json()
                except Exception:
                    return
                api_hits += 1
                api_comments.extend(extract_comment_texts_from_payload(payload))

            page.on("response", on_response)
            print(f"[comments] {index}/{len(posts)} {url}")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                clicked = click_comment_entry(page)
                if not clicked:
                    print("[comments] comment entry not found, continue with scrolling fallback")

                for _ in range(5):
                    page.mouse.wheel(0, 2400)
                    page.wait_for_timeout(1200)

                if api_comments:
                    raw_comments = api_comments
                else:
                    raw_comments = extract_comments_from_dom(page)

                unique_comments: list[str] = []
                seen: set[str] = set()
                for text in raw_comments:
                    normalized = clean_text(text)
                    if not is_meaningful_comment(normalized) or normalized in seen:
                        continue
                    seen.add(normalized)
                    unique_comments.append(normalized)
                    if len(unique_comments) >= max_comments:
                        break

                if not unique_comments:
                    print(f"[comments] no comment captured for post_id={post_id}, api_hits={api_hits}")

                for comment_index, comment_text in enumerate(unique_comments, start=1):
                    rows.append(
                        {
                            "post_id": post_id,
                            "comment_id": f"{post_id}_{comment_index}",
                            "comment_text": comment_text,
                            "source_url": url,
                        }
                    )
            finally:
                page.remove_listener("response", on_response)
                time.sleep(pause_seconds)
    finally:
        safe_close_browser(playwright_obj, context)

    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_post_comment_rows(
    posts: list[dict[str, Any]],
    comments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    post_map = {item["post_id"]: item for item in posts}
    merged: list[dict[str, Any]] = []
    for comment in comments:
        post = post_map.get(comment["post_id"], {})
        merged.append(
            {
                "post_id": comment["post_id"],
                "comment_id": comment["comment_id"],
                "comment_text": comment["comment_text"],
                "source_url": comment["source_url"],
                "post_time": post.get("time", ""),
                "post_title": post.get("title", ""),
                "post_content": post.get("content", ""),
                "post_link": post.get("link", ""),
            }
        )
    return merged


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    ensure_dir(output_dir)

    posts_csv_path = output_dir / "binance_square_posts.csv"
    comments_csv_path = output_dir / "binance_square_comments.csv"
    merged_csv_path = output_dir / "binance_square_posts_comments.csv"
    raw_json_path = output_dir / "binance_square_posts_raw.json"

    session = build_session(args.lang)
    if args.trust_env_proxy:
        session.trust_env = True
    try:
        raw_posts = fetch_posts(
            session=session,
            pages=args.pages,
            page_size=args.page_size,
            timeout=args.request_timeout,
        )
    except RequestException as exc:
        raise SystemExit(
            "帖子列表抓取失败。"
            "如果你本机需要走代理，请加上 --trust-env-proxy；"
            "如果你本机代理是坏的，保持默认即可。"
            f"\n原始错误: {exc}"
        ) from exc
    raw_posts = raw_posts[: args.max_posts]
    write_json(raw_json_path, raw_posts)

    normalized_posts = dedupe_rows(
        [normalize_post(item) for item in raw_posts],
        key="post_id",
    )
    write_csv(
        posts_csv_path,
        normalized_posts,
        fieldnames=[
            "post_id",
            "time",
            "title",
            "subtitle",
            "content",
            "author",
            "like_count",
            "comment_count",
            "view_count",
            "share_count",
            "link",
        ],
    )
    print(f"[ok] posts saved: {posts_csv_path} ({len(normalized_posts)} rows)")

    if args.skip_comments:
        comment_rows: list[dict[str, Any]] = []
        print("[ok] skip comment crawling by --skip-comments")
    else:
        candidate_posts = [
            post
            for post in normalized_posts
            if post["link"] and int(post.get("comment_count", 0) or 0) >= args.min_comment_count
        ]
        print(
            "[comments] candidate posts="
            f"{len(candidate_posts)} min_comment_count={args.min_comment_count}"
        )
        comment_rows = fetch_comments_for_posts(
            posts=candidate_posts,
            max_comments=args.max_comments,
            headless=args.headless,
            pause_seconds=args.pause_seconds,
            user_data_dir=args.user_data_dir,
        )

    write_csv(
        comments_csv_path,
        comment_rows,
        fieldnames=["post_id", "comment_id", "comment_text", "source_url"],
    )
    print(f"[ok] comments saved: {comments_csv_path} ({len(comment_rows)} rows)")

    merged_rows = merge_post_comment_rows(normalized_posts, comment_rows)
    write_csv(
        merged_csv_path,
        merged_rows,
        fieldnames=[
            "post_id",
            "comment_id",
            "comment_text",
            "source_url",
            "post_time",
            "post_title",
            "post_content",
            "post_link",
        ],
    )
    print(f"[ok] merged saved: {merged_csv_path} ({len(merged_rows)} rows)")


if __name__ == "__main__":
    main()
