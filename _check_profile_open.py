from playwright.sync_api import sync_playwright
url='https://www.binance.com/zh-CN/square/post/311338831687490'
with sync_playwright() as p:
    ctx=p.chromium.launch_persistent_context(
        user_data_dir=r'e:\\code\\sentiment\\tmp_chrome_profile',
        channel='chrome',
        headless=True,
        args=['--profile-directory=Default','--no-proxy-server','--proxy-bypass-list=*'],
        viewport={'width':1366,'height':1600},
    )
    pg=ctx.new_page()
    pg.goto(url,wait_until='domcontentloaded',timeout=90000)
    print('ok_title',pg.title())
    ctx.close()
