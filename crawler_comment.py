from __future__ import annotations

import re
from typing import Any

from crawler_util import clean_text
from crawler_util import extract_first_number
from crawler_util import extract_first_string
from crawler_util import is_meaningful_comment


def extract_comment_texts_from_payload(payload: Any) -> list[str]:
    """Recursively extract candidate comment text fields from API payload."""
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


def click_comment_entry(page: Any) -> bool:
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


def extract_comments_from_dom(page: Any) -> list[str]:
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


def extract_comment_cards_from_dom(
    page: Any,
    post_id: str,
    source_url: str,
    max_comments: int,
) -> list[dict[str, Any]]:
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


def extract_post_meta_from_page(page: Any) -> dict[str, Any]:
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
