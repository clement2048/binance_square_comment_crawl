from __future__ import annotations
import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd
import requests

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


NEWS_API = "https://www.binance.com/bapi/composite/v4/friendly/pgc/feed/news/list"
OUTPUT_DIR = Path("./update_news")
NEWS_CSV = OUTPUT_DIR / "binance_news.csv"
COMMENTS_CSV = OUTPUT_DIR / "binance_comments.csv"
RAW_JSON = OUTPUT_DIR / "binance_news_raw.json"


def safe_to_csv(df: pd.DataFrame, path: Path) -> Path:
    """将 DataFrame 写入 CSV；若目标文件被占用则自动落盘到带时间戳的新文件。"""
    try:
        df.to_csv(path, index=False, encoding="utf-8-sig")
        return path
    except PermissionError:
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        fallback = path.with_name(f"{path.stem}_{ts}{path.suffix}")
        df.to_csv(fallback, index=False, encoding="utf-8-sig")
        print(f"[warn] 文件被占用，已写入新文件: {fallback}")
        return fallback


# 参数解析
def parse_args() -> argparse.Namespace:
    """解析命令行参数，控制新闻抓取范围与评论抓取策略。"""
    parser = argparse.ArgumentParser(
        description="抓取币安广场新闻和评论（评论通过页面渲染抓取）。"
    )
    parser.add_argument("--pages", type=int, default=3, help="新闻页数，默认 3")
    parser.add_argument("--page-size", type=int, default=20, help="每页条数，默认 20")
    parser.add_argument("--max-posts", type=int, default=50, help="最多处理新闻条数，默认 50")
    parser.add_argument("--max-comments", type=int, default=30, help="每条新闻最多抓取评论条数，默认 30")
    parser.add_argument(
        "--min-comment-count",
        type=int,
        default=1,
        help="仅抓取评论数不小于该值的帖子，默认 1",
    )
    parser.add_argument(
        "--lang",
        type=str,
        default="zh-CN",
        help="请求语言（zh-CN/en），默认 zh-CN",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="启用无头浏览器（服务器环境建议开启）",
    )
    parser.add_argument(
        "--skip-comments",
        action="store_true",
        help="仅抓新闻，不抓评论（调试接口时可用）",
    )
    return parser.parse_args()


def parse_ts(value: Any) -> str:
    """将秒/毫秒时间戳转换为可读时间字符串。"""
    if value is None:
        return ""
    ts = int(value)
    if ts > 10**12:
        d = dt.datetime.fromtimestamp(ts / 1000)
    else:
        d = dt.datetime.fromtimestamp(ts)
    return d.strftime("%Y-%m-%d %H:%M:%S")


def build_session(lang: str) -> requests.Session:
    """构建带有必要请求头的会话，减少被目标站点拒绝的概率。"""
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": f"{lang},{lang.split('-')[0]};q=0.9",
            "Referer": f"https://www.binance.com/{lang}/square/news/all",
            "Origin": "https://www.binance.com",
            "clienttype": "web",
            "lang": lang,
        }
    )
    return s


def fetch_news(session: requests.Session, pages: int, page_size: int) -> list[dict[str, Any]]:
    """分页调用新闻接口并汇总返回结果。"""
    results: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        params = {
            "pageIndex": page,
            "pageSize": page_size,
            "strategy": 6,
            "tagId": 0,
            "featured": "false",
        }
        resp = session.get(NEWS_API, params=params, timeout=20)
        resp.raise_for_status()
        payload = resp.json()
        vos = payload.get("data", {}).get("vos", [])
        print(f"[news] page={page}, count={len(vos)}")
        if not vos:
            break
        results.extend(vos)
    return results


def clean_text(text: str) -> str:
    """规整文本空白，避免换行/多空格影响后续去重和过滤。"""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text


def looks_like_comment_text(text: str) -> bool:
    """判断一段文本是否像真实评论，过滤时间戳、按钮文案等噪音。"""
    if not text:
        return False

    # 过滤纯数字、数字+标点、时间标签等元信息
    if re.fullmatch(r"[\d\s,.:/+-]+", text):
        return False
    if re.fullmatch(r"\d+[smhdw]", text.lower()):
        return False
    if re.fullmatch(r"\d+[秒分分钟小时天周月年]前", text):
        return False

    # 过滤常见操作文案
    blocked = {
        "点赞",
        "回复",
        "分享",
        "查看更多回复",
        "查看全部回复",
        "发布",
        "作者",
        "置顶",
    }
    if text in blocked:
        return False

    return True


def extract_comment_texts_from_payload(payload: Any) -> list[str]:
    """从任意层级 JSON 中递归提取疑似评论文本字段。"""
    texts: list[str] = []
    candidate_keys = {
        "content",
        "comment",
        "commentcontent",
        "commenttext",
        "text",
        "message",
        "body",
    }

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and k.lower() in candidate_keys:
                    t = clean_text(v)
                    if looks_like_comment_text(t):
                        texts.append(t)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return texts


def extract_comments_with_browser(
    urls: list[str],
    max_comments: int,
    headless: bool,
) -> list[dict[str, Any]]:
    """使用 Playwright 打开帖子页并抓取评论（接口监听 + DOM 兜底）。"""
    if sync_playwright is None:
        raise RuntimeError(
            "未安装 playwright。请先执行: pip install playwright && playwright install chromium"
        )

    comment_selectors = [
        "[data-testid*='comment-content']",
        "[class*='comment-content']",
        "[class*='CommentContent']",
        "[data-testid*='comment']",
        "[class*='comment-item']",
        "[class*='CommentItem']",
        "[class*='commentItem']",
        "[class*='comment']",
    ]
    rows: list[dict[str, Any]] = []

    comment_entry_selectors = [
        "button:has-text('评论')",
        "button:has-text('Comment')",
        "[data-testid*='comment']",
        "[class*='comment-btn']",
        "[class*='commentButton']",
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 1366, "height": 1600})

        for idx, url in enumerate(urls, start=1):
            captured_api_comments: list[str] = []
            api_hit_count = 0

            def on_response(resp: Any) -> None:
                # 站点接口路径可能变更，保留更宽松匹配，避免漏掉评论接口。
                nonlocal api_hit_count
                if "comment" not in resp.url.lower():
                    return
                try:
                    js = resp.json()
                except Exception:
                    return
                api_hit_count += 1
                captured_api_comments.extend(extract_comment_texts_from_payload(js))

            page.on("response", on_response)

            print(f"[comment] ({idx}/{len(urls)}) {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3500)
            page_title = ""
            page_content_len = 0
            try:
                page_title = page.title().strip()
                page_content_len = len(page.content())
            except Exception:
                pass

            # 某些页面需要先点击评论入口才会发起评论请求或渲染评论 DOM。
            clicked_comment_entry = False
            for sel in comment_entry_selectors:
                try:
                    btn = page.locator(sel).first
                    if btn.count() > 0:
                        btn.click(timeout=2500)
                        page.wait_for_timeout(1200)
                        clicked_comment_entry = True
                        break
                except Exception:
                    continue
            if not clicked_comment_entry:
                print("[comment][debug] 未找到可点击的评论入口，继续尝试滚动触发加载")

            # 滚动几次，触发评论区域加载
            for _ in range(5):
                page.mouse.wheel(0, 2400)
                page.wait_for_timeout(1200)

            matched = []
            if captured_api_comments:
                matched = [clean_text(x) for x in captured_api_comments if looks_like_comment_text(clean_text(x))]
            else:
                selector_match_count = 0
                for sel in comment_selectors:
                    loc = page.locator(sel)
                    count = loc.count()
                    if count:
                        selector_match_count += count
                        candidates: list[str] = []
                        for raw_block in loc.all_inner_texts():
                            for line in raw_block.splitlines():
                                text = clean_text(line)
                                if looks_like_comment_text(text):
                                    candidates.append(text)
                        matched = candidates
                        if matched:
                            break
                if not matched:
                    print(
                        "[comment][debug] no comments found | "
                        f"api_hits={api_hit_count}, api_candidates={len(captured_api_comments)}, "
                        f"selector_nodes={selector_match_count}, "
                        f"title={page_title!r}, content_len={page_content_len}"
                    )

            # 去重并截断
            uniq = []
            seen = set()
            for item in matched:
                if item in seen:
                    continue
                seen.add(item)
                uniq.append(item)
                if len(uniq) >= max_comments:
                    break

            post_id = url.rstrip("/").split("/")[-1]
            for i, comment_text in enumerate(uniq, start=1):
                rows.append(
                    {
                        "post_id": post_id,
                        "comment_id": f"{post_id}_{i}",
                        "comment_text": comment_text,
                        "source_url": url,
                    }
                )
            # 解除监听，避免后续页面的请求干扰数据收集
            page.remove_listener("response", on_response)

        browser.close()

    return rows


def main() -> None:
    """主流程：抓新闻 -> 抓评论 -> 生成情绪分析数据集。"""
    args = parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    session = build_session(args.lang)
    news_payload = fetch_news(session, pages=args.pages, page_size=args.page_size)
    news_payload = news_payload[: args.max_posts]

    RAW_JSON.write_text(
        json.dumps(news_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    news_rows = []
    for item in news_payload:
        post_id = str(item.get("id", ""))
        title = clean_text(item.get("title", ""))
        subtitle = clean_text(item.get("subTitle", ""))
        content = clean_text(f"{title} {subtitle}")
        news_rows.append(
            {
                "post_id": post_id,
                "time": parse_ts(item.get("date")),
                "title": title,
                "subtitle": subtitle,
                "content": content,
                "author": item.get("authorName", ""),
                "like_count": item.get("likeCount", 0),
                "comment_count": item.get("commentCount", 0),
                "view_count": item.get("viewCount", 0),
                "link": item.get("webLink", ""),
            }
        )

    news_df = pd.DataFrame(news_rows)
    news_df = news_df.drop_duplicates(subset=["post_id"], keep="first")
    news_saved_path = safe_to_csv(news_df, NEWS_CSV)
    print(f"[ok] news saved: {news_saved_path} ({len(news_df)} rows)")

    if args.skip_comments:
        comment_rows = []
        print("[warn] 已启用 --skip-comments，跳过评论抓取")
    else:
        # comment_count 来源于新闻列表元信息，仅用于筛选候选帖子。
        # 真实评论能否抓到取决于页面实际加载行为与接口返回内容。
        urls = [
            x["link"]
            for x in news_rows
            if x.get("link") and int(x.get("comment_count", 0) or 0) >= args.min_comment_count
        ]
        print(f"[comment] candidate posts: {len(urls)} (min_comment_count={args.min_comment_count})")
        comment_rows = extract_comments_with_browser(
            urls=urls,
            max_comments=args.max_comments,
            headless=args.headless,
        )

    comments_df = pd.DataFrame(comment_rows)
    if comments_df.empty:
        comments_df = pd.DataFrame(columns=["post_id", "comment_id", "comment_text", "source_url"])
    comments_saved_path = safe_to_csv(comments_df, COMMENTS_CSV)
    print(f"[ok] comments saved: {comments_saved_path} ({len(comments_df)} rows)")

    merged_df = comments_df.merge(
        news_df[["post_id", "time", "title", "content", "link"]],
        on="post_id",
        how="left",
    )
    merged_path = OUTPUT_DIR / "binance_news_comments_for_sentiment.csv"
    merged_saved_path = safe_to_csv(merged_df, merged_path)
    print(f"[ok] sentiment dataset saved: {merged_saved_path} ({len(merged_df)} rows)")


if __name__ == "__main__":
    main()