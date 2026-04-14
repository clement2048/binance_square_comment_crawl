#!/usr/bin/env python3
"""调试评论提取的工具"""

import re
import html

def debug_card_hd_extraction():
    # 读取HTML文件的部分内容用于调试
    with open("update_news/binance_square_page_dump/311344932991073.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    
    # 查找第一个评论内容块
    comment_content_pattern = r'<div[^>]*class="[^"]*feed-content-text[^"]*"[^>]*datatype="squareCard"[^>]*data="311414100519362"[^>]*>.*?<div[^>]*class="card__description rich-text"[^>]*style="[^"]*"[^>]*>(.*?)</div>'
    
    content_match = re.search(comment_content_pattern, html_content, re.DOTALL)
    
    if content_match:
        print("找到了评论内容块")
        content_start = content_match.start()
        print(f"评论内容块开始位置: {content_start}")
        
        # 向前搜索更多字符查找card__hd
        search_start = max(0, content_start - 5000)
        search_area = html_content[search_start:content_start]
        
        print(f"\n搜索区域长度: {len(search_area)} 字符")
        
        # 1. 直接查找card__hd模式
        card_hd_pattern = r'(<div[^>]*card__hd[^>]*>[\s\S]*?)<div[^>]*card__bd'
        card_hd_match = re.search(card_hd_pattern, search_area, re.DOTALL)
        
        if card_hd_match:
            print("找到card__hd块")
            card_hd_content = card_hd_match.group(1)
            print(f"card__hd内容长度: {len(card_hd_content)}")
            
            # 在card_hd_content中查找作者
            author_patterns = [
                r'<div[^>]*class="nick-username"[^>]*>[\s\S]*?<a[^>]*class="nick"[^>]*>([^<]+)</a>',
                r'class="nick"[^>]*>([^<]+)</a>',
            ]
            
            for i, pattern in enumerate(author_patterns):
                author_match = re.search(pattern, card_hd_content, re.DOTALL)
                if author_match:
                    author = html.unescape(author_match.group(1)).strip()
                    print(f"模式{i+1}找到作者: {author}")
                    break
                else:
                    print(f"模式{i+1}未找到作者")
                    
            # 在card_hd_content中查找时间
            time_pattern = r'<div[^>]*class="create-time"[^>]*>([^<]+)</div>'
            time_match = re.search(time_pattern, card_hd_content, re.DOTALL)
            if time_match:
                time_text = html.unescape(time_match.group(1)).strip()
                print(f"找到时间: {time_text}")
            else:
                print("未找到时间")
                
            # 查看card_hd_content的前200字符
            print(f"\ncard__hd内容前200字符:\n{card_hd_content[:200]}")
            if len(card_hd_content) > 200:
                print(f"... (还有 {len(card_hd_content)-200} 字符)")
        else:
            print("未找到card__hd块")
            # 看看搜索区域的内容
            print(f"\n搜索区域最后500字符:\n{search_area[-500:]}")
            
            # 尝试直接查找作者
            author_direct_pattern = r'<div class="nick-username"[^>]*>.*?<a [^>]*class="nick"[^>]*>([^<]+)</a>'
            author_direct_match = re.search(author_direct_pattern, search_area, re.DOTALL)
            if author_direct_match:
                author = html.unescape(author_direct_match.group(1)).strip()
                print(f"直接搜索找到作者: {author}")
                
            # 查找card__hd的多种可能模式
            print("\n尝试不同模式查找card__hd:")
            patterns = [
                r'<div[^>]*card__hd[^>]*>',
                r'class="[^"]*card__hd[^"]*"',
                r'card__hd',
            ]
            
            for pattern in patterns:
                matches = list(re.finditer(pattern, search_area, re.DOTALL))
                print(f"模式 '{pattern}' 找到 {len(matches)} 个匹配")
                if matches:
                    last_match = matches[-1]
                    print(f"  最后一个匹配位置: {last_match.start()}, 内容附近: {search_area[max(0, last_match.start()-50):last_match.end()+50]}")
    else:
        print("未找到评论内容块")

if __name__ == "__main__":
    debug_card_hd_extraction()