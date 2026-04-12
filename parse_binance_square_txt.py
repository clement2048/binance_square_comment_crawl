from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any


TIME_PATTERN = re.compile(r"^\d+\s*(秒钟?|分钟|小时|天|周|月|年)$")
USERNAME_PATTERN = re.compile(r"^@[A-Za-z0-9_.-]+$")
PAIR_PATTERN = re.compile(r"\b[A-Z0-9]{2,15}USDT\b")
SYMBOL_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]{1,9}\b")
FOOTER_MARKERS = {
    "相关创作者",
    "网站地图",
    "Cookie偏好设置",
    "平台条款和条件",
    "我们使用 Cookie",
    "接受所有 Cookie",
    "全部拒绝",
    "Cookie 设置",
}
NAV_MARKERS = {
    "自动翻译",
    "发现",
    "正在关注",
    "新闻",
    "通知",
    "个人主页",
    "书签",
    "聊天",
    "历史记录",
    "创作者中心",
    "设置",
    "发文",
    "短帖",
}
ENGAGEMENT_LINE = re.compile(r"^(回复\s*\d+|引用\s*\d+|最相关|\d+(\.\d+)?k?)$")
COMMENT_SPLITTER = re.compile(r"^\S.*$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="解析 Binance Square 页面导出的 txt 文件。")
    parser.add_argument(
        "--input",
        default="update_news/binance_square_page_dump",
        help="输入 txt 文件或目录，默认 update_news/binance_square_page_dump",
    )
    parser.add_argument(
        "--output-dir",
        default="update_news/parsed_from_txt",
        help="输出目录，默认 update_news/parsed_from_txt",
    )
    return parser.parse_args()


def clean_line(line: str) -> str:
    return re.sub(r"\s+", " ", (line or "")).strip()


def read_txt_paths(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    return sorted(path for path in input_path.glob("*.txt") if path.is_file())


def is_probable_author_line(line: str) -> bool:
    if not line or line in NAV_MARKERS or line in FOOTER_MARKERS:
        return False
    if USERNAME_PATTERN.match(line):
        return False
    if TIME_PATTERN.match(line):
        return False
    if line.startswith("回复") or line.startswith("引用") or line == "最相关":
        return False
    if "免责声明" in line:
        return False
    if len(line) > 40:
        return False
    return True


def find_post_header(lines: list[str]) -> tuple[int, str, str]:
    short_post_markers = {"短帖", "鐭笘"}
    for idx, line in enumerate(lines):
        if line not in short_post_markers:
            continue
        for look_ahead in range(idx + 1, min(idx + 8, len(lines) - 1)):
            author = lines[look_ahead]
            time_text = lines[look_ahead + 1]
            if is_probable_author_line(author) and TIME_PATTERN.match(time_text):
                author_username = ""
                for back in range(max(0, idx - 3), idx):
                    if USERNAME_PATTERN.match(lines[back]):
                        author_username = lines[back].lstrip("@")
                        break
                return look_ahead, author, author_username

    for idx in range(len(lines) - 2):
        line = lines[idx]
        next_line = lines[idx + 1]
        next_next = lines[idx + 2]
        if (
            is_probable_author_line(line)
            and USERNAME_PATTERN.match(next_line)
            and TIME_PATTERN.match(next_next)
        ):
            return idx, line, next_line.lstrip("@")
    raise ValueError("未找到帖子头部信息")


def collect_post_body(lines: list[str], start_idx: int) -> tuple[list[str], int]:
    body: list[str] = []
    idx = start_idx
    while idx < len(lines):
        line = lines[idx]
        if not line:
            idx += 1
            continue
        if line == "最相关":
            break
        if line.startswith("免责声明："):
            body.append(line)
            idx += 1
            continue
        if line in FOOTER_MARKERS:
            break
        body.append(line)
        idx += 1
    return body, idx


def split_post_body_and_meta(body_lines: list[str]) -> tuple[list[str], dict[str, Any]]:
    meta: dict[str, Any] = {
        "trade_pair": "",
        "symbols": [],
        "reply_count": "",
        "quote_count": "",
        "like_count": "",
        "view_count": "",
        "folded_comment_marker": "",
    }
    content_lines: list[str] = []

    for line in body_lines:
        if line.startswith("回复 "):
            meta["reply_count"] = line.replace("回复", "").strip()
            continue
        if line.startswith("引用 "):
            meta["quote_count"] = line.replace("引用", "").strip()
            continue
        if line == "展示被折叠的评论":
            meta["folded_comment_marker"] = line
            continue
        if PAIR_PATTERN.search(line) and not meta["trade_pair"]:
            meta["trade_pair"] = PAIR_PATTERN.search(line).group(0)
        content_lines.append(line)

    numbers = [line for line in body_lines if re.fullmatch(r"\d+(\.\d+)?k?", line, flags=re.I)]
    if len(numbers) >= 3:
        meta["comment_count"] = numbers[0]
        meta["like_count"] = numbers[1]
        meta["view_count"] = numbers[2]

    symbols: list[str] = []
    joined = "\n".join(body_lines)
    for pair in PAIR_PATTERN.findall(joined):
        if pair not in symbols:
            symbols.append(pair)
    for symbol in SYMBOL_PATTERN.findall(joined):
        if symbol in {
            "USDT",
            "Cookie",
        }:
            continue
        if symbol not in symbols and len(symbol) <= 10:
            symbols.append(symbol)
    meta["symbols"] = symbols
    return content_lines, meta


def parse_comments(lines: list[str], start_idx: int) -> list[dict[str, str]]:
    if start_idx >= len(lines):
        return []

    comments: list[dict[str, str]] = []
    idx = start_idx
    if idx < len(lines) and lines[idx] == "最相关":
        idx += 1

    while idx < len(lines):
        line = lines[idx]
        if not line:
            idx += 1
            continue
        if line in FOOTER_MARKERS:
            break
        if line == "展示被折叠的评论":
            idx += 1
            continue

        author = line
        if not is_probable_author_line(author):
            idx += 1
            continue

        if idx + 2 >= len(lines) or lines[idx + 1] != "·" or not TIME_PATTERN.match(lines[idx + 2]):
            idx += 1
            continue

        time_text = lines[idx + 2]
        idx += 3

        comment_lines: list[str] = []
        while idx < len(lines):
            current = lines[idx]
            if not current:
                idx += 1
                continue
            if current in FOOTER_MARKERS:
                break
            if current == "查看翻译":
                idx += 1
                continue
            if idx + 2 < len(lines) and lines[idx + 1] == "·" and TIME_PATTERN.match(lines[idx + 2]):
                break
            if ENGAGEMENT_LINE.match(current):
                idx += 1
                continue
            if current == "展示被折叠的评论":
                idx += 1
                break
            comment_lines.append(current)
            idx += 1

        comment_text = clean_line(" ".join(comment_lines))
        if comment_text:
            comments.append(
                {
                    "comment_author": author,
                    "comment_time": time_text,
                    "comment_text": comment_text,
                }
            )

    return comments


def parse_txt_file(path: Path) -> dict[str, Any]:
    lines = [clean_line(line) for line in path.read_text(encoding="utf-8").splitlines()]
    lines = [line for line in lines if line]

    try:
        header_idx, author, author_username = find_post_header(lines)
    except ValueError as exc:
        preview = "\n".join(lines[:80])
        raise ValueError(f"{exc}\n文件: {path}\n预览:\n{preview}") from exc
    post_time = lines[header_idx + 2]
    body_lines, comment_start_idx = collect_post_body(lines, header_idx + 3)
    content_lines, meta = split_post_body_and_meta(body_lines)
    comments = parse_comments(lines, comment_start_idx)

    disclaimer = ""
    content_only: list[str] = []
    for line in content_lines:
        if line.startswith("免责声明："):
            disclaimer = line
        else:
            content_only.append(line)

    return {
        "source_file": str(path),
        "post_id": path.stem.replace("_after_login", ""),
        "post_author": author,
        "post_author_username": author_username,
        "post_time": post_time,
        "post_content": "\n".join(content_only).strip(),
        "disclaimer": disclaimer,
        "trade_pair": meta.get("trade_pair", ""),
        "related_symbols": ",".join(meta.get("symbols", [])),
        "comment_count_hint": meta.get("comment_count", ""),
        "like_count_hint": meta.get("like_count", ""),
        "view_count_hint": meta.get("view_count", ""),
        "reply_count_hint": meta.get("reply_count", ""),
        "quote_count_hint": meta.get("quote_count", ""),
        "has_folded_comments": bool(meta.get("folded_comment_marker")),
        "comments": comments,
    }


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    txt_paths = read_txt_paths(input_path)
    if not txt_paths:
        raise SystemExit(f"没有找到可解析的 txt 文件: {input_path}")

    parsed_posts: list[dict[str, Any]] = []
    parsed_comments: list[dict[str, Any]] = []

    for txt_path in txt_paths:
        if txt_path.stem.endswith("_after_login"):
            target_path = txt_path
        else:
            after_login = txt_path.with_name(f"{txt_path.stem}_after_login{txt_path.suffix}")
            target_path = after_login if after_login.exists() else txt_path

        parsed = parse_txt_file(target_path)
        parsed_posts.append(
            {
                "source_file": parsed["source_file"],
                "post_id": parsed["post_id"],
                "post_author": parsed["post_author"],
                "post_author_username": parsed["post_author_username"],
                "post_time": parsed["post_time"],
                "post_content": parsed["post_content"],
                "disclaimer": parsed["disclaimer"],
                "trade_pair": parsed["trade_pair"],
                "related_symbols": parsed["related_symbols"],
                "comment_count_hint": parsed["comment_count_hint"],
                "like_count_hint": parsed["like_count_hint"],
                "view_count_hint": parsed["view_count_hint"],
                "reply_count_hint": parsed["reply_count_hint"],
                "quote_count_hint": parsed["quote_count_hint"],
                "has_folded_comments": parsed["has_folded_comments"],
            }
        )

        for idx, comment in enumerate(parsed["comments"], start=1):
            parsed_comments.append(
                {
                    "post_id": parsed["post_id"],
                    "comment_id": f"{parsed['post_id']}_{idx}",
                    "comment_author": comment["comment_author"],
                    "comment_time": comment["comment_time"],
                    "comment_text": comment["comment_text"],
                }
            )

    posts_json = output_dir / "binance_square_posts_from_txt.json"
    comments_csv = output_dir / "binance_square_comments_from_txt.csv"
    posts_csv = output_dir / "binance_square_posts_from_txt.csv"

    posts_json.write_text(json.dumps(parsed_posts, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(
        posts_csv,
        parsed_posts,
        fieldnames=[
            "source_file",
            "post_id",
            "post_author",
            "post_author_username",
            "post_time",
            "post_content",
            "disclaimer",
            "trade_pair",
            "related_symbols",
            "comment_count_hint",
            "like_count_hint",
            "view_count_hint",
            "reply_count_hint",
            "quote_count_hint",
            "has_folded_comments",
        ],
    )
    write_csv(
        comments_csv,
        parsed_comments,
        fieldnames=["post_id", "comment_id", "comment_author", "comment_time", "comment_text"],
    )

    print(f"[ok] parsed txt files: {len(txt_paths)}")
    print(f"[ok] posts csv: {posts_csv}")
    print(f"[ok] comments csv: {comments_csv}")
    print(f"[ok] posts json: {posts_json}")


if __name__ == "__main__":
    main()
