"""
Binance Square 文本解析器

功能：解析从Binance Square页面导出的txt文件，提取帖子信息和评论数据
输入：从浏览器开发者工具复制的页面文本
输出：JSON和CSV格式的结构化数据

处理流程：
1. 读取并清理文本行
2. 识别帖子头部（作者、用户名、时间）
3. 提取帖子正文内容
4. 分离正文和元数据（交易对、互动数据）
5. 解析评论区域
6. 输出结构化数据到文件
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any


# 正则表达式和标记定义
TIME_PATTERN = re.compile(r"^\d+\s*(秒钟?|分钟|小时|天|周|月|年)$")  # 时间格式匹配
USERNAME_PATTERN = re.compile(r"^@[A-Za-z0-9_.-]+$")  # 用户名格式匹配
PAIR_PATTERN = re.compile(r"\b[A-Z0-9]{2,15}USDT\b")  # 交易对匹配（如BTCUSDT）
SYMBOL_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]{1,9}\b")  # 代币符号匹配

# 页面底部标记，用于识别帖子内容结束位置
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

# 导航标记，用于过滤非内容部分
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

# 互动信息行匹配（回复数、引用数、点赞数等）
ENGAGEMENT_LINE = re.compile(r"^(回复\s*\d+|引用\s*\d+|最相关|\d+(\.\d+)?k?)$")
COMMENT_SPLITTER = re.compile(r"^\S.*$")


"""解析命令行参数
参数：
返回：
    argparse.Namespace: 包含--input和--output-dir参数的命名空间对象
"""
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


"""清理文本行：去除多余空白字符
参数：
    line: str - 原始文本行
返回：
    str: 清理后的文本行（去除多余空格和首尾空白）
"""
def clean_line(line: str) -> str:
    return re.sub(r"\s+", " ", (line or "")).strip()


"""读取所有待处理的txt文件路径
参数：
    input_path: Path - 输入的文件路径或目录路径
返回：
    list[Path]: 排序后的txt文件路径列表
"""
def read_txt_paths(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    return sorted(path for path in input_path.glob("*.txt") if path.is_file())


"""判断一行是否可能是作者名（而非用户名、时间等）
参数：
    line: str - 待判断的文本行
返回：
    bool: 如果是可能的作者名返回True，否则返回False
规则：
    1. 非空
    2. 不在导航和页脚标记中
    3. 不是@开头的用户名格式
    4. 不是时间格式
    5. 不包含"回复"、"引用"或"最相关"
    6. 不包含"免责声明"
    7. 长度不超过40字符
"""
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


"""在文本行中查找帖子头部信息，返回（索引, 作者名, 用户名）
参数：
    lines: list[str] - 已清理的文本行列表
返回：
    tuple[int, str, str]: (头部索引, 作者名, 用户名)
    头部索引：作者名在lines中的索引位置
    作者名：帖子的作者显示名称
    用户名：@开头的用户名（如果有）
算法：
    1. 先查找短帖标记后的作者-时间结构
    2. 再查找标准的作者-用户名-时间三行结构
异常：
    如果没有找到头部信息，抛出ValueError
"""
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


"""从指定索引开始收集帖子正文，直到遇到评论或底部标记
参数：
    lines: list[str] - 文本行列表
    start_idx: int - 开始收集的索引位置
返回：
    tuple[list[str], int]: (正文行列表, 下一行索引)
    正文行列表：收集到的帖子正文
    下一行索引：收集完成后指向下一个要处理的行索引
停止条件：
    1. 遇到"最相关"（评论开始标记）
    2. 遇到页脚标记
"""
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


"""将帖子正文内容与元数据（交易对、互动数据等）分离
参数：
    body_lines: list[str] - 帖子正文行列表
返回：
    tuple[list[str], dict[str, Any]]: (纯内容行列表, 元数据字典)
元数据包括：
    trade_pair: 交易对（如BTCUSDT）
    symbols: 相关代币符号列表
    reply_count: 回复数
    quote_count: 引用数
    like_count: 点赞数
    view_count: 查看数
    folded_comment_marker: 折叠评论标记
提取规则：
    1. "回复 "开头的行作为回复数
    2. "引用 "开头的行作为引用数  
    3. PAIR_PATTERN匹配交易对
    4. 数字格式行作为计数数据
    5. 使用正则提取所有代币符号
"""
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


"""解析评论部分，提取评论作者、时间和内容
参数：
    lines: list[str] - 文本行列表
    start_idx: int - 评论开始的位置索引
返回：
    list[dict[str, str]]: 评论数据列表，每个评论包含：
        comment_author: 评论作者名
        comment_time: 评论时间
        comment_text: 评论内容
评论结构：
    作者名
    ·
    时间文本
    评论内容（可能多行）
"""
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


"""解析单个txt文件，提取帖子信息和评论数据
参数：
    path: Path - txt文件的路径
返回：
    dict[str, Any]: 包含帖子所有信息的数据字典，包含：
        source_file: 源文件名
        post_id: 帖子ID（从文件名提取）
        post_author: 帖子作者名
        post_author_username: 作者用户名
        post_time: 发布时间
        post_content: 帖子内容
        disclaimer: 免责声明（如果有）
        trade_pair: 交易对
        related_symbols: 相关代币符号
        comment_count_hint: 评论数提示
        like_count_hint: 点赞数提示
        view_count_hint: 查看数提示
        reply_count_hint: 回复数提示
        quote_count_hint: 引用数提示
        has_folded_comments: 是否有折叠评论
        comments: 评论列表（每个评论包含作者、时间、内容）
处理流程：
    1. 读取并清理文本
    2. 查找帖子头部
    3. 收集帖子正文
    4. 分离内容和元数据
    5. 解析评论
"""
def parse_txt_file(path: Path) -> dict[str, Any]:
    lines = [clean_line(line) for line in path.read_text(encoding="utf-8").splitlines()]
    lines = [line for line in lines if line]

    # 核心解析流程：依次提取帖子头部、正文、元数据和评论
    try:
        header_idx, author, author_username = find_post_header(lines)
    except ValueError as exc:
        preview = "\n".join(lines[:80])
        raise ValueError(f"{exc}\n文件: {path}\n预览:\n{preview}") from exc
    post_time = lines[header_idx + 2]  # 时间信息在作者信息后两行
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


"""将数据写入CSV文件
参数：
    path: Path - CSV文件输出路径
    rows: list[dict[str, Any]] - 要写入的数据行列表（每行是一个字典）
    fieldnames: list[str] - CSV字段名列表
返回：
    None
编码：
    使用utf-8-sig编码（带BOM的UTF8），确保Excel兼容
"""
def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


"""主函数：批量解析txt文件并输出结果
功能：
    1. 解析命令行参数
    2. 读取输入目录中的所有txt文件
    3. 批量解析每个txt文件
    4. 输出结构化数据到CSV和JSON文件
参数：
    --input: 输入文件或目录（默认：update_news/binance_square_page_dump）
    --output-dir: 输出目录（默认：update_news/parsed_from_txt）
输出文件：
    binance_square_posts_from_txt.json: 帖子数据的JSON格式
    binance_square_posts_from_txt.csv: 帖子数据的CSV格式
    binance_square_comments_from_txt.csv: 评论数据的CSV格式
"""
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

    # 批量处理所有txt文件
    for txt_path in txt_paths:
        # 优先使用 "_after_login" 版本的文件（包含登录后的完整内容）
        if txt_path.stem.endswith("_after_login"):
            target_path = txt_path
        else:
            after_login = txt_path.with_name(f"{txt_path.stem}_after_login{txt_path.suffix}")
            target_path = after_login if after_login.exists() else txt_path

        # 解析单个文件
        parsed = parse_txt_file(target_path)
        
        # 写入帖子数据
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

        # 写入评论数据，每个评论生成唯一ID
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
