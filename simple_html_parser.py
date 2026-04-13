#!/usr/bin/env python3
"""
简单的HTML解析器 - 专门用于解析Binance Square HTML
"""

import json
import re
import html
from pathlib import Path


def extract_app_data_json(html_content: str) -> dict:
    """提取APP_DATA中的JSON数据"""
    # 方法1：使用正则表达式提取完整的JSON
    pattern = r'<script[^>]*id="__APP_DATA"[^>]*type="application/json"[^>]*>(.*?)</script>'
    match = re.search(pattern, html_content, re.DOTALL | re.IGNORECASE)
    
    if match:
        json_str = match.group(1).strip()
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            # 尝试清理
            json_str = re.split(r'</script>', json_str)[0].strip()
            return json.loads(json_str)
    
    # 方法2：查找__APP_DATA开始位置
    app_data_start = html_content.find('__APP_DATA')
    if app_data_start != -1:
        # 找到JSON开始位置
        json_start = html_content.find('{', app_data_start)
        if json_start != -1:
            # 找到匹配的结束括号
            brace_count = 0
            json_end = -1
            for i in range(json_start, len(html_content)):
                if html_content[i] == '{':
                    brace_count += 1
                elif html_content[i] == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        json_end = i + 1
                        break
            
            if json_end != -1:
                json_str = html_content[json_start:json_end]
                try:
                    return json.loads(json_str)
                except json.JSONDecodeError:
                    pass
    
    raise ValueError("无法提取APP_DATA JSON")


def extract_post_details_from_html(html_content: str) -> dict:
    """从HTML中提取帖子详情（使用meta标签）"""
    details = {}
    
    # 提取meta标签信息
    meta_patterns = {
        "title": r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']*)["\'][^>]*>',
        "description": r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\']([^"\']*)["\'][^>]*>',
        "url": r'<meta[^>]*property=["\']og:url["\'][^>]*content=["\']([^"\']*)["\'][^>]*>',
        "author": r'<meta[^>]*name=["\']author["\'][^>]*content=["\']([^"\']*)["\'][^>]*>',
    }
    
    for key, pattern in meta_patterns.items():
        match = re.search(pattern, html_content, re.IGNORECASE)
        if match:
            details[key] = html.unescape(match.group(1)).strip()
    
    # 尝试从title标签提取
    if "title" not in details:
        title_match = re.search(r'<title[^>]*>([^<]*)</title>', html_content, re.IGNORECASE)
        if title_match:
            details["title"] = html.unescape(title_match.group(1)).split("|")[0].strip()
    
    return details


def extract_comments_from_html(html_content: str) -> list:
    """从HTML中提取评论数据（如果可能）"""
    comments = []
    
    # 方法1：搜索评论相关的div
    comment_divs = re.findall(r'<div[^>]*class=[^>]*comment[^>]*>.*?</div>', html_content, re.DOTALL | re.IGNORECASE)
    
    for i, div in enumerate(comment_divs[:20]):  # 限制数量
        # 提取文本内容
        text = re.sub(r'<[^>]+>', ' ', div).strip()
        text = re.sub(r'\s+', ' ', text)
        
        if len(text) > 10 and len(text) < 500:
            # 尝试提取作者
            author = ""
            author_match = re.search(r'<[^>]*class=[^>]*author[^>]*>([^<]+)</', div, re.IGNORECASE)
            if author_match:
                author = author_match.group(1).strip()
            
            # 创建评论记录
            comments.append({
                "comment_id": f"dom_{i}",
                "author": author,
                "text": text,
                "source": "dom"
            })
    
    return comments


def parse_html_file(html_file: Path) -> dict:
    """解析HTML文件"""
    print(f"解析文件: {html_file}")
    
    try:
        # 读取文件
        with open(html_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 提取APP_DATA
        app_data = extract_app_data_json(content)
        
        # 提取HTML meta信息
        html_details = extract_post_details_from_html(content)
        
        # 提取评论
        comments = extract_comments_from_html(content)
        
        # 从文件名获取帖子ID
        post_id = html_file.stem
        
        # 构建结果
        result = {
            "source_file": str(html_file),
            "post_id": post_id,
            "post_title": html_details.get("title", ""),
            "post_content": html_details.get("description", ""),
            "post_url": html_details.get("url", ""),
            "post_author": html_details.get("author", ""),
            "post_text": html_details.get("description", ""),  # 与post_content相同
            "post_time": "",  # 需要从APP_DATA提取
            "comments_count": len(comments),
            "comments": comments,
            "app_data_keys": list(app_data.keys()) if isinstance(app_data, dict) else []
        }
        
        # 尝试从APP_DATA提取更多信息
        if isinstance(app_data, dict):
            # 搜索帖子时间
            def find_time(data: any, path: str = "") -> str:
                if isinstance(data, dict):
                    for key, value in data.items():
                        if key.lower() in ["createtime", "publishtime", "posttime", "date", "time"]:
                            if isinstance(value, (str, int)):
                                return str(value)
                        if isinstance(value, (dict, list)):
                            result = find_time(value, f"{path}.{key}")
                            if result:
                                return result
                elif isinstance(data, list):
                    for i, item in enumerate(data[:10]):
                        result = find_time(item, f"{path}[{i}]")
                        if result:
                            return result
                return ""
            
            post_time = find_time(app_data)
            if post_time:
                result["post_time"] = post_time
            
            # 搜索作者名
            def find_author(data: any) -> str:
                if isinstance(data, dict):
                    for key, value in data.items():
                        if key.lower() in ["authorname", "authornickname", "author", "nickname", "username"]:
                            if isinstance(value, str):
                                return value
                        if isinstance(value, (dict, list)):
                            result = find_author(value)
                            if result:
                                return result
                elif isinstance(data, list):
                    for item in data[:10]:
                        result = find_author(item)
                        if result:
                            return result
                return ""
            
            author = find_author(app_data)
            if author and not result["post_author"]:
                result["post_author"] = author
        
        return result
        
    except Exception as e:
        print(f"解析出错: {e}")
        import traceback
        traceback.print_exc()
        
        # 返回简单结构
        return {
            "source_file": str(html_file),
            "post_id": html_file.stem,
            "post_title": "",
            "post_content": "",
            "post_time": "",
            "post_author": "",
            "post_text": "",
            "comments": [],
            "error": str(e)
        }


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="解析Binance Square HTML文件")
    parser.add_argument(
        "--input",
        default="update_news/binance_square_page_dump/311344932991073.html",
        help="输入HTML文件"
    )
    parser.add_argument(
        "--output",
        default="parsed_output.json",
        help="输出JSON文件"
    )
    
    args = parser.parse_args()
    html_file = Path(args.input)
    
    if not html_file.exists():
        print(f"文件不存在: {html_file}")
        return
    
    result = parse_html_file(html_file)
    
    # 写入输出文件
    output_file = Path(args.output)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"结果已写入: {output_file}")
    
    # 显示预览
    print("\n解析结果预览:")
    print(f"  帖子ID: {result.get('post_id')}")
    print(f"  标题: {result.get('post_title', '')[:50]}")
    print(f"  作者: {result.get('post_author', '')}")
    print(f"  发布时间: {result.get('post_time', '')}")
    print(f"  评论数: {len(result.get('comments', []))}")
    
    for i, comment in enumerate(result.get('comments', [])[:3]):
        print(f"  评论{i+1}: {comment.get('author', '匿名')}: {comment.get('text', '')[:50]}...")


if __name__ == "__main__":
    main()