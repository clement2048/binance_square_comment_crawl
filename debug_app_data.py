#!/usr/bin/env python3
"""
调试APP_DATA，查找实际的数据结构
"""

import json
from pathlib import Path
from typing import Any


def load_app_data(html_file: Path) -> dict[str, Any]:
    """加载APP_DATA"""
    content = html_file.read_text(encoding="utf-8")
    
    # 提取JSON
    start_marker = '<script id="__APP_DATA" type="application/json" nonce="">'
    end_marker = '</script>'
    
    start_pos = content.find(start_marker)
    if start_pos == -1:
        raise ValueError("未找到APP_DATA")
    
    start_pos += len(start_marker)
    end_pos = content.find(end_marker, start_pos)
    
    if end_pos == -1:
        raise ValueError("未找到APP_DATA结束标记")
    
    json_str = content[start_pos:end_pos].strip()
    
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        # 尝试清理
        json_str = json_str.split("</script>")[0].strip()
        return json.loads(json_str)


def find_post_content(app_data: dict[str, Any]) -> None:
    """查找帖子内容"""
    print("搜索帖子内容...")
    
    def search_recursive(obj: Any, path: str, depth: int = 0):
        if depth > 5:
            return
        
        if isinstance(obj, dict):
            # 检查是否包含明显的帖子内容字段
            matches = 0
            
            # 检查标题
            title = obj.get("title") or obj.get("og_title") or obj.get("og:title")
            if title and ("THEY DON'T WANT YOU TO SEE THIS" in title or "THEY DON\u2019T WANT YOU TO SEE THIS" in title):
                matches += 1
                print(f"\n找到帖子标题: path={path}")
                print(f"  title: {title}")
            
            # 检查内容
            content = obj.get("content") or obj.get("description") or obj.get("text")
            if content and isinstance(content, str) and len(content) > 100:
                # 检查是否包含帖子开头的句子
                opening_sentences = [
                    "This information was never meant for retail eyes",
                    "But I'm done watching people get slaughtered",
                    "Stop trading against them"
                ]
                
                for sentence in opening_sentences:
                    if sentence in content:
                        matches += 1
                        print(f"\n找到帖子内容: path={path}")
                        print(f"  内容预览: {content[:200]}...")
                        break
            
            # 检查帖子ID
            post_id = obj.get("id") or obj.get("postId") or obj.get("post_id")
            if post_id == "311344932991073":
                matches += 1
                print(f"\n找到帖子ID: path={path}")
                print(f"  post_id: {post_id}")
            
            # 如果找到多个匹配，打印整个对象
            if matches >= 2:
                print(f"\n完整对象: {path}")
                for key, value in obj.items():
                    if key in ["title", "content", "id", "postId", "authorName", "author", "createTime"]:
                        if isinstance(value, str) and len(value) > 100:
                            print(f"  {key}: {value[:150]}...")
                        else:
                            print(f"  {key}: {value}")
            
            # 递归搜索
            for key, value in obj.items():
                search_recursive(value, f"{path}.{key}", depth + 1)
        
        elif isinstance(obj, list):
            for i, item in enumerate(obj[:20]):  # 限制数量
                search_recursive(item, f"{path}[{i}]", depth + 1)
    
    search_recursive(app_data, "$")


def find_comment_data(app_data: dict[str, Any]) -> None:
    """查找评论数据"""
    print("\n" + "="*80)
    print("搜索评论数据...")
    
    comment_count = 0
    
    def search_recursive(obj: Any, path: str, depth: int = 0):
        nonlocal comment_count
        if depth > 5:
            return
        
        if isinstance(obj, dict):
            # 检查是否是评论
            is_comment = False
            comment_info = {}
            
            # 检查评论常见字段
            if obj.get("commentId") or obj.get("commentContent") or obj.get("commentText"):
                is_comment = True
                comment_info = {
                    "commentId": obj.get("commentId", ""),
                    "author": obj.get("authorName") or obj.get("nickName") or "",
                    "content": obj.get("commentContent") or obj.get("commentText") or "",
                    "parentId": obj.get("parentCommentId")
                }
            
            # 检查嵌套的评论结构
            elif "comment" in path.lower() or "reply" in path.lower():
                # 检查常见字段
                content = obj.get("content") or obj.get("text")
                if content and isinstance(content, str):
                    is_comment = True
                    comment_info = {
                        "commentId": obj.get("id", ""),
                        "author": obj.get("authorName") or obj.get("nickName") or "",
                        "content": content,
                        "parentId": obj.get("parentId")
                    }
            
            if is_comment and comment_info["content"]:
                comment_count += 1
                print(f"\n评论 #{comment_count}: path={path}")
                for key, value in comment_info.items():
                    if key == "content" and len(value) > 100:
                        print(f"  {key}: {value[:100]}...")
                    else:
                        print(f"  {key}: {value}")
            
            # 递归搜索
            for key, value in obj.items():
                search_recursive(value, f"{path}.{key}", depth + 1)
        
        elif isinstance(obj, list):
            for i, item in enumerate(obj[:20]):  # 限制数量
                search_recursive(item, f"{path}[{i}]", depth + 1)
    
    search_recursive(app_data, "$")
    
    print(f"\n总计找到 {comment_count} 个评论")


def find_sidebar_posts(app_data: dict[str, Any]) -> None:
    """查找侧边栏的帖子列表"""
    print("\n" + "="*80)
    print("搜索侧边栏帖子列表...")
    
    # 直接查找常见的侧边栏路径
    possible_paths = [
        "pageData.redux.ui.sidebarData.articles",
        "pageData.redux.ui.otherPosts",
        "pageData.redux.ui.articles",
        "pageData.redux.feed.articles",
    ]
    
    def search_in_path(base_obj: Any, path_parts: list[str]) -> Any:
        current = base_obj
        for part in path_parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        return current
    
    for test_path in possible_paths:
        parts = test_path.split(".")
        result = search_in_path(app_data, parts)
        if result:
            print(f"\n找到路径: {test_path}")
            if isinstance(result, list):
                print(f"  包含 {len(result)} 个条目")
                for i, item in enumerate(result[:5]):  # 显示前5个
                    if isinstance(item, dict):
                        print(f"  条目 {i}:")
                        for key in ["title", "id", "authorName"]:
                            if key in item:
                                value = item[key]
                                print(f"    {key}: {value[:50] if isinstance(value, str) and len(value) > 50 else value}")
            elif isinstance(result, dict):
                print(f"  字典, 键: {list(result.keys())}")
            break


def main():
    html_file = Path("update_news/binance_square_page_dump/311344932991073.html")
    
    if not html_file.exists():
        print(f"文件不存在: {html_file}")
        return
    
    try:
        print(f"加载文件: {html_file}")
        app_data = load_app_data(html_file)
        print("APP_DATA加载成功")
        
        # 查找帖子内容
        find_post_content(app_data)
        
        # 查找评论数据
        find_comment_data(app_data)
        
        # 查找侧边栏帖子
        find_sidebar_posts(app_data)
        
        # 保存完整的APP_DATA用于进一步分析
        debug_file = Path("app_data_debug.json")
        with open(debug_file, "w", encoding="utf-8") as f:
            json.dump(app_data, f, ensure_ascii=False, indent=2)
        print(f"\n完整APP_DATA已保存到: {debug_file}")
        
    except Exception as e:
        print(f"出错: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()