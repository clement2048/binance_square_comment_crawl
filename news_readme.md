# Coin News Pipeline — parse_article + fetch_coin_pages

## 流程

```
crawler_coin.py  (BAPI 采集帖子索引)
       ↓
fetch_coin_pages.py  (Playwright 下载 HTML + 拦截评论 API)
       ↓
parse_article.py  (离线解析 HTML + 加载 sidecar 评论)
```

## 安装

```bash
pip install -r requirements.txt
playwright install chromium
```

## 操作命令

### 1. 采集帖子

```bash
# 先检查命中率
python crawler_coin.py --check-only --symbols BTC,ETH,SOL --trust-env-proxy

# 正式采集（国内用户需要 --trust-env-proxy 走系统代理）
python crawler_coin.py --symbols BTC,ETH,SOL --max-posts 200 --trust-env-proxy

# 采集 + 下载 HTML（会同时拦截评论数据）
python crawler_coin.py --symbols BTC --max-posts 100 --fetch-html --headless --trust-env-proxy
```

### 2. 单独下载 HTML（已有 DB 的情况）

```bash
# 下载所有帖子，自动拦截评论数据保存为 sidecar JSON
python fetch_coin_pages.py --db-path crawler_coin_output/coin_posts.db

# 只下载 BTC 相关
python fetch_coin_pages.py --symbol BTC --limit 50

# 覆盖已有 HTML（重新下载）
python fetch_coin_pages.py --overwrite

# 浏览器可见模式（调试）
python fetch_coin_pages.py --no-headless

# 分页下载
python fetch_coin_pages.py --offset 0 --limit 50
python fetch_coin_pages.py --offset 50 --limit 50
```

### 3. 解析 HTML 到 JSON

```bash
# 批量解析目录下所有 HTML（自动加载侧边评论文件）
python parse_article.py --batch --input crawler_coin_output/html_pages --output update_news/parsed_articles/binance_square_articles_parsed.json

# 解析单个文件
python parse_article.py --input crawler_coin_output/html_pages/315747499824338.html
```

### 4. 连通性检查

```bash
python fetch_coin_pages.py --check-only
```

## 修改说明（2026-04-25）

### 问题

`parse_article.py` 输出的 `comment_num` 和 `comments` 始终为空。原因是：

1. `fetch_coin_pages.py` 用 `domcontentloaded` + 1 秒等待保存 HTML，评论通过异步 API 动态加载，保存时没渲染出来，HTML 里只有空的 `<div id="comment">` 占位符
2. `parse_article.py` 的 DOM 正则提取和 `__APP_DATA` 递归搜索都找不到评论数据（`__APP_DATA` 只有 `commentCount: 6` 这个整数，不含实际文本）

### 修改 1：fetch_coin_pages.py

在 `download_page()` 中新增 Playwright response 拦截：

- 导航前注册 `page.on("response", on_response)`，捕获 URL 含 "comment" 的 API 响应
- 用 `crawler_comment.extract_comment_rows_from_payload()` 解析原始数据
- 另存为 sidecar JSON（如 `315747499824338_comments.json`）
- 等待时间从 1 秒延长到 5 秒

### 修改 2：parse_article.py

新增 `load_sidecar_comments(html_file)` 函数：

- DOM 提取和 `__APP_DATA` 都失败后，查找 `{stem}_comments.json`
- 加载并转换到解析器的评论格式
- 无需额外参数，自动生效

### 关于 API 回退

不提供直接 API 调用回退。Binance 评论接口需要登录态（返回 403），只有 Playwright 拦截方式（使用已登录的 Chromium profile）能获取到。

### 输出结构

**HTML 目录结构：**
```
crawler_coin_output/html_pages/
├── 315747499824338.html
├── 315747499824338_comments.json   ← 新增的 sidecar 评论文件
├── 315931877343745.html
└── 315931877343745_comments.json
```

**解析输出字段（与之前一致，新增评论数据）：**
```json
{
  "post_id": "315747499824338",
  "post_author": "Binance News",
  "post_content": "...",
  "post_time": "2026-04-24 04:59:51",
  "products": ["AI", "ETH"],
  "comment_num": 3,
  "comment_total_num": 3,
  "comments": [
    {
      "comment_id": "c1",
      "original_comment_id": "...",
      "author": "用户名",
      "text": "评论内容",
      "post_time": "2026-04-24 05:10:00",
      "post_time_ms": 1776982200000,
      "replies": [],
      "is_article_comment": true,
      "comment_error": ""
    }
  ],
  "label_error": "article_relative_time"
}
```

`label_error: "article_relative_time"` 不是错误，是文章类的标准标记，表示使用了相对时间格式（如 "1h ago"），非 bug。
