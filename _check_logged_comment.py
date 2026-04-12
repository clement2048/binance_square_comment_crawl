from playwright.sync_api import sync_playwright

url = 'https://www.binance.com/zh-CN/square/post/311338831687490'
user_data_dir = r'e:\\code\\sentiment\\tmp_chrome_profile'

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        channel='chrome',
        headless=True,
        args=['--profile-directory=Default'],
        viewport={'width': 1366, 'height': 1600},
    )
    page = context.new_page()
    stats = {'comment_api_hits': 0, 'comment_nodes': 0}

    def on_response(resp):
        u = resp.url.lower()
        if 'comment' in u:
            stats['comment_api_hits'] += 1
            print('comment_api', resp.status, resp.url[:180])

    page.on('response', on_response)
    page.goto(url, wait_until='domcontentloaded', timeout=90000)
    page.wait_for_timeout(5000)

    for selector in ["button:has-text('评论')", "button:has-text('Comment')", "[data-testid*='comment']", "[class*='comment-btn']"]:
        try:
            loc = page.locator(selector)
            if loc.count() > 0:
                loc.first.click(timeout=2000)
                page.wait_for_timeout(1200)
                print('clicked', selector)
                break
        except Exception:
            pass

    for _ in range(7):
        page.mouse.wheel(0, 2600)
        page.wait_for_timeout(1200)

    comment_selectors = [
        "[data-testid*='comment-content']",
        "[class*='comment-content']",
        "[class*='CommentContent']",
        "[class*='comment-item']",
        "[class*='CommentItem']",
        "[class*='commentItem']",
    ]
    total_nodes = 0
    for sel in comment_selectors:
        try:
            c = page.locator(sel).count()
            total_nodes += c
        except Exception:
            pass

    html = page.content()
    login_hint = any(x in html for x in ['退出登录', '个人中心', '资产', 'Wallet'])
    print('title=', page.title())
    print('content_len=', len(html))
    print('comment_api_hits=', stats['comment_api_hits'])
    print('comment_nodes=', total_nodes)
    print('login_hint=', login_hint)

    context.close()
