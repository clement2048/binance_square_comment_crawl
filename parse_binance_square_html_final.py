#!/usr/bin/env python3
"""
Binance Square HTML解析器 - 最终版本

该解析器：
1. 从HTML提取帖子基本信息（标题、作者、时间、内容）
2. 使用两种方法提取评论数据：
   - 从APP_DATA JSON中提取（如果可用）
   - 从DOM中搜索评论文本（备用方法）
3. 输出符合要求的JSON格式
"""

import json
import re
import html
import argparse
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional
import sys


def extract_app_data(html_content: str) -> Optional[Dict[str, Any]]:
    """提取__APP_DATA中的JSON数据"""
    # 查找APP_DATA脚本标签
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
                # 检查是否是DiscussionForumPosting类型
                if data.get("@type") == "DiscussionForumPosting":
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
        # 优先使用完整文本，如果JSON-LD中没有完整文本，则尝试合并其他来源
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
    
    # 如果JSON-LD中没有作者，尝试从页面内容提取作者和时间
    if "author" not in meta_info:
        author_patterns = [
            r'<meta[^>]*name=["\']author["\'][^>]*content=["\']([^"\']*)["\'][^>]*>',
            r'<div[^>]*class=[^>]*author[^>]*>([^<]+)</div>',
            r'<span[^>]*class=[^>]*author[^>]*>([^<]+)</span>',
        ]
        
        for pattern in author_patterns:
            match = re.search(pattern, html_content, re.IGNORECASE)
            if match:
                meta_info["author"] = html.unescape(match.group(1)).strip()
                break
    
    # 生成完整的帖子内容
    # 优先使用JSON-LD的完整文本，否则使用headline + description的组合
    if "full_content" in meta_info:
        meta_info["description"] = meta_info["full_content"]
    elif "title" in meta_info and "description" in meta_info:
        # 合并标题和描述
        meta_info["description"] = f"{meta_info['title']}\n\n{meta_info['description']}"
    
    return meta_info


def find_post_data_in_app_data(app_data: Dict[str, Any], post_id: str) -> Dict[str, Any]:
    """在APP_DATA中查找帖子数据"""
    result = {}
    
    def search_recursive(data: Any, path: str = "") -> bool:
        if isinstance(data, dict):
            # 检查是否包含帖子ID
            data_id = str(data.get("id") or data.get("postId") or data.get("post_id") or "")
            if data_id == post_id:
                # 提取关键字段
                result.update({
                    "title": data.get("title") or data.get("subTitle") or "",
                    "content": data.get("content") or data.get("plainText") or data.get("text") or "",
                    "author": data.get("authorName") or data.get("nickName") or data.get("author") or "",
                    "createTime": data.get("createTime") or data.get("publishTime") or "",
                    "authorUsername": data.get("authorUserName") or data.get("userName") or "",
                    "likeCount": data.get("likeCount") or 0,
                    "commentCount": data.get("commentCount") or 0,
                    "viewCount": data.get("viewCount") or 0,
                })
                return True
            
            # 递归搜索
            for key, value in data.items():
                if search_recursive(value, f"{path}.{key}"):
                    return True
        
        elif isinstance(data, list):
            for i, item in enumerate(data):
                if search_recursive(item, f"{path}[{i}]"):
                    return True
        
        return False
    
    search_recursive(app_data)
    return result


def find_comments_in_app_data(app_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """在APP_DATA中查找评论数据"""
    comments = []
    
    def search_recursive(data: Any, parent_id: Optional[str] = None, depth: int = 0) -> None:
        if depth > 10:  # 限制深度防止无限递归
            return
        
        if isinstance(data, dict):
            # 检查是否是评论对象
            is_comment = False
            comment_data = {}
            
            # 检查评论字段
            if "commentContent" in data or "commentText" in data:
                is_comment = True
                comment_data = {
                    "comment_id": data.get("commentId") or data.get("id") or "",
                    "author": data.get("authorName") or data.get("nickName") or "",
                    "text": data.get("commentContent") or data.get("commentText") or "",
                    "time": data.get("createTime") or data.get("commentTime") or "",
                    "like_count": data.get("likeCount") or data.get("upCount") or 0,
                    "reply_count": data.get("replyCount") or 0,
                    "parent_comment_id": parent_id
                }
            
            # 检查内容字段（也可能是评论）
            elif "content" in data and isinstance(data["content"], str) and len(data["content"]) < 500:
                # 可能是嵌套的评论内容
                if data.get("authorName") or data.get("nickName"):
                    is_comment = True
                    comment_data = {
                        "comment_id": data.get("id") or "",
                        "author": data.get("authorName") or data.get("nickName") or "",
                        "text": data.get("content") or "",
                        "time": data.get("createTime") or "",
                        "like_count": data.get("likeCount") or 0,
                        "reply_count": data.get("replyCount") or 0,
                        "parent_comment_id": parent_id
                    }
            
            if is_comment and comment_data.get("text"):
                # 添加评论
                comments.append(comment_data)
                
                # 如果这个评论有回复，搜索回复
                reply_key = "replies" if "replies" in data else "replyList"
                if reply_key in data:
                    search_recursive(data[reply_key], comment_data["comment_id"], depth + 1)
            
            # 搜索评论列表
            for key in ["comments", "commentList", "commentInfo"]:
                if key in data:
                    search_recursive(data[key], parent_id, depth + 1)
            
            # 递归搜索其他字段
            for key, value in data.items():
                if key not in ["comments", "commentList", "commentInfo", "replies", "replyList"]:
                    search_recursive(value, parent_id, depth + 1)
        
        elif isinstance(data, list):
            for item in data:
                search_recursive(item, parent_id, depth + 1)
    
    search_recursive(app_data)
    return comments


def extract_comments_from_dom(html_content: str) -> List[Dict[str, Any]]:
    """从DOM中提取评论数据（按评论卡片顺序，确定性提取）"""
    comments: List[Dict[str, Any]] = []

    card_pattern = re.compile(
        r'<div[^>]*data-id="([^"]+)"[^>]*class="([^"]*FeedBuzzBaseViewRoot[^"]*)"[^>]*>',
        re.DOTALL,
    )
    card_matches = list(card_pattern.finditer(html_content))
    if not card_matches:
        print("  DOM中未找到评论卡片")
        return comments

    print(f"  DOM中找到 {len(card_matches)} 个评论卡片")

    for i, match in enumerate(card_matches):
        original_comment_id = match.group(1).strip()
        class_attr = match.group(2)

        start = match.end()
        end = card_matches[i + 1].start() if i + 1 < len(card_matches) else len(html_content)
        segment = html_content[start:end]

        author = ""
        time_text = ""
        text = ""

        author_match = re.search(
            r'<div[^>]*class="nick-username"[^>]*>[\s\S]*?<a[^>]*class="nick[^"]*"[^>]*>([^<]+)</a>',
            segment,
            re.DOTALL,
        )
        if author_match:
            author = html.unescape(author_match.group(1)).strip()

        time_match = re.search(
            r'<div[^>]*class="create-time"[^>]*>([^<]+)</div>',
            segment,
            re.DOTALL,
        )
        if time_match:
            time_text = html.unescape(time_match.group(1)).strip()

        content_match = re.search(
            rf'<div[^>]*class="[^"]*feed-content-text[^"]*"[^>]*data="{re.escape(original_comment_id)}"[^>]*>[\s\S]*?<div[^>]*class="card__description rich-text"[^>]*>([\s\S]*?)</div>',
            segment,
            re.DOTALL,
        )
        if not content_match:
            content_match = re.search(
                r'<div[^>]*class="card__description rich-text"[^>]*>([\s\S]*?)</div>',
                segment,
                re.DOTALL,
            )

        if content_match:
            text = html.unescape(content_match.group(1))
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()

        if not text:
            continue

        comment = {
            "comment_id": original_comment_id,
            "original_comment_id": original_comment_id,
            "author": author,
            "text": text,
            "time": time_text,
            "parent_comment_id": None,
            "is_thread_card": "in-thread-card" in class_attr,
            "is_thread_first": "in-thread-card-first" in class_attr,
            "is_thread_last": "in-thread-card-last" in class_attr,
            "replies": [],
        }
        comments.append(comment)

    print(f"  DOM提取到 {len(comments)} 条有效评论")
    return comments


def parse_datetime(datetime_str: Any) -> str:
    """解析时间字符串为可读格式"""
    if not datetime_str:
        return ""
    
    try:
        dt_str = str(datetime_str).strip()
        
        # 如果是时间戳（数字）
        if dt_str.isdigit():
            ts_int = int(dt_str)
            if ts_int == 0:
                return ""
            elif ts_int > 10**12:  # 毫秒级
                dt = datetime.fromtimestamp(ts_int / 1000)
            else:  # 秒级
                dt = datetime.fromtimestamp(ts_int)
        else:
            # 尝试解析ISO 8601格式（如：2026-04-11T10:25:46.000Z）
            # 移除毫秒和时区信息
            cleaned_dt = re.sub(r'\.\d+', '', dt_str)  # 移除毫秒
            cleaned_dt = cleaned_dt.replace('Z', '')  # 移除Z时区标记
            dt = datetime.fromisoformat(cleaned_dt)
        
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        # 如果无法解析，返回原始字符串
        return str(datetime_str)


def build_comment_tree(flat_comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """构建评论树形结构（优先使用线程标记；无标记时按parent_comment_id）"""
    if not flat_comments:
        return []

    for comment in flat_comments:
        comment["replies"] = []

    has_thread_marker = any("is_thread_card" in c for c in flat_comments)
    if has_thread_marker:
        root_comments: List[Dict[str, Any]] = []
        current_thread_root: Optional[Dict[str, Any]] = None

        for comment in flat_comments:
            is_first = bool(comment.get("is_thread_first"))
            is_last = bool(comment.get("is_thread_last"))
            is_thread = bool(comment.get("is_thread_card"))

            if is_first:
                comment["parent_comment_id"] = None
                root_comments.append(comment)
                current_thread_root = comment
                if is_last:
                    current_thread_root = None
                continue

            if is_thread and current_thread_root is not None:
                comment["parent_comment_id"] = current_thread_root.get("original_comment_id") or current_thread_root.get("comment_id")
                current_thread_root["replies"].append(comment)
                if is_last:
                    current_thread_root = None
                continue

            comment["parent_comment_id"] = None
            root_comments.append(comment)
            if not is_thread:
                current_thread_root = None

        return root_comments

    # APP_DATA等来源没有线程标记时，使用显式parent_comment_id构树
    comment_map: Dict[str, Dict[str, Any]] = {}
    root_comments = []

    for comment in flat_comments:
        node_id = str(comment.get("comment_id") or comment.get("original_comment_id") or "")
        if node_id:
            comment_map[node_id] = comment

    for comment in flat_comments:
        parent_id = str(comment.get("parent_comment_id") or "")
        if parent_id and parent_id in comment_map:
            comment_map[parent_id]["replies"].append(comment)
        else:
            root_comments.append(comment)

    return root_comments


def format_comment_for_output(comment: Dict[str, Any], id_counter: Dict[str, int]) -> Dict[str, Any]:
    """格式化评论为输出格式"""
    comment_id = f"c{id_counter['value']}"
    id_counter["value"] += 1
    author = comment.get("author", "")
    text = comment.get("text", "")
    post_time = comment.get("time", "")
    original_comment_id = str(comment.get("original_comment_id") or comment.get("comment_id") or "")
    
    return {
        "comment_id": comment_id,
        "original_comment_id": original_comment_id,
        "author": author,
        "text": text,
        "post_time": post_time,
        "replies": [format_comment_for_output(reply, id_counter) for reply in comment.get("replies", [])]
    }


def parse_html_file(html_file: Path) -> Dict[str, Any]:
    """解析单个HTML文件"""
    print(f"解析文件: {html_file.name}")
    
    try:
        with open(html_file, "r", encoding="utf-8") as f:
            content = f.read()
        
        # 获取帖子ID（从文件名）
        post_id = html_file.stem
        
        # 从meta标签提取信息
        meta_info = extract_from_meta_tags(content)
        
        # 提取APP_DATA
        app_data = extract_app_data(content)
        
        # 从APP_DATA获取帖子数据
        post_data = {}
        if app_data:
            post_data = find_post_data_in_app_data(app_data, post_id)
        
        # 获取评论数据
        comments = []
        # 优先从DOM提取（更准确，有作者和时间）
        comments = extract_comments_from_dom(content)
        
        # 如果DOM中没提取到评论，再尝试从APP_DATA提取
        if not comments and app_data:
            print(f"  从APP_DATA提取评论...")
            comments = find_comments_in_app_data(app_data)
        
        print(f"  总共提取到 {len(comments)} 条评论")
        
        # 构建评论树
        comment_tree = build_comment_tree(comments)
        
        # 构建最终结果
        # 优先使用JSON-LD中的数据
        post_author = (
            meta_info.get("author") or  # JSON-LD中的作者
            post_data.get("author") or  # APP_DATA中的作者
            ""
        )
        
        post_content = (
            meta_info.get("description") or  # JSON-LD中的完整文本
            post_data.get("content") or      # APP_DATA中的内容
            ""
        )
        
        post_time = (
            parse_datetime(meta_info.get("published_date")) or  # JSON-LD中的发布时间
            parse_datetime(post_data.get("createTime")) or      # APP_DATA中的创建时间
            ""
        )
        
        result = {
            "source_file": str(html_file),
            "post_id": post_id,
            "post_author": post_author,
            "post_content": post_content,
            "post_time": post_time,
            "comments": []
        }

        id_counter = {"value": 1}
        result["comments"] = [
            format_comment_for_output(comment, id_counter)
            for comment in comment_tree
        ]
        
        return result
        
    except Exception as e:
        print(f"  解析出错: {e}")
        import traceback
        traceback.print_exc()
        
        # 返回基本结构
        return {
            "source_file": str(html_file),
            "post_id": html_file.stem,
            "post_author": "",
            "post_content": "",
            "post_time": "",
            "comments": [],
            "error": str(e)
        }


def parse_html_directory(input_dir: Path) -> List[Dict[str, Any]]:
    """解析目录中的所有HTML文件"""
    html_files = list(input_dir.glob("*.html"))
    if not html_files:
        raise ValueError(f"在目录中未找到HTML文件: {input_dir}")
    
    results = []
    for html_file in html_files:
        result = parse_html_file(html_file)
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
    parser = argparse.ArgumentParser(
        description="Binance Square HTML解析器 - 提取帖子信息和评论数据"
    )
    
    parser.add_argument(
        "--input",
        default="update_news/binance_square_page_dump",
        help="输入HTML文件或目录，默认: update_news/binance_square_page_dump"
    )
    
    parser.add_argument(
        "--output",
        default="update_news/parsed_from_html/binance_square_html_parsed.json",
        help="输出JSON文件路径，默认: update_news/parsed_from_html/binance_square_html_parsed.json"
    )
    
    parser.add_argument(
        "--batch",
        action="store_true",
        help="批量处理目录中的所有HTML文件"
    )
    
    args = parser.parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    
    try:
        if args.batch or input_path.is_dir():
            # 批量处理目录
            print(f"批量解析目录: {input_path}")
            results = parse_html_directory(input_path)
            if results:
                write_output(output_path, results)
                print(f"成功解析 {len(results)} 个HTML文件")
            else:
                print("未解析出任何结果")
                
        elif input_path.is_file():
            # 解析单个文件
            print(f"解析单个文件: {input_path}")
            result = parse_html_file(input_path)
            if result:
                write_output(output_path, [result])
                print("单个文件解析完成")
            else:
                print("解析失败")
                
        else:
            print(f"路径不存在: {input_path}")
            sys.exit(1)
        
        print("\n解析完成!")
        
    except Exception as e:
        print(f"程序出错: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()