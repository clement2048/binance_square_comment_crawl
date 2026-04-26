#!/usr/bin/env python3
"""
crawler_coin.py — 按币种定向爬取 Binance 官方新闻

通过 Binance BAPI 接口分页获取新闻，在客户端按币种关键词 + 别名匹配，
支持采集阶段质量过滤（评论数、点赞数），SQLite 去重存储。

策略：
  - BAPI 接口不支持 server 端按币种筛选
  - 统一翻页采集，每条新闻对所有目标币种做关键词匹配
  - 一条新闻可能匹配多个币种（如 "BTC and ETH liquidation"）

用法:
  # 国内用户需要 --trust-env-proxy 走系统代理
  python crawler_coin.py --check-only --symbols BTC --trust-env-proxy
  python crawler_coin.py --symbols BTC,ETH,SOL --max-posts 200 --min-comment-count 5 --trust-env-proxy
  python crawler_coin.py --symbols BTC --max-posts 100 --fetch-html --headless --trust-env-proxy
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from crawler_util import clean_text, ensure_dir, timestamp_to_text

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BAPI_NEWS_LIST = (
    "https://www.binance.com/bapi/composite/v4/friendly/pgc/feed/news/list"
)
DEFAULT_OUTPUT_DIR = "crawler_coin_output"
DEFAULT_DB_NAME = "coin_posts.db"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)

# 币种 -> 关键词别名（用于标题/副标题模糊匹配）
# 取交集：符号、全名、常见变体
SYMBOL_ALIASES: dict[str, list[str]] = {
    "BTC":  ["btc", "bitcoin", "$btc", "#btc"],
    "ETH":  ["eth", "ethereum", "$eth", "#eth", "ether"],
    "SOL":  ["sol", "solana", "$sol", "#sol"],
    "BNB":  ["bnb", "binance coin", "$bnb", "#bnb"],
    "XRP":  ["xrp", "ripple", "$xrp", "#xrp"],
    "DOGE": ["doge", "dogecoin", "$doge", "#doge"],
    "ADA":  ["ada", "cardano", "$ada", "#ada"],
    "AVAX": ["avax", "avalanche", "$avax", "#avax"],
    "DOT":  ["dot", "polkadot", "$dot", "#dot"],
    "LINK": ["link", "chainlink", "$link", "#link"],
    "MATIC":["matic", "polygon", "$matic", "#matic"],
    "UNI":  ["uni", "uniswap", "$uni", "#uni"],
    "ATOM": ["atom", "cosmos", "$atom", "#atom"],
    "LTC":  ["ltc", "litecoin", "$ltc", "#ltc"],
    "SUI":  ["sui", "$sui", "#sui"],
    "APT":  ["apt", "aptos", "$apt", "#apt"],
    "ARB":  ["arb", "arbitrum", "$arb", "#arb"],
    "OP":   ["op", "optimism", "$op", "#op"],
    "NEAR": ["near", "near protocol", "$near", "#near"],
    "PEPE": ["pepe", "$pepe", "#pepe"],
}


def get_aliases(symbol: str) -> list[str]:
    """获取币种的匹配关键词列表，若不在预定义表中则自动生成。"""
    upper = symbol.upper()
    if upper in SYMBOL_ALIASES:
        return SYMBOL_ALIASES[upper]
    return [upper.lower(), f"${upper.lower()}", f"#{upper.lower()}"]


# ---------------------------------------------------------------------------
# 命令行参数
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="crawler_coin — 按币种定向爬取 Binance 官方新闻（BAPI + 客户端关键词匹配）"
    )
    parser.add_argument(
        "--symbols",
        required=True,
        help="目标币种，逗号分隔，如: BTC,ETH,SOL",
    )
    parser.add_argument("--lang", default="en", help="语言版本，默认 en")
    parser.add_argument(
        "--max-posts",
        type=int,
        default=200,
        help="每个币种最多获取帖子数，默认 200",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=100,
        help="最大翻页数，默认 100",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=20,
        help="每页帖子数，默认 20",
    )
    parser.add_argument(
        "--min-comment-count",
        type=int,
        default=5,
        help="最低评论数，默认 5",
    )
    parser.add_argument(
        "--min-like-count",
        type=int,
        default=0,
        help="最低点赞数，默认 0（不过滤）",
    )
    parser.add_argument(
        "--min-post-age-days",
        type=int,
        default=0,
        help="只保留至少 N 天前的帖子，0=不过滤",
    )
    parser.add_argument(
        "--idle-stop-pages",
        type=int,
        default=10,
        help="连续 N 页无任何币种匹配时停止，默认 10",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=0.3,
        help="请求间隔秒数，默认 0.3",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="HTTP 重试次数，默认 3",
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=30,
        help="HTTP 超时秒数，默认 30",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"输出目录，默认 {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--db-path",
        default="",
        help=f"SQLite 路径，默认 <output-dir>/{DEFAULT_DB_NAME}",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="检查 API 连通性并估算各币种命中率",
    )
    parser.add_argument(
        "--fetch-html",
        action="store_true",
        help="采集后自动调用 fetch_pages_from_db.py 下载 HTML",
    )
    parser.add_argument(
        "--html-limit",
        type=int,
        default=100,
        help="下载 HTML 的最大帖子数，默认 100",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="下载 HTML 时使用无头模式",
    )
    parser.add_argument(
        "--user-data-dir",
        default="tmp_chrome_profile",
        help="浏览器用户数据目录",
    )
    parser.add_argument(
        "--trust-env-proxy",
        action="store_true",
        help="使用系统代理环境变量（国内用户需要开启）",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# HTTP 会话
# ---------------------------------------------------------------------------

def build_session(lang: str, retries: int, trust_env_proxy: bool = False) -> requests.Session:
    session = requests.Session()
    session.trust_env = trust_env_proxy
    if trust_env_proxy:
        print("[session] using system proxy (trust_env=True)")
    retry_strategy = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": f"{lang},{lang.split('-')[0]};q=0.9",
            "Origin": "https://www.binance.com",
            "Referer": f"https://www.binance.com/{lang}/square/news/all",
            "clienttype": "web",
            "lang": lang,
        }
    )
    return session


# ---------------------------------------------------------------------------
# Cookie 预热
# ---------------------------------------------------------------------------

def warmup_session(session: requests.Session, lang: str, timeout: int) -> None:
    """先访问 Square 首页获取 cookie，模拟真实浏览器，避免被反爬。"""
    warmup_url = f"https://www.binance.com/{lang}/square"
    try:
        r = session.get(warmup_url, timeout=timeout)
        print(f"[warmup] {warmup_url} status={r.status_code}")
    except Exception as exc:
        print(f"[warmup] warning: could not reach {warmup_url}: {exc}")
        print("[warmup] continuing anyway, BAPI might still work...")


# ---------------------------------------------------------------------------
# API 请求
# ---------------------------------------------------------------------------

def fetch_page(
    session: requests.Session,
    page_index: int,
    page_size: int,
    timeout: int,
) -> list[dict[str, Any]]:
    params = {
        "pageIndex": page_index,
        "pageSize": page_size,
        "strategy": 6,
        "tagId": 0,
        "featured": "false",
    }
    response = session.get(BAPI_NEWS_LIST, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    items = data.get("vos", []) if isinstance(data, dict) else []
    return [item for item in items if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# 关键词匹配
# ---------------------------------------------------------------------------

def match_symbols(
    title: str,
    subtitle: str,
    symbols: list[str],
) -> list[str]:
    """返回该帖子匹配到的币种列表。"""
    haystack = f"{title} {subtitle}".lower()
    # 移除 HTML 标签
    haystack = re.sub(r"<[^>]+>", " ", haystack)
    matched: list[str] = []
    for sym in symbols:
        for alias in get_aliases(sym):
            # 用词边界匹配，避免 "ETH" 匹配到 "ETHEREUM" 以外的词中
            if _word_match(alias, haystack):
                matched.append(sym)
                break
    return matched


def _word_match(keyword: str, text: str) -> bool:
    """词边界匹配：$btc 和 #btc 可以部分匹配，其他用完整词匹配。"""
    if keyword.startswith("$") or keyword.startswith("#"):
        return keyword in text
    # 对于普通关键词（如 btc, bitcoin），使用词边界
    return bool(re.search(rf"\b{re.escape(keyword)}\b", text))


# ---------------------------------------------------------------------------
# 数据标准化
# ---------------------------------------------------------------------------

def normalize_post(item: dict[str, Any], matched_symbols: list[str]) -> dict[str, Any]:
    title = clean_text(item.get("title", ""))
    subtitle = clean_text(item.get("subTitle", ""))
    content = clean_text(" ".join(part for part in [title, subtitle] if part))
    post_id = str(item.get("id", ""))
    raw_date = item.get("date")

    return {
        "post_id": post_id,
        "time": timestamp_to_text(raw_date),
        "timestamp_ms": _normalize_timestamp(raw_date),
        "title": title,
        "subtitle": subtitle,
        "content": content,
        "author": clean_text(item.get("authorName", "")),
        "author_username": clean_text(
            item.get("authorUserName", "")
            or item.get("authorCode", "")
            or item.get("authorId", "")
        ),
        "like_count": int(item.get("likeCount", 0) or 0),
        "comment_count": int(item.get("commentCount", 0) or 0),
        "view_count": int(item.get("viewCount", 0) or 0),
        "share_count": int(item.get("shareCount", 0) or 0),
        "related_symbols": ",".join(matched_symbols),
        "link": item.get("webLink", ""),
        "matched_symbols": matched_symbols,
    }


def _normalize_timestamp(value: Any) -> int:
    if value is None:
        return 0
    num = int(value)
    return num if num > 10**12 else num * 1000


# ---------------------------------------------------------------------------
# 质量过滤
# ---------------------------------------------------------------------------

def evaluate_post_quality(
    post: dict[str, Any],
    min_comment_count: int,
    min_like_count: int,
    min_age_days: int,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []

    if min_comment_count > 0 and post["comment_count"] < min_comment_count:
        reasons.append("low_comment_count")

    if min_like_count > 0 and post["like_count"] < min_like_count:
        reasons.append("low_like_count")

    if min_age_days > 0:
        ts = post["timestamp_ms"]
        if ts <= 0:
            reasons.append("missing_timestamp")
        else:
            now_ms = int(time.time() * 1000)
            min_age_ms = min_age_days * 24 * 3600 * 1000
            if (now_ms - ts) < min_age_ms:
                reasons.append("too_recent")

    return len(reasons) == 0, reasons


# ---------------------------------------------------------------------------
# SQLite 存储
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coin_posts (
            post_id TEXT PRIMARY KEY,
            link TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            subtitle TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            author TEXT NOT NULL DEFAULT '',
            author_username TEXT NOT NULL DEFAULT '',
            like_count INTEGER NOT NULL DEFAULT 0,
            comment_count INTEGER NOT NULL DEFAULT 0,
            view_count INTEGER NOT NULL DEFAULT 0,
            share_count INTEGER NOT NULL DEFAULT 0,
            related_symbols TEXT NOT NULL DEFAULT '',
            timestamp_ms INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            symbols TEXT NOT NULL,
            max_pages INTEGER NOT NULL,
            min_comment_count INTEGER NOT NULL DEFAULT 0,
            min_like_count INTEGER NOT NULL DEFAULT 0,
            pages_done INTEGER NOT NULL DEFAULT 0,
            new_added INTEGER NOT NULL DEFAULT 0,
            total_posts_after INTEGER NOT NULL DEFAULT 0,
            stop_reason TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_coin_posts_symbols ON coin_posts(related_symbols)"
    )
    conn.commit()


def count_posts(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(1) FROM coin_posts").fetchone()
    return int(row[0]) if row else 0


def count_posts_for_symbol(conn: sqlite3.Connection, symbol: str) -> int:
    row = conn.execute(
        "SELECT COUNT(1) FROM coin_posts WHERE related_symbols LIKE ?",
        (f"%{symbol}%",),
    ).fetchone()
    return int(row[0]) if row else 0


def insert_or_update_posts(
    conn: sqlite3.Connection,
    posts: list[dict[str, Any]],
    seen_at: str,
) -> int:
    new_count = 0
    for post in posts:
        pid = post["post_id"]
        if not pid:
            continue
        existing = conn.execute(
            "SELECT post_id FROM coin_posts WHERE post_id=?", (pid,)
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO coin_posts(
                    post_id, link, title, subtitle, content,
                    author, author_username,
                    like_count, comment_count, view_count, share_count,
                    related_symbols, timestamp_ms,
                    first_seen_at, last_seen_at, seen_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    pid,
                    post["link"],
                    post["title"],
                    post["subtitle"],
                    post["content"],
                    post["author"],
                    post["author_username"],
                    post["like_count"],
                    post["comment_count"],
                    post["view_count"],
                    post["share_count"],
                    post["related_symbols"],
                    post["timestamp_ms"],
                    seen_at,
                    seen_at,
                ),
            )
            new_count += 1
        else:
            # 更新统计字段，并追加新匹配的币种
            old_symbols = conn.execute(
                "SELECT related_symbols FROM coin_posts WHERE post_id=?", (pid,)
            ).fetchone()
            old_set = set(old_symbols[0].split(",")) if old_symbols and old_symbols[0] else set()
            new_set = set(post["related_symbols"].split(","))
            merged = ",".join(sorted(old_set | new_set))
            conn.execute(
                """
                UPDATE coin_posts
                SET last_seen_at=?, seen_count=seen_count+1,
                    like_count=?, comment_count=?, view_count=?, share_count=?,
                    related_symbols=?
                WHERE post_id=?
                """,
                (
                    seen_at,
                    post["like_count"],
                    post["comment_count"],
                    post["view_count"],
                    post["share_count"],
                    merged,
                    pid,
                ),
            )
    conn.commit()
    return new_count


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

def export_posts(
    conn: sqlite3.Connection,
    all_symbols: list[str],
    csv_path: Path,
    json_path: Path,
) -> int:
    query = "SELECT * FROM coin_posts ORDER BY first_seen_at ASC"
    records = conn.execute(query).fetchall()
    rows: list[dict[str, Any]] = []
    for r in records:
        row = dict(r)
        rows.append(
            {
                "post_id": str(row["post_id"]),
                "time": timestamp_to_text(row["timestamp_ms"]),
                "title": str(row.get("title", "")),
                "subtitle": str(row.get("subtitle", "")),
                "content": str(row.get("content", "")),
                "author": str(row.get("author", "")),
                "author_username": str(row.get("author_username", "")),
                "like_count": int(row.get("like_count", 0)),
                "comment_count": int(row.get("comment_count", 0)),
                "view_count": int(row.get("view_count", 0)),
                "share_count": int(row.get("share_count", 0)),
                "related_symbols": str(row.get("related_symbols", "")),
                "link": str(row.get("link", "")),
            }
        )

    if rows:
        import io
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        csv_path.write_text(buf.getvalue(), encoding="utf-8-sig")

    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(rows)


# ---------------------------------------------------------------------------
# 统一采集逻辑
# ---------------------------------------------------------------------------

def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def crawl_unified(
    session: requests.Session,
    symbols: list[str],
    max_posts_per_symbol: int,
    max_pages: int,
    page_size: int,
    timeout: int,
    pause_seconds: float,
    min_comment_count: int,
    min_like_count: int,
    min_age_days: int,
    idle_stop_pages: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """统一翻页采集，对所有币种同时做关键词匹配。

    返回 (入库帖子列表, 统计信息)。
    """
    all_posts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    symbol_counts: dict[str, int] = {s: 0 for s in symbols}
    idle_pages = 0
    stats = {
        "pages_done": 0,
        "total_api_items": 0,
        "quality_filtered": 0,
        "keyword_miss": 0,
        "stopped_reason": "max_pages",
    }

    for page in range(1, max_pages + 1):
        # 检查是否所有币种都已达标
        all_satisfied = all(symbol_counts[s] >= max_posts_per_symbol for s in symbols)
        if all_satisfied:
            stats["stopped_reason"] = "all_symbols_target_reached"
            break

        try:
            items = fetch_page(session, page, page_size, timeout)
        except Exception as exc:
            print(f"[page={page}] API error: {exc}")
            stats["stopped_reason"] = f"api_error_page_{page}"
            break

        stats["pages_done"] = page
        stats["total_api_items"] += len(items)

        if not items:
            stats["stopped_reason"] = "api_returned_empty"
            break

        page_matches = 0

        for item in items:
            pid = str(item.get("id", ""))
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)

            title = clean_text(item.get("title", ""))
            subtitle = clean_text(item.get("subTitle", ""))

            # 关键词匹配
            matched = match_symbols(title, subtitle, symbols)
            if not matched:
                stats["keyword_miss"] += 1
                continue

            page_matches += len(matched)

            # 只匹配那些尚未达标的币种
            eligible_symbols = [
                s for s in matched if symbol_counts[s] < max_posts_per_symbol
            ]
            if not eligible_symbols:
                continue

            normalized = normalize_post(item, matched)

            # 质量过滤
            passed, reasons = evaluate_post_quality(
                normalized, min_comment_count, min_like_count, min_age_days
            )
            if not passed:
                stats["quality_filtered"] += 1
                continue

            all_posts.append(normalized)
            for s in matched:
                if symbol_counts[s] < max_posts_per_symbol:
                    symbol_counts[s] += 1

        # idle 检测
        if page_matches == 0:
            idle_pages += 1
        else:
            idle_pages = 0

        # 进度报告
        count_str = " ".join(f"{s}={symbol_counts[s]}" for s in symbols)
        print(
            f"[page={page}/{max_pages}] items={len(items)} "
            f"keyword_matched={page_matches} "
            f"counts: {count_str}"
            f"{' [idle=' + str(idle_pages) + ']' if idle_pages > 0 else ''}"
        )

        if idle_pages >= idle_stop_pages:
            stats["stopped_reason"] = f"idle_{idle_pages}_pages"
            break

        time.sleep(max(0.0, pause_seconds))

    stats["final_symbol_counts"] = symbol_counts
    return all_posts, stats


# ---------------------------------------------------------------------------
# check-only 模式
# ---------------------------------------------------------------------------

def run_check_only(
    session: requests.Session,
    symbols: list[str],
    timeout: int,
    lang: str,
    sample_pages: int = 5,
) -> None:
    print("=" * 60)
    print("[check] API 连通性检查 + 币种命中率估算")
    print("=" * 60)

    total_items = 0
    symbol_hits: dict[str, int] = {s: 0 for s in symbols}
    samples: list[str] = []

    for page in range(1, sample_pages + 1):
        try:
            items = fetch_page(session, page, 20, timeout)
        except Exception as exc:
            print(f"[check] page={page} API error: {exc}")
            break
        if not items:
            print(f"[check] page={page} empty, stop")
            break
        total_items += len(items)
        for item in items:
            title = clean_text(item.get("title", ""))
            subtitle = clean_text(item.get("subTitle", ""))
            matched = match_symbols(title, subtitle, symbols)
            for s in matched:
                symbol_hits[s] += 1
            if matched and len(samples) < 10:
                samples.append(f"[{','.join(matched)}] {title[:80]}")
        print(f"[check] page={page} items={len(items)} total={total_items}")

    print(f"\n[check] 共扫描 {total_items} 条新闻 ({sample_pages} 页)")
    print(f"[check] 各币种命中率:")
    for sym in symbols:
        count = symbol_hits[sym]
        rate = 100 * count / total_items if total_items > 0 else 0
        print(f"  {sym}: {count} 条 ({rate:.1f}%)")

    if samples:
        print(f"\n[check] 匹配样本:")
        for s in samples:
            print(f"  {s}")

    # 估算需要翻多少页才能达到目标
    print(f"\n[check] 翻页估算 (按目标 200 条/币种):")
    for sym in symbols:
        count = symbol_hits[sym]
        rate = count / total_items if total_items > 0 else 0
        if rate > 0:
            pages_needed = int(200 / (rate * 20)) + 1
            print(f"  {sym}: 命中率 {rate:.1%}, 约需 {pages_needed} 页")
        else:
            print(f"  {sym}: 命中率 0%, 建议调整关键词或语言")


# ---------------------------------------------------------------------------
# HTML 下载衔接
# ---------------------------------------------------------------------------

def run_html_fetch_stage(
    db_path: Path,
    output_dir: Path,
    html_limit: int,
    headless: bool,
    user_data_dir: str,
) -> None:
    fetch_script = Path(__file__).resolve().with_name("fetch_pages_from_db.py")
    if not fetch_script.exists():
        print(f"[warn] fetch_pages_from_db.py 未找到: {fetch_script}，跳过 HTML 下载")
        return

    command = [
        sys.executable,
        str(fetch_script),
        "--db-path", str(db_path),
        "--output-dir", str(output_dir),
        "--limit", str(html_limit),
        "--user-data-dir", str(user_data_dir),
        "--pause-seconds", "0.8",
        "--timeout-seconds", "60",
    ]
    if headless:
        command.append("--headless")

    print(f"[html-stage] command: {' '.join(command)}")
    subprocess.run(command, check=True)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        raise SystemExit("错误: --symbols 不能为空")

    output_dir = Path(args.output_dir)
    ensure_dir(output_dir)
    db_path = Path(args.db_path) if args.db_path else output_dir / DEFAULT_DB_NAME
    csv_path = output_dir / "coin_posts.csv"
    json_path = output_dir / "coin_posts.json"
    run_summary_path = output_dir / "crawler_coin_last_run.json"

    session = build_session(args.lang, args.retries, trust_env_proxy=args.trust_env_proxy)

    # Cookie 预热
    warmup_session(session, args.lang, args.request_timeout)

    # --check-only
    if args.check_only:
        run_check_only(session, symbols, args.request_timeout, args.lang)
        return

    # === 正式采集 ===
    print("=" * 60)
    print(f"[crawler_coin] 目标币种: {symbols}")
    print(f"[crawler_coin] 每币种最大: {args.max_posts}  |  最大翻页: {args.max_pages}")
    print(f"[crawler_coin] 质量过滤: min_comments={args.min_comment_count}  "
          f"min_likes={args.min_like_count}  min_age_days={args.min_post_age_days}")
    print(f"[crawler_coin] 闲置停止: {args.idle_stop_pages} 页无匹配即停")
    print("=" * 60)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    init_db(conn)

    started_at = now_text()
    existing_before = count_posts(conn)
    print(f"[crawler_coin] DB 中已有帖子: {existing_before}")

    # 检查哪些币种已经达标
    for s in symbols:
        already = count_posts_for_symbol(conn, s)
        if already >= args.max_posts:
            print(f"[crawler_coin] {s} 已有 {already} 条, 已达目标 {args.max_posts}，跳过后续采集（仍参与匹配）")

    posts, stats = crawl_unified(
        session=session,
        symbols=symbols,
        max_posts_per_symbol=args.max_posts,
        max_pages=args.max_pages,
        page_size=args.page_size,
        timeout=args.request_timeout,
        pause_seconds=args.pause_seconds,
        min_comment_count=args.min_comment_count,
        min_like_count=args.min_like_count,
        min_age_days=args.min_post_age_days,
        idle_stop_pages=args.idle_stop_pages,
    )

    new_added = 0
    if posts:
        new_added = insert_or_update_posts(conn, posts, now_text())

    total_after = count_posts(conn)
    ended_at = now_text()

    # 导出
    exported = export_posts(conn, symbols, csv_path, json_path)
    print(f"\n[crawler_coin] 导出 {exported} 条到 {csv_path} 和 {json_path}")

    # 各币种最终数量
    final_counts = {}
    for s in symbols:
        final_counts[s] = count_posts_for_symbol(conn, s)
    print(f"[crawler_coin] 各币种最终数量: {final_counts}")

    # 运行摘要
    summary = {
        "started_at": started_at,
        "ended_at": ended_at,
        "symbols": symbols,
        "target_per_symbol": args.max_posts,
        "min_comment_count": args.min_comment_count,
        "min_like_count": args.min_like_count,
        "existing_before": existing_before,
        "new_added": new_added,
        "total_after": total_after,
        "exported_rows": exported,
        "db_path": str(db_path),
        "csv": str(csv_path),
        "json": str(json_path),
        "stopped_reason": stats["stopped_reason"],
        "pages_done": stats["pages_done"],
        "total_api_items": stats["total_api_items"],
        "quality_filtered": stats["quality_filtered"],
        "keyword_miss": stats["keyword_miss"],
        "final_symbol_counts": final_counts,
    }
    run_summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # runs 表
    conn.execute(
        """
        INSERT INTO runs(started_at, ended_at, symbols, max_pages,
                         min_comment_count, min_like_count, pages_done,
                         new_added, total_posts_after, stop_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            started_at,
            ended_at,
            args.symbols,
            args.max_pages,
            args.min_comment_count,
            args.min_like_count,
            stats["pages_done"],
            new_added,
            total_after,
            stats["stopped_reason"],
        ),
    )
    conn.commit()
    conn.close()

    print(f"\n[crawler_coin] 完成. stop_reason={stats['stopped_reason']}  "
          f"翻页={stats['pages_done']}  新增入库={new_added}  总计={total_after}")

    # HTML 下载
    if args.fetch_html:
        html_output = output_dir / "html_pages"
        run_html_fetch_stage(
            db_path=db_path,
            output_dir=html_output,
            html_limit=args.html_limit,
            headless=args.headless,
            user_data_dir=args.user_data_dir,
        )


if __name__ == "__main__":
    main()
