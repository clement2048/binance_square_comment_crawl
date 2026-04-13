#!/usr/bin/env python3
"""
解析Binance Square HTML文件中的__APP_DATA JSON数据
"""

import re
import json
from pathlib import Path
from typing import Any


def extract_app_data(html_file: Path) -> dict[str, Any]:
    """从HTML文件中提取__APP_DATA JSON数据"""
    content = html_file.read_text(encoding="utf-8")
    
    # 使用正则表达式提取__APP_DATA中的JSON
    pattern = r'<script id="__APP_DATA" type="application/json"\s+nonce="">(.*?)</script>'
    match = re.search(pattern, content, re.DOTALL)
    
    if not match:
        raise ValueError(f"未找到__APP_DATA数据: {html_file}")
    
    json_str = match.group(1)
    
    # 尝试解析JSON
    try:
        data = json.loads(json_str)
        return data
    except json.JSONDecodeError as e:
        print(f"JSON解析错误: {e}")
        # 尝试清理JSON字符串
        json_str_clean = json_str.strip()
        try:
            data = json.loads(json_str_clean)
            return data
        except json.JSONDecodeError as e2:
            # 输出JSON字符串的前500字符以便调试
            print(f"JSON前500字符: {json_str_clean[:500]}")
            raise RuntimeError(f"无法解析JSON数据: {e2}")


def analyze_app_data(data: dict[str, Any]) -> None:
    """分析__APP_DATA数据结构"""
    print("=" * 80)
    print("APP_DATA键:", list(data.keys()))
    print("=" * 80)
    
    # 递归探索数据结构
    def explore(obj: Any, path: str = "", depth: int = 0):
        if depth > 3:  # 限制递归深度
            return
        
        if isinstance(obj, dict):
            for key, value in obj.items():
                new_path = f"{path}.{key}" if path else key
                if isinstance(value, (dict, list)):
                    print(f"{'  ' * depth}{key}: {type(value).__name__}")
                    explore(value, new_path, depth + 1)
                else:
                    if key in ["title", "authorName", "content", "createTime", "post_id", "id"]:
                        print(f"{'  ' * depth}{key}: {value}")
        elif isinstance(obj, list):
            if obj:
                print(f"{'  ' * depth}list[{len(obj)}] items:")
                for i, item in enumerate(obj[:3]):  # 只看前3个
                    explore(item, f"{path}[{i}]", depth + 1)
    
    explore(data)


def find_posts_in_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    """在APP_DATA中查找帖子数据"""
    posts = []
    
    def search_for_posts(obj: Any, path: str = ""):
        if isinstance(obj, dict):
            # 检查是否包含帖子相关字段
            post_keys = {"id", "post_id", "authorName", "title", "content", "createTime"}
            if post_keys & set(obj.keys()):
                # 检查是否是有效的帖子数据
                if obj.get("id") and obj.get("authorName"):
                    print(f"找到帖子数据: id={obj.get('id')}, author={obj.get('authorName')}, path={path}")
                    posts.append(obj)
            
            # 继续递归搜索
            for key, value in obj.items():
                search_for_posts(value, f"{path}.{key}")
        
        elif isinstance(obj, list):
            for item in obj:
                search_for_posts(item, f"{path}[]")
    
    search_for_posts(data)
    return posts


def find_comments_in_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    """在APP_DATA中查找评论数据"""
    comments = []
    
    def search_for_comments(obj: Any, path: str = ""):
        if isinstance(obj, dict):
            # 检查是否包含评论相关字段
            comment_keys = {"commentId", "commentContent", "commentAuthor", "replyCount"}
            if comment_keys & set(obj.keys()):
                if obj.get("commentContent"):
                    print(f"找到评论数据: content={obj.get('commentContent')[:50]}..., path={path}")
                    comments.append(obj)
            
            # 继续递归搜索
            for key, value in obj.items():
                search_for_comments(value, f"{path}.{key}")
        
        elif isinstance(obj, list):
            for item in obj:
                search_for_comments(item, f"{path}[]")
    
    search_for_comments(data)
    return comments


def main():
    # 测试解析
    html_file = Path("update_news/binance_square_page_dump/311344932991073.html")
    
    if not html_file.exists():
        print(f"文件不存在: {html_file}")
        return
    
    print(f"解析文件: {html_file}")
    
    try:
        app_data = extract_app_data(html_file)
        print("成功提取APP_DATA数据")
        
        # 分析数据结构
        analyze_app_data(app_data)
        
        # 查找帖子数据
        posts = find_posts_in_data(app_data)
        print(f"\n找到 {len(posts)} 个帖子")
        
        # 查找评论数据
        comments = find_comments_in_data(app_data)
        print(f"\n找到 {len(comments)} 个评论")
        
        # 如果需要，可以将数据保存为JSON以便进一步分析
        output_file = Path("app_data_analysis.json")
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(app_data, f, ensure_ascii=False, indent=2)
        print(f"\n完整的APP_DATA数据已保存到: {output_file}")
        
    except Exception as e:
        print(f"解析出错: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()