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


def extract_from_meta_tags(html_content: str) -> Dict[str, str]:
    """从HTML meta标签提取信息"""
    meta_info = {}
    
    # 提取OG标签
    og_patterns = {
        "title": r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']*)["\'][^>]*>',
        "description": r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\']([^"\']*)["\'][^>]*>',
        "url": r'<meta[^>]*property=["\']og:url["\'][^>]*content=["\']([^"\']*)["\'][^>]*>',
    }
    
    for key, pattern in og_patterns.items():
        match = re.search(pattern, html_content, re.IGNORECASE)
        if match:
            meta_info[key] = html.unescape(match.group(1)).strip()
    
    # 从title标签提取标题
    if "title" not in meta_info:
        title_match = re.search(r'<title[^>]*>([^<]*)</title>', html_content, re.IGNORECASE)
        if title_match:
            meta_info["title"] = html.unescape(title_match.group(1)).split("|")[0].strip()
    
    # 尝试从页面内容提取作者和时间
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


def extract_comments_from_dom(html_content: str) -> List[Dict[str, str]]:
    """从DOM中提取评论数据（备用方法）"""
    comments = []
    
    # 搜索评论相关文本
    # 常见评论类名和数据属性
    comment_selectors = [
        r'<div[^>]*class=[^>]*comment-item[^>]*>.*?</div>',
        r'<div[^>]*class=[^>]*comment[^>]*>.*?</div>',
        r'<div[^>]*data-testid=[^>]*comment[^>]*>.*?</div>',
        r'<div[^>]*role=["\']comment["\'][^>]*>.*?</div>',
    ]
    
    for pattern in comment_selectors:
        matches = re.findall(pattern, html_content, re.DOTALL | re.IGNORECASE)
        if matches:
            for i, match in enumerate(matches[:50]):  # 限制数量
                # 提取文本
                text = re.sub(r'<[^>]+>', ' ', match)
                text = re.sub(r'\s+', ' ', text).strip()
                
                if 10 < len(text) < 500:  # 合理长度
                    # 尝试提取作者
                    author = ""
                    author_patterns = [
                        r'<[^>]*class=[^>]*author[^>]*>([^<]+)</',
                        r'<[^>]*class=[^>]*nickname[^>]*>([^<]+)</',
                        r'<[^>]*class=[^>]*username[^>]*>([^<]+)</',
                    ]
                    
                    for author_pattern in author_patterns:
                        author_match = re.search(author_pattern, match, re.IGNORECASE)
                        if author_match:
                            author = html.unescape(author_match.group(1)).strip()
                            break
                    
                    comments.append({
                        "comment_id": f"dom_{i}",
                        "author": author,
                        "text": text,
                        "time": "",
                    })
    
    return comments


def timestamp_to_readable(timestamp: Any) -> str:
    """转换时间戳为可读格式"""
    if not timestamp:
        return ""
    
    try:
        ts_str = str(timestamp)
        if not ts_str.isdigit() or ts_str == "0":
            return ""
        
        ts_int = int(ts_str)
        if ts_int > 10**12:  # 毫秒级
            dt = datetime.fromtimestamp(ts_int / 1000)
        else:  # 秒级
            dt = datetime.fromtimestamp(ts_int)
        
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(timestamp)


def build_comment_tree(flat_comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """构建评论树形结构"""
    comment_map = {}
    root_comments = []
    
    # 第一次遍历：建立ID映射
    for comment in flat_comments:
        comment_id = comment.get("comment_id")
        if comment_id:
            comment["replies"] = []
            comment_map[comment_id] = comment
    
    # 第二次遍历：建立父子关系
    for comment in flat_comments:
        parent_id = comment.get("parent_comment_id")
        if parent_id and parent_id in comment_map:
            # 这是回复
            comment_map[parent_id]["replies"].append(comment)
        else:
            # 这是根评论
            root_comments.append(comment)
    
    return root_comments


def format_comment_for_output(comment: Dict[str, Any], idx: int) -> Dict[str, Any]:
    """格式化评论为输出格式"""
    return {
        "comment_id": comment.get("comment_id") or f"c{idx}",
        "author": comment.get("author", ""),
        "text": comment.get("text", ""),
        "replies": [format_comment_for_output(reply, i) for i, reply in enumerate(comment.get("replies", []))]
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
        if app_data:
            comments = find_comments_in_app_data(app_data)
        
        # 如果APP_DATA中没有评论，尝试从DOM提取
        if not comments:
            comments = extract_comments_from_dom(content)
        
        # 构建评论树
        comment_tree = build_comment_tree(comments)
        
        # 构建最终结果
        result = {
            "source_file": str(html_file),
            "post_id": post_id,
            "post_author": post_data.get("author") or meta_info.get("author", ""),
            "post_content": post_data.get("content") or meta_info.get("description", ""),
            "post_time": timestamp_to_readable(post_data.get("createTime")),
            "post_text": post_data.get("content") or meta_info.get("description", ""),
            "comments": [format_comment_for_output(comment, i) for i, comment in enumerate(comment_tree)]
        }
        
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
            "post_text": "",
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