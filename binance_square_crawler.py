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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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
DEFAULT_USER_DATA_DIR = Path("tmp_chrome_profile")
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
        "--source",
        choices=["news", "square-home"],
        default="news",
        help="帖子来源：news=新闻流接口，square-home=币安广场首页滚动采集",
    )
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
        default=str(DEFAULT_USER_DATA_DIR),
        help=f"Chromium 用户目录，默认 {DEFAULT_USER_DATA_DIR}，用于复用登录态",
    )
    parser.add_argument(
        "--wait-for-login",
        action="store_true",
        help="打开页面后暂停，等你手动登录并回车，再继续抓取",
    )
    parser.add_argument(
        "--trust-env-proxy",
        action="store_true",
        help="默认不读取系统代理环境变量；如果你确定代理可用，再显式开启",
    )
    parser.add_argument(
        "--news-api",
        default=BINANCE_NEWS_API,
        help="帖子列表接口地址，默认使用当前脚本内置的 Binance Square 列表接口",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="帖子列表接口失败后的重试次数，默认 2",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="只检查接口和网络可达性，不写出帖子和评论文件",
    )
    parser.add_argument(
        "--save-comment-debug",
        action="store_true",
        help="保存命中的评论接口原始响应，便于排查评论字段和接口路径",
    )
    parser.add_argument(
        "--dump-page",
        action="store_true",
        help="保存帖子详情页的 HTML、纯文本和截图，便于手动筛选页面结构",
    )
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
创建目录（如果不存在），支持创建多级父目录。

这是文件系统操作的辅助函数，用于确保输出目录、调试目录等路径存在，
避免在写入文件时因目录不存在而失败。

参数:
    path: 需要确保存在的目录路径
"""
def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


"""
将字符串转换为安全文件名，使用下划线替换不安全的字符。

用于为页面保存内容（HTML、TXT、截图）生成文件名，避免因特殊字符导致的文件系统问题。

参数:
    value: 原始字符串，如帖子ID或页面标题
    
返回值:
    str: 仅包含字母、数字、点、连字符和下划线的安全文件名
"""
def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "page"


"""
将时间戳（毫秒或秒）转换为可读的日期时间字符串。

处理两种格式的时间戳：毫秒级（>10^12）和秒级，将UNIX时间戳转换为标准的YYYY-MM-DD HH:MM:SS格式。

参数:
    value: 时间戳值，可以是字符串、整数或None
    
返回值:
    str: 格式化后的日期时间字符串，如果输入为空则返回空字符串
"""
def timestamp_to_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    number = int(value)
    if number > 10**12:
        date_value = dt.datetime.fromtimestamp(number / 1000)
    else:
        date_value = dt.datetime.fromtimestamp(number)
    return date_value.strftime("%Y-%m-%d %H:%M:%S")


"""
清理文本内容，移除多余空白字符（多个连续空格、换行等）。

这是文本处理的基础函数，用于标准化从页面或API获取的文本内容，
确保后续处理不受空白字符格式差异的影响。

参数:
    text: 原始文本内容
    
返回值:
    str: 清理后的文本，多个连续空白字符替换为单个空格，并去除首尾空格
"""
def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


"""
判断文本是否是有意义的评论内容，过滤UI元素、时间提示和无意义文本。

通过多级筛选排除非评论内容：1) 精确匹配过滤词（如"回复"、"点赞"）；2) 纯数字/符号组合；
3) 时间格式文本（如"5分钟"、"2小时前"）。这是评论数据质量保证的关键函数。

参数:
    text: 待判断的文本内容
    
返回值:
    bool: 如果文本被认为是有意义的真实评论则返回True，否则返回False
"""
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


"""
从API响应payload中递归提取所有评论文本。

通过深度优先遍历JSON结构，查找候选键名（content, comment, text等）对应的字符串值，
并调用is_meaningful_comment过滤非评论内容。这是评论API响应的第一层解析。

参数:
    payload: API响应数据（字典、列表或嵌套结构）
    
返回值:
    list[str]: 提取出的评论文本列表，已过滤无意义内容
"""
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


"""
从字典节点中提取第一个匹配给定键名的字符串值（键名大小写不敏感）。

这是评论数据提取的辅助函数，支持嵌套查找：如果在当前层级没找到，
会递归到嵌套字典中搜索。用于提取评论文本、作者名等字段。

参数:
    node: JSON节点（通常是字典）
    keys: 候选键名列表，会按顺序尝试查找
    
返回值:
    str: 找到的第一个非空字符串值，否则返回空字符串
"""
def extract_first_string(node: Any, keys: list[str]) -> str:
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


def looks_like_comment_node(node: dict[str, Any]) -> bool:
    text = extract_first_string(
        node,
        ["content", "comment", "commentcontent", "commenttext", "text", "message", "body"],
    )
    if not is_meaningful_comment(text):
        return False

    lowered_keys = {str(key).lower() for key in node.keys()}
    markers = {
        "commentid",
        "replycount",
        "likecount",
        "subcomments",
        "replies",
        "comment",
        "commentvo",
        "commentitem",
    }
    return bool(lowered_keys & markers) or bool(
        extract_first_string(node, ["nickname", "username", "authorname", "screenname"])
    )


def extract_comment_rows_from_payload(
    payload: Any,
    post_id: str,
    source_url: str,
    max_comments: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen_texts: set[str] = set()
    seen_ids: set[str] = set()

    def walk(node: Any) -> None:
        if len(results) >= max_comments:
            return
        if isinstance(node, dict):
            if looks_like_comment_node(node):
                text = clean_text(
                    extract_first_string(
                        node,
                        ["content", "comment", "commentcontent", "commenttext", "text", "message", "body"],
                    )
                )
                comment_id = (
                    extract_first_string(node, ["commentid", "id", "rootcommentid", "replyid"])
                    or f"{post_id}_{len(results) + 1}"
                )
                if text and is_meaningful_comment(text):
                    dedupe_key = comment_id if comment_id else text
                    if dedupe_key not in seen_ids and text not in seen_texts:
                        seen_ids.add(dedupe_key)
                        seen_texts.add(text)
                        results.append(
                            {
                                "post_id": post_id,
                                "comment_id": comment_id,
                                "comment_text": text,
                                "comment_author": extract_first_string(
                                    node,
                                    ["nickname", "username", "authorname", "screenname", "name", "usernickname"],
                                ),
                                "comment_author_username": extract_first_string(
                                    node,
                                    ["userid", "username", "authorid", "usercode", "authorcode"],
                                ),
                                "comment_time": extract_first_number(
                                    node,
                                    ["createtime", "createat", "commenttime", "publishtime", "time"],
                                ),
                                "reply_count": extract_first_number(node, ["replycount", "childrencount"]),
                                "like_count": extract_first_number(node, ["likecount", "upcount"]),
                                "source_url": source_url,
                            }
                        )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return results[:max_comments]


"""
创建并启动Playwright浏览器上下文，支持持久化用户目录以保持登录状态。

这是浏览器自动化的入口函数，根据配置决定使用无头模式还是可见模式，
并支持使用持久化用户目录（用于复用登录态）或创建临时会话。
是"square-home"模式评论抓取的基础设施。

参数:
    headless: 是否使用无头模式（无GUI界面）
    user_data_dir: Chromium用户数据目录路径，用于持久化登录信息
    
返回值:
    tuple: (playwright对象, 浏览器上下文对象)，需要在结束时调用safe_close_browser释放资源
"""
def create_browser_context(headless: bool, user_data_dir: str) -> tuple[Any, BrowserContext]:
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


def safe_close_browser(playwright_obj: Any, context: BrowserContext) -> None:
    try:
        context.close()
    finally:
        playwright_obj.stop()


"""
尝试点击评论入口按钮，展开评论区。

使用多种CSS选择器尝试找到并点击评论按钮，支持中英文界面。
这是触发评论懒加载的关键步骤，因为很多页面的评论内容需要用户交互才会完全加载。

参数:
    page: Playwright页面对象
    
返回值:
    bool: 是否成功找到并点击了评论按钮
"""
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


def extract_comment_cards_from_dom(page: Page, post_id: str, source_url: str, max_comments: int) -> list[dict[str, Any]]:
    script = """
    () => {
      const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
      const isBadLine = (line) => {
        const lowered = line.toLowerCase();
        if (!line) return true;
        if (/^[\\d\\s,.:/+\\-]+$/.test(line)) return true;
        if (/^\\d+[smhdw]$/.test(lowered)) return true;
        if (/^\\d+[秒分钟小时天周月年]前$/.test(line)) return true;
        const blocked = new Set([
          "查看翻译", "translate", "like", "reply", "share", "comment",
          "点赞", "回复", "分享", "评论", "发布"
        ]);
        return blocked.has(lowered) || blocked.has(line);
      };

      const anchors = Array.from(document.querySelectorAll("a[href*='/square/profile/']"));
      const cards = [];
      const seen = new Set();

      for (const anchor of anchors) {
        let container = anchor;
        for (let i = 0; i < 6 && container; i += 1) {
          container = container.parentElement;
          if (!container) break;
          const text = clean(container.innerText || "");
          const lines = text.split(/\\n+/).map(clean).filter(Boolean);
          if (lines.length >= 2 && lines.length <= 14 && text.length >= 8 && text.length <= 500) {
            const name = clean(anchor.textContent || "");
            const href = anchor.getAttribute("href") || "";
            const username = href.split("/").filter(Boolean).pop() || "";
            let timeText = "";
            for (const line of lines) {
              if (/\\d+\\s*(秒|分钟|小时|天|周|月|年)前/.test(line) || /\\d+[smhdw]/i.test(line)) {
                timeText = line;
                break;
              }
            }
            const contentLines = lines.filter((line) => line !== name && line !== timeText && !isBadLine(line));
            const commentText = clean(contentLines.join(" "));
            if (!commentText || commentText === name) continue;
            const key = `${username}__${commentText}`;
            if (seen.has(key)) continue;
            seen.add(key);
            cards.push({
              comment_author: name,
              comment_author_username: username,
              comment_time_text: timeText,
              comment_text: commentText,
            });
            break;
          }
        }
      }
      return cards;
    }
    """
    try:
        cards = page.evaluate(script)
    except Exception:
        return []

    rows: list[dict[str, Any]] = []
    seen_texts: set[str] = set()
    for index, card in enumerate(cards, start=1):
        text = clean_text(card.get("comment_text", ""))
        if not is_meaningful_comment(text) or text in seen_texts:
            continue
        seen_texts.add(text)
        rows.append(
            {
                "post_id": post_id,
                "comment_id": f"{post_id}_dom_{index}",
                "comment_text": text,
                "comment_author": clean_text(card.get("comment_author", "")),
                "comment_author_username": clean_text(card.get("comment_author_username", "")),
                "comment_time": clean_text(card.get("comment_time_text", "")),
                "reply_count": "",
                "like_count": "",
                "source_url": source_url,
            }
        )
        if len(rows) >= max_comments:
            break
    return rows


"""
从页面DOM中提取帖子标题、内容、作者、相关币种等元数据。

使用多种选择器策略（OG标签、数据测试ID、类名等）从完整页面提取信息，
包括：1) 帖子标题（优先OG标签）；2) 正文内容；3) 作者信息（含用户名）；
4) 相关币种符号（从美元符号标记和价格链接提取）。
这是直接从页面补充API数据缺失字段的关键函数。

参数:
    page: Playwright页面对象
    
返回值:
    dict: 包含title, content, author, author_username, related_symbols的元数据字典
"""
def extract_post_meta_from_page(page: Page) -> dict[str, Any]:
    title = ""
    content = ""
    author = ""
    author_username = ""
    related_symbols: list[str] = []

    title_selectors = [
        "meta[property='og:title']",
        "h1",
        "[data-testid*='title']",
    ]
    for selector in title_selectors:
        try:
            if selector.startswith("meta"):
                value = page.locator(selector).first.get_attribute("content")
                if value:
                    title = clean_text(value)
                    break
            else:
                locator = page.locator(selector).first
                if locator.count() > 0:
                    value = clean_text(locator.inner_text(timeout=1500))
                    if value:
                        title = value
                        break
        except Exception:
            continue

    content_selectors = [
        "meta[property='og:description']",
        "[data-testid*='content']",
        "[class*='content']",
        "article",
        "main",
    ]
    for selector in content_selectors:
        try:
            if selector.startswith("meta"):
                value = page.locator(selector).first.get_attribute("content")
                if value:
                    content = clean_text(value)
                    break
            else:
                locator = page.locator(selector).first
                if locator.count() > 0:
                    value = clean_text(locator.inner_text(timeout=1500))
                    if value:
                        content = value[:5000]
                        break
        except Exception:
            continue

    author_selectors = [
        "[data-testid*='author']",
        "[class*='author']",
        "a[href*='/square/profile/']",
    ]
    for selector in author_selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                value = clean_text(locator.inner_text(timeout=1000))
                if value:
                    author = value
                    href = locator.get_attribute("href")
                    if href:
                        author_username = href.rstrip("/").split("/")[-1]
                    break
        except Exception:
            continue

    try:
        page_text = clean_text(page.locator("body").inner_text(timeout=2000))
    except Exception:
        page_text = ""
    for symbol in re.findall(r"\$([A-Z][A-Z0-9]{1,9})\b", page_text):
        if symbol not in related_symbols:
            related_symbols.append(symbol)
    try:
        coin_links = page.locator("a[href*='/price/']").evaluate_all(
            "(els) => els.map(el => (el.textContent || '').trim()).filter(Boolean)"
        )
        for coin in coin_links:
            normalized = clean_text(coin).upper().replace("$", "")
            if re.fullmatch(r"[A-Z][A-Z0-9]{1,9}", normalized) and normalized not in related_symbols:
                related_symbols.append(normalized)
    except Exception:
        pass

    return {
        "title": title,
        "content": content,
        "author": author,
        "author_username": author_username,
        "related_symbols": ",".join(related_symbols),
    }


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
                if not clicked:
                    print("[comments] comment entry not found, continue with scrolling fallback")
                else:
                    print(f"[comments] clicked comment entry for post_id={post_id}")

                for _ in range(5):
                    page.mouse.wheel(0, 2400)
                    page.wait_for_timeout(1200)

                # ========== NEW: Click all "Show More Replies" ==========
                try:
                    show_more_selectors = [
                        "button:has-text('Show More Replies')",
                        "button:has-text('查看更多回复')",
                        "[data-testid*='show-more']",
                        "div:has-text('Show More Replies')",
                        "div:has-text('查看更多回复')",
                        "span:has-text('Show More Replies')"
                    ]
                    for _ in range(5): # Try multiple passes in case new ones appear
                        clicked_any = False
                        for sel in show_more_selectors:
                            try:
                                locators = page.locator(sel).all()
                                for loc in locators:
                                    if loc.is_visible(timeout=500):
                                        loc.click(timeout=1000)
                                        page.wait_for_timeout(800)
                                        clicked_any = True
                            except Exception:
                                pass
                        if not clicked_any:
                            break
                except Exception:
                    pass
                # ========================================================

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
                        if not is_meaningful_comment(normalized) or normalized in seen:
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

                if not comment_rows:
                    print(f"[comments] no comment captured for post_id={post_id}, api_hits={api_hits}")
                else:
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
保存页面完整内容到本地，包括HTML、纯文本和截图。

实现数据冗余备份和可视化Debug：1) 原始HTML（用于DOM分析）；
2) 纯文本（用于后续离线解析）；3) 全页截图（用于视觉验证）。
这是--dump-page参数的核心实现，为离线分析提供完整数据。

参数:
    page: Playwright页面对象
    dump_dir: 保存内容的目录
    post_id: 帖子ID，用作文件名基础
    
返回值:
    None: 三种格式的文件被写入指定目录
"""
def dump_page_content(page: Page, dump_dir: Path, post_id: str) -> None:
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


"""
将帖子信息和评论数据按post_id进行合并，生成宽表格式。

创建数据关联视图：对于每条评论，查找对应的帖子信息并整合为一行。
便于分析评论与帖子内容的关联关系，例如：某条评论对应的帖子作者、标题、币种等。
这是生成最终合并CSV的核心函数。

参数:
    posts: 帖子信息列表
    comments: 评论信息列表
    
返回值:
    list[dict]: 合并后的数据行，每行包含评论信息和关联的帖子信息
"""
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
                "comment_author": comment.get("comment_author", ""),
                "comment_author_username": comment.get("comment_author_username", ""),
                "comment_time": comment.get("comment_time", ""),
                "comment_reply_count": comment.get("reply_count", ""),
                "comment_like_count": comment.get("like_count", ""),
                "source_url": comment["source_url"],
                "post_time": post.get("time", ""),
                "post_title": post.get("title", ""),
                "post_content": post.get("content", ""),
                "post_author": post.get("author", ""),
                "post_author_username": post.get("author_username", ""),
                "related_symbols": post.get("related_symbols", ""),
                "post_link": post.get("link", ""),
            }
        )
    return merged


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
    comments_csv_path = output_dir / "binance_square_comments.csv"
    merged_csv_path = output_dir / "binance_square_posts_comments.csv"
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
        comment_rows: list[dict[str, Any]] = []
        print("[ok] skip comment crawling by --skip-comments")
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

    write_csv(
        comments_csv_path,
        comment_rows,
        fieldnames=[
            "post_id",
            "comment_id",
            "comment_text",
            "comment_author",
            "comment_author_username",
            "comment_time",
            "reply_count",
            "like_count",
            "source_url",
        ],
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
            "comment_author",
            "comment_author_username",
            "comment_time",
            "comment_reply_count",
            "comment_like_count",
            "source_url",
            "post_time",
            "post_title",
            "post_content",
            "post_author",
            "post_author_username",
            "related_symbols",
            "post_link",
        ],
    )
    print(f"[ok] merged saved: {merged_csv_path} ({len(merged_rows)} rows)")


if __name__ == "__main__":
    main()
