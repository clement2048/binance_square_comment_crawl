#!/usr/bin/env python3
"""
parse_article.py — 解析 Binance 官方新闻（Article）HTML

专门处理官方新闻 Article 页面，与用户 Post 区分：
- 时间格式为 "1h", "2d" 等相对时间
- 避免误将相关文章当作评论
- 增强币种提取准确度

用法:
  python parse_article.py --input crawler_coin_output/html_pages/315747499824338.html
  python parse_article.py --batch --input crawler_coin_output/html_pages --output update_news/parsed_articles.json
"""

from __future__ import annotations

import json
import re
import html
import argparse
from urllib.parse import urljoin, urlsplit, parse_qs
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import sys
import requests

from crawler_util import timestamp_to_text


# ---------------------------------------------------------------------------
# 时间处理（Article 特有）
# ---------------------------------------------------------------------------

def relative_time_to_absolute(rel: str, base_ts_ms: int) -> int:
    """将相对时间字符串转换为绝对时间戳（毫秒）。

    支持: "1h", "2d", "1d ago", "5 minutes ago", "just now"

    base_ts_ms: 基准时间戳（如文章发布时间）
    """
    if not rel:
        return 0

    rel_lower = rel.lower().strip()

    # "just now" -> 现在
    if rel_lower in {"just now", "刚刚", "now"}:
        return base_ts_ms

    # 移除 "ago" 等后缀
    rel_lower = rel_lower.replace(" ago", "").replace("前", "")

    # 单位映射
    unit_mapping: List[tuple[str, int]] = [
        ("s", 1), ("sec", 1), ("second", 1),
        ("m", 60), ("min", 60), ("minute", 60),
        ("h", 3600), ("hour", 3600),
        ("d", 86400), ("day", 86400),
        ("w", 604800), ("week", 604800),
        ("mon", 2592000), ("month", 2592000),
    ]

    # 匹配数字和单位
    match = re.search(r'(\d+)\s*([a-z]+)', rel_lower)
    if not match:
        # 尝试纯数字（小时）
        if rel_lower.isdigit():
            hours = int(rel_lower)
            return base_ts_ms - hours * 3600 * 1000
        # 无法解析
        return 0

    num = int(match.group(1))
    unit_str = match.group(2)

    # 查找单位
    for unit, multiplier in unit_mapping:
        if unit_str.startswith(unit):
            seconds_ago = num * multiplier
            return base_ts_ms - seconds_ago * 1000

    return base_ts_ms  # 无法解析则用基时间


def parse_datetime_absolute_or_relative(
    datetime_str: Any,
    post_ts_ms: int = 0,
) -> tuple[str, int]:
    """解析时间字符串，支持绝对/相对格式，返回 (可读时间字符串, 毫秒时间戳)。"""
    if not datetime_str:
        return "", 0

    dt_str = str(datetime_str).strip()

    # 尝试解析为绝对时间（时间戳或 ISO 格式）
    try:
        if dt_str.isdigit():
            ts_int = int(dt_str)
            if ts_int == 0:
                return "", 0
            elif ts_int > 10**12:  # 毫秒级
                ms = ts_int
            else:  # 秒级
                ms = ts_int * 1000
            dt = datetime.fromtimestamp(ms / 1000)
            return dt.strftime("%Y-%m-%d %H:%M:%S"), ms
        else:
            # 尝试解析 ISO 8601 格式
            cleaned_dt = re.sub(r'\\.\\d+', '', dt_str)  # 移除毫秒
            cleaned_dt = cleaned_dt.replace('Z', '')  # 移除Z时区标记
            dt = datetime.fromisoformat(cleaned_dt)
            ms = int(dt.timestamp() * 1000)
            return dt.strftime("%Y-%m-%d %H:%M:%S"), ms
    except (ValueError, TypeError):
        # 尝试作为相对时间解析
        if post_ts_ms > 0:
            ts_ms = relative_time_to_absolute(dt_str, post_ts_ms)
            if ts_ms > 0:
                dt = datetime.fromtimestamp(ts_ms / 1000)
                return dt.strftime("%Y-%m-%d %H:%M:%S"), ts_ms

    # 解析失败，返回原始字符串和0时间戳
    return dt_str, 0


# ---------------------------------------------------------------------------
# 基础解析函数（复用原解析器）
# ---------------------------------------------------------------------------

def extract_app_data(html_content: str) -> Optional[Dict[str, Any]]:
    """提取__APP_DATA中的JSON数据"""
    pattern = r'<script[^>]*id="__APP_DATA"[^>]*type="application/json"[^>]*>(.*?)</script>'
    match = re.search(pattern, html_content, re.DOTALL)

    if match:
        json_str = match.group(1).strip()
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            # 尝试清理
            json_str = re.split(r'</script>', json_str)[0].strip()
            try:
                return json.loads(json_str)
            except:
                return None
    return None


def extract_json_ld_data(html_content: str) -> Dict[str, Any]:
    """提取JSON-LD结构化数据（schema.org）"""
    ld_data = {}

    # 查找JSON-LD脚本
    pattern = r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'
    matches = re.findall(pattern, html_content, re.DOTALL | re.IGNORECASE)

    for json_ld_str in matches:
        try:
            data = json.loads(json_ld_str.strip())
            if isinstance(data, dict):
                # 检查是否是DiscussionForumPosting或NewsArticle类型
                if data.get("@type") in {"DiscussionForumPosting", "NewsArticle"}:
                    # 提取关键信息
                    if "headline" in data:
                        ld_data["headline"] = html.unescape(data["headline"])
                    if "text" in data:
                        ld_data["full_text"] = html.unescape(data["text"])
                    if "datePublished" in data:
                        ld_data["date_published"] = data["datePublished"]
                    if "url" in data:
                        ld_data["url"] = data["url"]

                    # 提取作者信息
                    if "author" in data and isinstance(data["author"], dict):
                        author = data["author"]
                        if "name" in author:
                            ld_data["author_name"] = html.unescape(author["name"])
                        if "url" in author:
                            ld_data["author_url"] = author["url"]

                    # 提取互动统计
                    if "interactionStatistic" in data and isinstance(data["interactionStatistic"], dict):
                        interaction = data["interactionStatistic"]
                        if "userInteractionCount" in interaction:
                            ld_data["like_count"] = interaction["userInteractionCount"]

        except json.JSONDecodeError:
            continue

    return ld_data


def extract_from_meta_tags(html_content: str) -> Dict[str, str]:
    """从HTML meta标签提取信息"""
    meta_info = {}

    # 首先提取JSON-LD结构化数据（优先级更高）
    ld_data = extract_json_ld_data(html_content)

    # 优先使用JSON-LD数据
    if "headline" in ld_data:
        meta_info["title"] = ld_data["headline"]
    if "full_text" in ld_data:
        meta_info["full_content"] = ld_data["full_text"]
    if "author_name" in ld_data:
        meta_info["author"] = ld_data["author_name"]
    if "date_published" in ld_data:
        meta_info["published_date"] = ld_data["date_published"]
    if "like_count" in ld_data:
        meta_info["likes"] = str(ld_data["like_count"])

    # 提取OG标签
    og_patterns = {
        "title": r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']*)["\'][^>]*>',
        "description": r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\']([^"\']*)["\'][^>]*>',
        "url": r'<meta[^>]*property=["\']og:url["\'][^>]*content=["\']([^"\']*)["\'][^>]*>',
    }

    for key, pattern in og_patterns.items():
        match = re.search(pattern, html_content, re.IGNORECASE)
        if match:
            # 如果JSON-LD中已有数据，则不要覆盖
            if key not in meta_info:
                meta_info[key] = html.unescape(match.group(1)).strip()

    # 从title标签提取标题
    if "title" not in meta_info:
        title_match = re.search(r'<title[^>]*>([^<]*)</title>', html_content, re.IGNORECASE)
        if title_match:
            meta_info["title"] = html.unescape(title_match.group(1)).split("|")[0].strip()

    # 生成完整的帖子内容
    # 优先使用JSON-LD的完整文本，否则使用headline + description的组合
    if "full_content" in meta_info:
        meta_info["description"] = meta_info["full_content"]
    elif "title" in meta_info and "description" in meta_info:
        # 合并标题和描述
        meta_info["description"] = f"{meta_info['title']}\n\n{meta_info['description']}"

    return meta_info


def normalize_product_symbol(raw: str) -> str:
    """规范化币种符号，返回空字符串表示无效。"""
    symbol = (raw or "").strip().upper().replace("$", "")
    symbol = re.sub(r"[^A-Z0-9]", "", symbol)
    if re.fullmatch(r"[A-Z][A-Z0-9]{1,9}", symbol):
        return symbol
    return ""


def to_base_symbol(symbol: str) -> str:
    """将交易对归一化为基础币种（如 RAVEUSDT -> RAVE）。"""
    normalized = normalize_product_symbol(symbol)
    if not normalized:
        return ""

    quote_suffixes = [
        "USDT", "USDC", "FDUSD", "BUSD", "TUSD", "USDP", "DAI",
        "TRY", "EUR", "BRL", "RUB", "UAH", "BIDR",
    ]
    for suffix in quote_suffixes:
        if normalized.endswith(suffix) and len(normalized) > len(suffix) + 1:
            candidate = normalized[: -len(suffix)]
            candidate = normalize_product_symbol(candidate)
            if candidate:
                return candidate

    return normalized


def extract_products_from_article_content(content: str) -> List[str]:
    """从 Article 内容中提取币种（增强版，适合规范新闻）。"""
    products: List[str] = []
    seen: set[str] = set()

    if not content:
        return products

    # 1. 提取所有 $SYMBOL 形式
    for symbol in re.findall(r'\$([A-Za-z][A-Za-z0-9]{1,14})\b', content):
        base = to_base_symbol(symbol)
        if base and base not in seen:
            seen.add(base)
            products.append(base)

    # 2. 大写匹配币种关键词
    uppercase_sections = re.findall(r'\b[A-Z]{2,10}\b', content)
    for token in uppercase_sections:
        if 2 <= len(token) <= 8:
            # 常见币种列表（优先）
            common_tokens = {
                "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "DOT",
                "LINK", "MATIC", "UNI", "ATOM", "LTC", "SUI", "APT", "ARB", "OP",
                "NEAR", "PEPE", "LDO", "ETHFI", "ARB", "AAVE", "MKR", "COMP",
                "SNX", "YFI", "CRV", "SUSHI", "1INCH", "ZRX", "BAL", "REN",
            }
            if token in common_tokens:
                base = to_base_symbol(token)
                if base and base not in seen:
                    seen.add(base)
                    products.append(base)
            else:
                # 检查是否为交易对
                base = to_base_symbol(token)
                if base and len(base) >= 2 and base not in seen:
                    seen.add(base)
                    products.append(base)

    return products


# ---------------------------------------------------------------------------
# 评论提取（Article 版 - 过滤相关文章）
# ---------------------------------------------------------------------------

def is_real_comment_text(text: str, author: str = "") -> bool:
    """判断是否为真实评论（而非相关文章摘要）。

    相关文章特征：
    - 包含大量数字和统计
    - 包含 "Key Takeaways"、"Summary"、"According to" 等新闻摘要关键词
    - 作者为空或明显是新闻机构
    - 文本较长但结构化
    """
    if not text or len(text) < 30:
        return False

    text_lower = text.lower()

    # 过滤新闻摘要
    news_keywords = {
        "key takeaways", "summary", "according to", "report shows",
        "data reveals", "study finds", "research indicates",
        "analysis suggests", "experts say", "official statistics",
        "market analysis", "trading volume", "price action",
        "market cap", "trading at", "up by", "down by",
        "percent", "percentage", "increase", "decrease",
    }

    for keyword in news_keywords:
        if keyword in text_lower:
            return False

    # 过滤纯数字/统计文本
    num_count = sum(c.isdigit() for c in text)
    if num_count > len(text) * 0.2:  # 数字占比超过20%
        return False

    # 过滤过长的结构化文本（可能是文章内容）
    if len(text) > 500 and ("\n" in text or "•" in text or "·" in text):
        return False

    # 真正的评论通常有第一人称、观点、情绪
    comment_keywords = {
        "i think", "i believe", "in my opinion", "personally",
        "this is", "that's", "wow", "amazing", "terrible",
        "bullish", "bearish", "pump", "dump", "moon", "rekt",
        "hodl", "lfg", "gm", "wagmi", "ngmi",
    }

    for keyword in comment_keywords:
        if keyword in text_lower:
            return True

    # 默认判断为真实评论
    return True


def load_sidecar_comments(html_file: Path) -> List[Dict[str, Any]]:
    """尝试从 sidecar JSON 文件加载评论数据（由 fetch_coin_pages.py 拦截保存）。"""
    sidecar_path = html_file.with_name(html_file.stem + "_comments.json")
    if not sidecar_path.exists():
        return []

    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    if not isinstance(data, list):
        return []

    comments = []
    for row in data:
        if not isinstance(row, dict):
            continue
        text = row.get("comment_text") or ""
        if not text:
            continue
        time_val = row.get("comment_time", "")
        if time_val:
            try:
                time_val = timestamp_to_text(int(time_val))
            except (ValueError, TypeError):
                time_val = str(time_val)
        comment = {
            "comment_id": str(row.get("comment_id", "")),
            "original_comment_id": str(row.get("comment_id", "")),
            "author": str(row.get("comment_author", "")),
            "text": text,
            "time": time_val,
            "parent_comment_id": None,
            "replies": [],
        }
        comments.append(comment)

    return comments


def extract_real_comments_from_dom(html_content: str, post_id: str) -> List[Dict[str, Any]]:
    """从 DOM 中提取真实评论（过滤相关文章）。"""
    comments: List[Dict[str, Any]] = []

    # 尝试找评论卡片（与 Post 页面可能不同）
    comment_pattern = re.compile(
        r'<div[^>]*data-id="([^"]+)"[^>]*class="([^"]*comment[^"]*)"[^>]*>',
        re.DOTALL,
    )
    card_matches = list(comment_pattern.finditer(html_content))

    if not card_matches:
        # 尝试备用模式
        comment_pattern = re.compile(
            r'<div[^>]*class="([^"]*comment-card[^"]*)"[^>]*data-id="([^"]+)"[^>]*>',
            re.DOTALL,
        )
        card_matches = list(comment_pattern.finditer(html_content))

    if not card_matches:
        # Article 可能没有评论，或者评论在另一个区域
        return comments

    for i, match in enumerate(card_matches):
        original_comment_id = match.group(1).strip()
        class_attr = match.group(2)

        start = match.end()
        end = card_matches[i + 1].start() if i + 1 < len(card_matches) else len(html_content)
        segment = html_content[start:end]

        # 提取评论信息
        author = ""
        time_text = ""
        text = ""

        # 作者匹配（简化）
        author_match = re.search(
            r'<div[^>]*class="[^"]*author[^"]*"[^>]*>([^<]+)</div>',
            segment,
            re.DOTALL,
        )
        if author_match:
            author = html.unescape(author_match.group(1)).strip()

        # 时间匹配
        time_match = re.search(
            r'<div[^>]*class="[^"]*time[^"]*"[^>]*>([^<]+)</div>',
            segment,
            re.DOTALL,
        )
        if time_match:
            time_text = html.unescape(time_match.group(1)).strip()

        # 正文匹配
        text_match = re.search(
            r'<div[^>]*class="[^"]*content[^"]*"[^>]*>([\s\S]*?)</div>',
            segment,
            re.DOTALL,
        )
        if text_match:
            raw_text = text_match.group(1)
            # 清理 HTML
            clean_text = re.sub(r'<[^>]+>', ' ', raw_text)
            clean_text = re.sub(r'\s+', ' ', clean_text).strip()
            text = html.unescape(clean_text)

        if not text or not is_real_comment_text(text, author):
            # 不是真实评论，跳过
            continue

        comment = {
            "comment_id": original_comment_id,
            "original_comment_id": original_comment_id,
            "author": author,
            "text": text,
            "time": time_text,
            "parent_comment_id": None,
            "replies": [],  # Article 评论通常没有回复
        }
        comments.append(comment)

    return comments


# ---------------------------------------------------------------------------
# 核心解析
# ---------------------------------------------------------------------------

def parse_article_file(
    html_file: Path,
    t_window_hours: int = 24,
    price_interval: str = "1h",
) -> Dict[str, Any]:
    """解析单个 Article HTML 文件（Article 专用）。"""
    print(f"解析 Article 文件: {html_file.name}")

    try:
        with open(html_file, "r", encoding="utf-8") as f:
            content = f.read()

        # 获取帖子ID
        post_id = html_file.stem

        # 从 meta 标签提取信息
        meta_info = extract_from_meta_tags(content)

        # 提取 APP_DATA
        app_data = extract_app_data(content)

        # 获取帖子发布时间
        post_time_str = meta_info.get("published_date", "")
        post_time_ms = 0

        if post_time_str:
            try:
                if post_time_str.isdigit():
                    ts_int = int(post_time_str)
                    post_time_ms = ts_int if ts_int > 10**12 else ts_int * 1000
                else:
                    cleaned_dt = re.sub(r'\.\d+', '', post_time_str).replace('Z', '')
                    dt = datetime.fromisoformat(cleaned_dt)
                    post_time_ms = int(dt.timestamp() * 1000)
            except Exception:
                post_time_ms = 0

        # 从 APP_DATA 获取帖子数据（简化版）
        post_data = {}
        if app_data and isinstance(app_data, dict):
            # 查找包含帖子数据
            def find_post_data(node: Any) -> Dict[str, Any]:
                if isinstance(node, dict):
                    node_id = str(node.get("id") or node.get("postId") or "")
                    if node_id == post_id:
                        return {
                            "title": node.get("title") or "",
                            "content": node.get("content") or node.get("plainText") or "",
                            "author": node.get("authorName") or node.get("author") or "",
                            "createTime": node.get("createTime") or node.get("date") or "",
                        }
                    for value in node.values():
                        result = find_post_data(value)
                        if result:
                            return result
                elif isinstance(node, list):
                    for item in node:
                        result = find_post_data(item)
                        if result:
                            return result
                return {}

            app_post_data = find_post_data(app_data)
            if app_post_data:
                post_data.update(app_post_data)

        # 获取评论数据
        comments = []
        real_comments = extract_real_comments_from_dom(content, post_id)

        # 如果 DOM 中没有找到评论，尝试从 APP_DATA 找
        if not real_comments and app_data:
            # 简化版的 APP_DATA 评论搜索
            def search_comments(node: Any, comments_list: List[Dict[str, Any]]) -> None:
                if isinstance(node, dict):
                    # 检查是否是评论
                    text = node.get("commentContent") or node.get("content") or ""
                    if text and isinstance(text, str) and len(text) < 500:
                        author = node.get("authorName") or ""
                        time_str = node.get("createTime") or node.get("date") or ""
                        comment_id = str(node.get("commentId") or "")

                        if comment_id and is_real_comment_text(text, author):
                            comment = {
                                "comment_id": comment_id,
                                "original_comment_id": comment_id,
                                "author": author,
                                "text": text,
                                "time": time_str,
                                "parent_comment_id": None,
                                "replies": [],
                            }
                            comments_list.append(comment)

                    # 递归搜索
                    for value in node.values():
                        search_comments(value, comments_list)

                elif isinstance(node, list):
                    for item in node:
                        search_comments(item, comments_list)

            search_comments(app_data, real_comments)

        comments = real_comments

        # 如果 DOM 和 APP_DATA 都没有评论，尝试加载 sidecar JSON
        if not comments:
            comments = load_sidecar_comments(html_file)

        print(f"  提取到 {len(comments)} 条评论")

        # 构建结果
        post_author = (
            meta_info.get("author") or
            post_data.get("author") or
            "Binance News"  # Article 作者默认
        )

        post_content = (
            meta_info.get("description") or
            post_data.get("content") or
            ""
        )

        # 解析文章发布时间
        post_time_readable, post_time_ms = parse_datetime_absolute_or_relative(
            post_data.get("createTime") or meta_info.get("published_date") or "",
            int(datetime.now().timestamp() * 1000)
        )

        # 提取币种（Article 增强版）
        products = extract_products_from_article_content(post_content)
        first_product = products[0] if products else ""

        # 提取第一个产品链接
        first_product_url = ""
        market_type = "spot"
        product_link_pattern = rf'href=["\']([^"\']*(?:/trade/|/futures/|/price/)[^"\']*\?[^"\']*contentId={re.escape(post_id)}[^"\']*)["\']'
        link_match = re.search(product_link_pattern, content, re.IGNORECASE)
        if link_match:
            url = urljoin("https://www.binance.com", html.unescape(link_match.group(1)))
            first_product_url = url
            if "/futures/" in url.lower():
                market_type = "futures"

        # 提取帖子 URL
        post_url = ""
        if "url" in meta_info and "/square/post/" in meta_info["url"]:
            post_url = meta_info["url"]
        else:
            url_pattern = rf'href=["\']([^"\']*/square/post/{re.escape(post_id)}[^"\']*)["\']'
            url_match = re.search(url_pattern, content, re.IGNORECASE)
            if url_match:
                post_url = urljoin("https://www.binance.com", html.unescape(url_match.group(1)))

        # 提取地区
        post_region = ""
        if post_url:
            region_match = re.search(r"binance\.com/([^/]+)/square/post/", post_url, re.IGNORECASE)
            if region_match:
                post_region = region_match.group(1)

        # 初始化结果
        result = {
            "source_file": str(html_file),
            "post_id": post_id,
            "post_url": post_url,
            "post_author": post_author,
            "post_content": post_content,
            "post_region": post_region,
            "post_time": post_time_readable,
            "post_time_ms": post_time_ms,
            "products": products,
            "first_product": first_product,
            "first_product_url": first_product_url,
            "market_type": market_type or "spot",
            "post_type": "article",
            "comment_num": 0,  # 根评论数
            "comment_total_num": 0,  # 含回复总数
            "comments": [],
            "label_error": "",
        }

        # 转换评论为输出格式
        id_counter = {"value": 1}
        formatted_comments = []

        # 更新评论时间戳（相对时间 -> 绝对时间）
        for comment in comments:
            comment_time_str = comment.get("time", "")
            comment_time_readable, comment_time_ms = parse_datetime_absolute_or_relative(
                comment_time_str,
                post_time_ms  # 相对时间的基准时间
            )

            formatted_comment = {
                "comment_id": f"c{id_counter['value']}",
                "original_comment_id": comment["original_comment_id"],
                "author": comment["author"],
                "text": comment["text"],
                "post_time": comment_time_readable,
                "post_time_ms": comment_time_ms,
                "replies": [],
                "is_article_comment": True,
                "comment_error": "relative_time" if not comment_time_ms and comment_time_str else "",
            }
            id_counter["value"] += 1
            formatted_comments.append(formatted_comment)

        result["comments"] = formatted_comments
        result["comment_num"] = len(formatted_comments)
        result["comment_total_num"] = len(formatted_comments)  # Article 无回复

        # 尝试价格标注（简化版，Article 可能不需要）
        if first_product and post_time_ms > 0:
            result["label_error"] = "article_relative_time"
        else:
            result["label_error"] = "missing_symbol_or_timestamp"

        return result

    except Exception as e:
        print(f"  解析出错: {e}")
        import traceback
        traceback.print_exc()

        # 返回基本结构
        return {
            "source_file": str(html_file),
            "post_id": html_file.stem,
            "post_url": "",
            "post_author": "",
            "post_content": "",
            "post_region": "",
            "post_time": "",
            "post_time_ms": 0,
            "products": [],
            "first_product": "",
            "first_product_url": "",
            "market_type": "",
            "post_type": "article",
            "comment_num": 0,
            "comment_total_num": 0,
            "comments": [],
            "label_error": f"parse_error: {e}",
        }


def parse_article_directory(
    input_dir: Path,
    t_window_hours: int = 24,
    price_interval: str = "1h",
) -> List[Dict[str, Any]]:
    """解析目录中的所有 Article HTML 文件。"""
    html_files = list(input_dir.glob("*.html"))
    if not html_files:
        raise ValueError(f"在目录中未找到 HTML 文件: {input_dir}")

    results = []
    for html_file in html_files:
        result = parse_article_file(html_file, t_window_hours=t_window_hours, price_interval=price_interval)
        if result:
            results.append(result)

    return results


def write_output(output_file: Path, data: List[Dict[str, Any]]):
    """写入输出文件"""
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"结果已写入: {output_file}")


def main():
    # 命令行参数解析
    parser = argparse.ArgumentParser(
        description="Binance Article 解析器 - 处理官方新闻 HTML"
    )

    parser.add_argument(
        "--input",
        default="update_news/binance_square_page_dump",
        help="输入 HTML 文件或目录，默认: update_news/binance_square_page_dump"
    )

    parser.add_argument(
        "--output",
        default="update_news/parsed_articles/binance_square_articles_parsed.json",
        help="输出 JSON 文件路径，默认: update_news/parsed_articles/binance_square_articles_parsed.json"
    )

    parser.add_argument(
        "--batch",
        action="store_true",
        help="批量处理目录中的所有 HTML 文件"
    )

    parser.add_argument(
        "--t-window-hours",
        type=int,
        default=24,
        help="价格窗口（小时），目前 Article 仅作占位，默认 24"
    )

    parser.add_argument(
        "--price-interval",
        default="1h",
        help="K线周期（如 1m/5m/1h），目前 Article 仅作占位，默认 1h"
    )

    args = parser.parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)

    try:
        # 批量处理目录
        if args.batch or input_path.is_dir():
            print(f"批量解析 Article 目录: {input_path}")
            results = parse_article_directory(
                input_path,
                t_window_hours=args.t_window_hours,
                price_interval=args.price_interval,
            )
            if results:
                write_output(output_path, results)
                print(f"成功解析 {len(results)} 个 Article 文件")
            else:
                print("未解析出任何结果")

        elif input_path.is_file():
            # 解析单个文件
            print(f"解析单个 Article 文件: {input_path}")
            result = parse_article_file(
                input_path,
                t_window_hours=args.t_window_hours,
                price_interval=args.price_interval,
            )
            if result:
                write_output(output_path, [result])
                print("单个文件解析完成")
            else:
                print("解析失败")

        else:
            print(f"路径不存在: {input_path}")
            sys.exit(1)

        print("\nArticle 解析完成!")

    except Exception as e:
        print(f"程序出错: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()