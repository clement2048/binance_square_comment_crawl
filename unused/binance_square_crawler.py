from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path
from typing import Any

import requests
from requests import RequestException
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from crawler_comment import click_comment_entry, extract_comment_cards_from_dom, extract_comment_rows_from_payload, extract_comments_from_dom, extract_post_meta_from_page
from config import CRAWLER_ARG_DEFINITIONS

from crawler_util import clean_text
from crawler_util import dump_page_content
from crawler_util import ensure_dir
from crawler_util import timestamp_to_text

try:
    from playwright.sync_api import BrowserContext
    from playwright.sync_api import Page
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    BrowserContext = Any  # type: ignore[assignment]
    Page = Any  # type: ignore[assignment]
    sync_playwright = None

SQUARE_HOME_URL_TEMPLATE = "https://www.binance.com/{lang}/square"
COMMENT_DEBUG_DIR_NAME = "binance_square_comment_debug"
PAGE_DUMP_DIR_NAME = "binance_square_page_dump"


"""
解析并返回所有命令行参数的配置对象，该对象控制爬虫的核心行为。

该函数定义了完整的爬虫配置接口，包括帖子来源选择、抓取数量限制、浏览器设置、
登录控制、输出配置等，支持API和首页滚动两种抓取模式。

返回值:
    argparse.Namespace: 包含所有命令行参数值的对象，这些值将在整个爬虫流程中使用
"""
def parse_args() -> argparse.Namespace:
    """解析命令行参数，返回包含所有抓取配置（页数、帖子数、评论数、是否等待登录等）的命名空间对象。"""
    parser = argparse.ArgumentParser(
        description="抓取币安广场帖子列表以及评论，并导出 CSV/JSON 文件。"
    )
    for arg in CRAWLER_ARG_DEFINITIONS:
        parser.add_argument(*arg["flags"], **arg["kwargs"])
    return parser.parse_args()


"""
构建并配置用于API请求的requests.Session对象，包含重试机制、头部信息和代理设置。

该会话配置了自动重试（针对500系列错误和429限流），设置了Binance网站所需的完整HTTP头部，
包括User-Agent、Accept-Language等，并提供了代理控制选项。

参数:
    lang: 语言代码，如'zh-CN'，用于设置Accept-Language头
    retries: HTTP请求失败时的重试次数

返回值:
    requests.Session: 配置好的请求会话，用于后续的HTTP调用
"""
def build_session(lang: str, retries: int) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
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


"""
通过API分页获取Binance Square帖子列表。

调用Binance官方新闻流接口，按指定页数和每页数量爬取帖子数据。
使用增量策略：遇到空页时提前停止，避免不必要的请求。
这是"news"模式的核心数据来源函数。

参数:
    session: 配置好的HTTP会话对象
    news_api: API端点URL
    pages: 最大爬取页数
    page_size: 每页帖子数量
    timeout: 请求超时时间（秒）

返回值:
    list[dict]: 原始API响应中的帖子数据列表，每个元素包含完整的帖子信息
"""
def fetch_posts(
    session: requests.Session,
    news_api: str,
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
        response = session.get(news_api, params=params, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        items = payload.get("data", {}).get("vos", [])
        print(f"[posts] page={page} items={len(items)}")
        if not items:
            break
        rows.extend(items)
    return rows


"""
测试API连接性和网络可达性，使用最小请求验证配置是否正确。

这是网络检查模式（--check-only）的核心函数，通过一次小型请求验证：
1. 网络是否可以访问Binance API；2. 代理配置是否正确；3. API响应格式是否符合预期。

参数:
    session: 配置好的HTTP会话对象
    news_api: API端点URL
    timeout: 请求超时时间（秒）

返回值:
    dict: 包含连接状态信息的字典：ok(是否成功)、status_code(HTTP状态码)、
          sample_count(采样帖子数)、url(实际请求URL)
"""
def check_connectivity(session: requests.Session, news_api: str, timeout: int) -> dict[str, Any]:
    params = {
        "pageIndex": 1,
        "pageSize": 1,
        "strategy": 6,
        "tagId": 0,
        "featured": "false",
    }
    response = session.get(news_api, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    items = payload.get("data", {}).get("vos", [])
    return {
        "ok": True,
        "status_code": response.status_code,
        "sample_count": len(items),
        "url": response.url,
    }


"""
将原始API帖子数据标准化为统一的结构化格式。

处理API返回的原始JSON，提取关键字段并进行清理，将不同格式的时间戳、
作者信息、统计数值等转换为一致的格式，用于后续的CSV导出。
这是数据清洗和标准化的关键转换层。

参数:
    item: 原始API响应中的一个帖子数据字典
    
返回值:
    dict: 标准化的帖子信息，包含post_id, time, title, subtitle, content,
          author, author_username, like_count, comment_count, view_count,
          share_count, related_symbols, link等字段
"""
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
        "author_username": clean_text(
            item.get("authorUserName", "") or item.get("authorCode", "") or item.get("authorId", "")
        ),
        "like_count": item.get("likeCount", 0),
        "comment_count": item.get("commentCount", 0),
        "view_count": item.get("viewCount", 0),
        "share_count": item.get("shareCount", 0),
        "related_symbols": "",
        "link": item.get("webLink", ""),
    }


def build_minimal_post_from_url(url: str) -> dict[str, Any]:
    post_id = url.rstrip("/").split("/")[-1]
    return {
        "post_id": post_id,
        "time": "",
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
        "link": url,
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


"""创建并启动浏览器上下文（支持持久化用户目录以复用登录态）。"""
def create_browser_context(headless: bool, user_data_dir: str) -> tuple[Any, Any]:
    if sync_playwright is None:
        raise RuntimeError(
            "未安装 playwright。请先执行: pip install playwright && playwright install chromium"
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


"""
使用浏览器直接采集Binance Square首页的帖子链接。

替代API模式，通过模拟用户滚动浏览首页来抓取帖子链接。
核心流程：1) 打开首页；2) 手动登录（如需要）；3) 滚动并收集帖子链接；
4) 构建最小帖子信息。这是"square-home"来源模式的核心实现。

参数:
    lang: 页面语言代码
    max_posts: 最大采集帖子数
    headless: 是否无头模式
    pause_seconds: 滚动间隔时间
    user_data_dir: Chromium用户数据目录
    wait_for_login: 是否等待手动登录
    
返回值:
    list[dict]: 包含基本信息（post_id, link）的帖子字典列表
"""
def collect_posts_from_square_home(
    lang: str,
    max_posts: int,
    headless: bool,
    pause_seconds: float,
    user_data_dir: str,
    wait_for_login: bool,
) -> list[dict[str, Any]]:
    playwright_obj, context = create_browser_context(
        headless=headless,
        user_data_dir=user_data_dir,
    )
    page = context.new_page()
    square_url = SQUARE_HOME_URL_TEMPLATE.format(lang=lang)

    try:
        print(f"[square-home] open {square_url}")
        page.goto(square_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        if wait_for_login:
            input(
                "[login] 已打开币安广场首页。请先在浏览器里完成登录，"
                "完成后回到终端按回车继续采集帖子..."
            )
            page.wait_for_timeout(1500)

        collected: list[str] = []
        seen: set[str] = set()

        for _ in range(12):
            try:
                hrefs = page.locator("a[href*='/square/post/']").evaluate_all(
                    "(els) => els.map(el => el.href).filter(Boolean)"
                )
            except Exception:
                hrefs = []

            for href in hrefs:
                if "/square/post/" not in href or href in seen:
                    continue
                seen.add(href)
                collected.append(href)
                if len(collected) >= max_posts:
                    break

            if len(collected) >= max_posts:
                break

            page.mouse.wheel(0, 2600)
            page.wait_for_timeout(int(max(0.8, pause_seconds) * 1000))

        print(f"[square-home] collected post urls={len(collected)}")
        return [build_minimal_post_from_url(url) for url in collected[:max_posts]]
    finally:
        safe_close_browser(playwright_obj, context)


def fetch_comments_for_posts(
    posts: list[dict[str, Any]],
    max_comments: int,
    headless: bool,
    pause_seconds: float,
    user_data_dir: str,
    wait_for_login: bool,
    comment_debug_dir: Path | None,
    page_dump_dir: Path | None,
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
            api_comments: list[dict[str, Any]] = []
            api_hits = 0
            debug_index = 0

            def on_response(response: Any) -> None:
                nonlocal api_hits, debug_index
                if "comment" not in response.url.lower():
                    return
                try:
                    payload = response.json()
                except Exception:
                    return
                api_hits += 1
                if comment_debug_dir is not None:
                    debug_index += 1
                    debug_path = comment_debug_dir / f"{post_id}_{debug_index}.json"
                    debug_path.write_text(
                        json.dumps({"url": response.url, "payload": payload}, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                api_comments.extend(
                    extract_comment_rows_from_payload(
                        payload=payload,
                        post_id=post_id,
                        source_url=url,
                        max_comments=max_comments,
                    )
                )

            page.on("response", on_response)
            print(f"[comments] {index}/{len(posts)} {url}")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                print(f"[comments] opened post_id={post_id}")
                if page_dump_dir is not None:
                    dump_page_content(page=page, dump_dir=page_dump_dir, post_id=post_id)
                    print(f"[dump] saved page dump for post_id={post_id}")
                if wait_for_login and index == 1:
                    input(
                        "[login] 已打开第一条帖子。请确认当前浏览器里已经登录且能看到评论区，"
                        "然后回到终端按回车继续抓评论..."
                    )
                    page.wait_for_timeout(1500)
                    if page_dump_dir is not None:
                        dump_page_content(
                            page=page,
                            dump_dir=page_dump_dir,
                            post_id=f"{post_id}_after_login",
                        )
                        print(f"[dump] saved page dump after login for post_id={post_id}")
                post_meta = extract_post_meta_from_page(page)
                if post_meta["title"] and not post.get("title"):
                    post["title"] = post_meta["title"]
                if post_meta["content"] and not post.get("content"):
                    post["content"] = post_meta["content"]
                if post_meta["author"] and not post.get("author"):
                    post["author"] = post_meta["author"]
                if post_meta["author_username"] and not post.get("author_username"):
                    post["author_username"] = post_meta["author_username"]
                if post_meta["related_symbols"]:
                    post["related_symbols"] = post_meta["related_symbols"]
                clicked = click_comment_entry(page)
                if clicked:
                    print(f"[comments] clicked comment entry for post_id={post_id}")

                for _ in range(5):
                    page.mouse.wheel(0, 2400)
                    page.wait_for_timeout(1200)

                # Expand as many replies/collapsed comments as possible.
                try:
                    expand_selectors = [
                        "button:has-text('Show More Replies')",
                        "button:has-text('Show collapsed comments')",
                        "button:has-text('Show Collapsed Comments')",
                        "button:has-text('Show more comments')",
                        "button:has-text('Show More Comments')",
                        "button:has-text('More Replies')",
                        "button:has-text('More comments')",
                        "button:has-text('More Comments')",
                        "button:has-text('查看更多回复')",
                        "button:has-text('展示被折叠的评论')",
                        "button:has-text('显示被折叠的评论')",
                        "div.cursor-pointer.text-center.t-subtitle2:has-text('Show collapsed comments')",
                        "div.cursor-pointer.text-center.t-subtitle2:has-text('Show Collapsed Comments')",
                        "div.cursor-pointer.text-center.t-subtitle2:has-text('展示被折叠的评论')",
                        "div.cursor-pointer.text-center.t-subtitle2:has-text('显示被折叠的评论')",
                        "div[class*='cursor-pointer'][class*='text-center'][class*='t-subtitle2']:has-text('Show collapsed comments')",
                        "div[class*='cursor-pointer'][class*='text-center'][class*='t-subtitle2']:has-text('Show Collapsed Comments')",
                        "button[data-testid*='show-more']",
                        "[role='button'][data-testid*='show-more']",
                        "[data-qa*='expand']",
                        "[data-qa*='show-more']",
                        "button[aria-label*='reply' i]",
                        "button[aria-label*='collapsed' i]",
                        "button[aria-label*='show' i][aria-label*='more' i]",
                        "button[aria-label*='expand' i]",
                        "[role='button'][aria-label*='reply' i]",
                        "[role='button'][aria-label*='collapsed' i]",
                        "[role='button'][aria-label*='show' i][aria-label*='more' i]",
                        "[role='button'][aria-label*='expand' i]",
                    ]
                    max_expand_rounds = 20
                    max_per_selector = 100
                    no_click_rounds = 0
                    no_match_rounds = 0
                    stalled_rounds = 0
                    total_clicked = 0
                    stop_reason = "max_rounds"
                    print(f"[expansion] start post_id={post_id}")

                    for round_idx in range(1, max_expand_rounds + 1):
                        clicked_any = False
                        round_clicks = 0
                        round_matches = 0
                        round_visible = 0
                        for sel in expand_selectors:
                            try:
                                locator = page.locator(sel)
                                count = locator.count()
                                round_matches += count
                                for idx in range(min(count, max_per_selector)):
                                    loc = locator.nth(idx)
                                    if not loc.is_visible(timeout=300):
                                        continue

                                    round_visible += 1

                                    try:
                                        tag_name = str(loc.evaluate("el => el.tagName", timeout=300) or "").upper()
                                        if tag_name and tag_name not in {"BUTTON", "A", "DIV", "SPAN"}:
                                            continue
                                    except Exception:
                                        pass

                                    click_ok = False
                                    for retry_idx in range(3):
                                        before_url = page.url
                                        try:
                                            loc.scroll_into_view_if_needed(timeout=600)
                                        except Exception:
                                            pass
                                        try:
                                            loc.hover(timeout=400)
                                        except Exception:
                                            pass
                                        try:
                                            loc.click(timeout=1500)
                                            click_ok = True
                                        except Exception:
                                            page.wait_for_timeout(250 + 200 * retry_idx)
                                            continue

                                        page.wait_for_timeout(850)
                                        # Guard against accidental navigation caused by broad UI overlaps.
                                        if page.url != before_url:
                                            try:
                                                page.go_back(wait_until="domcontentloaded", timeout=20000)
                                                page.wait_for_timeout(1200)
                                            except Exception:
                                                # If back fails, keep current page and continue best-effort.
                                                pass
                                        break

                                    if click_ok:
                                        clicked_any = True
                                        round_clicks += 1
                                        total_clicked += 1
                            except Exception:
                                pass

                        # Try scrolling likely comment containers first; fallback to page wheel.
                        try:
                            scroll_containers = page.locator(
                                "[class*='comment'], [data-testid*='comment'], [role='region']"
                            )
                            for c_idx in range(min(scroll_containers.count(), 4)):
                                try:
                                    scroll_containers.nth(c_idx).evaluate(
                                        "el => { el.scrollTop = (el.scrollTop || 0) + 3200; }",
                                        timeout=300,
                                    )
                                except Exception:
                                    pass
                        except Exception:
                            pass

                        # Scroll to reveal more expandable entries loaded lazily.
                        page.mouse.wheel(0, 3200)
                        page.wait_for_timeout(1200)

                        if round_matches == 0:
                            no_match_rounds += 1
                        else:
                            no_match_rounds = 0

                        if not clicked_any:
                            no_click_rounds += 1
                            if round_visible > 0:
                                stalled_rounds += 1
                            else:
                                stalled_rounds = 0
                        else:
                            no_click_rounds = 0
                            stalled_rounds = 0

                        print(
                            f"[expansion] post_id={post_id} round={round_idx}/{max_expand_rounds} "
                            f"matches={round_matches} visible={round_visible} clicked={round_clicks} total_clicked={total_clicked} "
                            f"no_click_rounds={no_click_rounds} no_match_rounds={no_match_rounds} stalled_rounds={stalled_rounds}"
                        )

                        if no_match_rounds >= 3:
                            stop_reason = "no_new_buttons_3_rounds"
                            break

                        if no_click_rounds >= 5 and no_match_rounds >= 2:
                            stop_reason = "stabilized_no_click_and_no_match"
                            break

                        if stalled_rounds >= 2 and no_click_rounds >= 2:
                            stop_reason = "stalled_on_visible_buttons"
                            break

                    print(
                        f"[expansion] done post_id={post_id} total_clicked={total_clicked} "
                        f"stop_reason={stop_reason}"
                    )
                except Exception:
                    pass

                # Re-dump after expansion so offline parser reads the fuller HTML.
                if page_dump_dir is not None:
                    dump_page_content(page=page, dump_dir=page_dump_dir, post_id=post_id)
                    print(f"[dump] saved expanded page dump for post_id={post_id}")

                comment_rows = api_comments[:max_comments]
                source_kind = "api"
                if not comment_rows:
                    comment_rows = extract_comment_cards_from_dom(
                        page=page,
                        post_id=post_id,
                        source_url=url,
                        max_comments=max_comments,
                    )
                    source_kind = "dom-cards"
                if not comment_rows:
                    raw_comments = extract_comments_from_dom(page)
                    comment_rows = []
                    source_kind = "dom-text"
                    seen: set[str] = set()
                    for idx_fallback, text in enumerate(raw_comments, start=1):
                        normalized = clean_text(text)
                        if not normalized or normalized in seen:
                            continue
                        seen.add(normalized)
                        comment_rows.append(
                            {
                                "post_id": post_id,
                                "comment_id": f"{post_id}_dom_text_{idx_fallback}",
                                "comment_text": normalized,
                                "comment_author": "",
                                "comment_author_username": "",
                                "comment_time": "",
                                "reply_count": "",
                                "like_count": "",
                                "source_url": url,
                            }
                        )
                        if len(comment_rows) >= max_comments:
                            break

                if comment_rows:
                    print(
                        f"[comments] captured {len(comment_rows)} comments for post_id={post_id} via {source_kind}"
                    )

                for comment_row in comment_rows:
                    rows.append(comment_row)
            finally:
                page.remove_listener("response", on_response)
                time.sleep(pause_seconds)
    finally:
        safe_close_browser(playwright_obj, context)

    return rows


"""
将数据列表写入CSV文件，使用UTF-8 with BOM编码以确保Excel兼容。

这是数据导出的核心函数，处理三项主要输出：帖子CSV、评论CSV、合并CSV。
使用DictWriter确保列顺序一致，并添加BOM头以便在Excel中正确显示中文。

参数:
    path: 输出文件路径
    rows: 要写入的数据行列表（字典列表）
    fieldnames: CSV文件的列名顺序
    
返回值:
    None: 文件被写入磁盘
"""
def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


"""
Binance Square抓取脚本的主入口函数，协调整个爬取流程的各个阶段。

核心执行逻辑：
1. 解析命令行参数，初始化输出目录和调试目录
2. 根据source参数选择数据获取模式（news API模式 / square-home 浏览器模式）
3. 获取帖子数据并导出CSV和JSON格式
4. 根据配置决定是否抓取评论数据（可跳过）
5. 抓取评论数据（通过浏览器自动化和API响应拦截）
6. 合并帖子与评论数据，输出完整的关联CSV
7. 可选的调试功能：保存页面内容、评论API响应等

这是整个应用程序的编排层，将各个功能模块组合成完整的工作流。
"""
def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    ensure_dir(output_dir)

    posts_csv_path = output_dir / "binance_square_posts.csv"
    raw_json_path = output_dir / "binance_square_posts_raw.json"
    comment_debug_dir = output_dir / COMMENT_DEBUG_DIR_NAME
    page_dump_dir = output_dir / PAGE_DUMP_DIR_NAME
    if args.save_comment_debug:
        ensure_dir(comment_debug_dir)
    else:
        comment_debug_dir = None  # type: ignore[assignment]
    if args.dump_page:
        ensure_dir(page_dump_dir)
    else:
        page_dump_dir = None  # type: ignore[assignment]

    if args.source == "news":
        session = build_session(args.lang, retries=args.retries)
        if args.trust_env_proxy:
            session.trust_env = True
        try:
            check_result = check_connectivity(
                session=session,
                news_api=args.news_api,
                timeout=args.request_timeout,
            )
            print(
                "[check] api reachable "
                f"status={check_result['status_code']} "
                f"sample_count={check_result['sample_count']} "
                f"url={check_result['url']}"
            )
            if args.check_only:
                return
            raw_posts = fetch_posts(
                session=session,
                news_api=args.news_api,
                pages=args.pages,
                page_size=args.page_size,
                timeout=args.request_timeout,
            )
        except RequestException as exc:
            raise SystemExit(
                "帖子列表抓取失败。"
                "这更像是网络不可达、地区限制或代理配置问题，而不一定是接口地址写错。"
                "如果你本机需要走代理，请加上 --trust-env-proxy；"
                "也可以用 --news-api 指定你自己验证过的新地址；"
                "先试试 --check-only 只做网络检测。"
                f"\n原始错误: {exc}"
            ) from exc
        raw_posts = raw_posts[: args.max_posts]
        write_json(raw_json_path, raw_posts)
        normalized_posts = dedupe_rows(
            [normalize_post(item) for item in raw_posts],
            key="post_id",
        )
    else:
        if args.check_only:
            print("[check] square-home mode uses browser collection and is ready to run")
            return
        normalized_posts = dedupe_rows(
            collect_posts_from_square_home(
                lang=args.lang,
                max_posts=args.max_posts,
                headless=args.headless,
                pause_seconds=args.pause_seconds,
                user_data_dir=args.user_data_dir,
                wait_for_login=args.wait_for_login,
            ),
            key="post_id",
        )
        write_json(raw_json_path, normalized_posts)
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
            "author_username",
            "like_count",
            "comment_count",
            "view_count",
            "share_count",
            "related_symbols",
            "link",
        ],
    )
    print(f"[ok] posts saved: {posts_csv_path} ({len(normalized_posts)} rows)")

    if args.skip_comments:
        print("[ok] skip comment crawling by --skip-comments")
        return
    else:
        if args.source == "square-home":
            candidate_posts = [post for post in normalized_posts if post["link"]]
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
            wait_for_login=args.wait_for_login,
            comment_debug_dir=comment_debug_dir,
            page_dump_dir=page_dump_dir,
        )
    print(f"[ok] comment crawling finished: captured {len(comment_rows)} rows (CSV export disabled)")


if __name__ == "__main__":
    main()
