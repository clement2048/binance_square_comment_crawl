# Binance Square 抓取与 HTML 解析

当前推荐流程（大规模场景）为三步：

1. 用 `crawler_v2.py` 增量采集帖子链接到 SQLite（可断点续跑）
2. 用 `fetch_pages_from_db.py` 从 DB 批量下载帖子 HTML
3. 用 `parse_binance_square_html_final.py` 离线解析 HTML，生成结构化 JSON

不包含 TXT 解析工作流。

## 项目脚本

- 增量采集器（推荐）：`crawler_v2.py`
- DB 转 HTML 下载器：`fetch_pages_from_db.py`
- HTML 解析器：`parse_binance_square_html_final.py`

## 环境准备

建议 Python 3.10+。

```bash
pip install requests playwright
playwright install chromium
```

## 推荐主流程（v2）

### 1) 增量采集帖子到 SQLite(以5000条为例)

```bash
python crawler_v2.py --lang en --target-posts 5000 --max-scroll-rounds 20000 --idle-stop-rounds 200 --wait-for-login --output-dir update_news_v2
```

说明：
- 这是“目标驱动 + 持久化去重”模式，适合 5000/100000 这类大规模场景。
- 结果会持续累积到 `update_news_v2/square_posts_v2.db`。
- 再次运行同命令会在已有基础上继续采集（不会从零开始）。

### 2) 从 DB 批量下载 HTML

```bash
python fetch_pages_from_db.py --db-path update_news_v2/square_posts_v2.db --output-dir update_news/binance_square_page_dump --limit 200 --headless
```

常用变体：

```bash
# 覆盖已存在 HTML
python fetch_pages_from_db.py --db-path update_news_v2/square_posts_v2.db --output-dir update_news/binance_square_page_dump --limit 200 --headless --overwrite

# 分批拉取（例如第 201~400 条）
python fetch_pages_from_db.py --db-path update_news_v2/square_posts_v2.db --output-dir update_news/binance_square_page_dump --offset 200 --limit 200 --headless
```

说明：
- 默认会跳过已存在 HTML 文件（日志中的 `skipped` 是正常行为）。
- 默认只保存 HTML，较省空间；可选 `--save-screenshot` 保存截图。

### 3) 批量解析 HTML

```bash
python parse_binance_square_html_final.py --batch --input update_news/binance_square_page_dump --output update_news/parsed_from_html/binance_square_html_parsed.json
```

## 解析器输出字段说明（重要）

`parse_binance_square_html_final.py` 当前会输出：

- `post_id`：帖子的html的id
- `post_author`：帖子作者
- `post_content`：帖子评论内容
- `post_time`：帖子时间
- `product`：与该贴相关铲平
- `comments`：帖子下方的评论

其中 `product` 规则：

- 按“当前帖子范围”提取，不再扫描整页噪音
- 交易对会归一为基础币种（如 `RAVEUSDT -> RAVE`）
- 输出为数组，如：`["RAVE"]`

## 连通性检查

### crawler_v2 浏览器链路检查

```bash
python crawler_v2.py --check-only --lang en
```

### DB->HTML 下载链路检查

```bash
python fetch_pages_from_db.py --check-only --db-path update_news_v2/square_posts_v2.db --output-dir update_news/binance_square_page_dump --headless
```

## 输出目录（v2）

### crawler_v2 输出

- `update_news_v2/square_posts_v2.db`（去重主库）
- `update_news_v2/binance_square_posts_raw.json`（快照导出）
- `update_news_v2/crawler_v2_last_run.json`（最近一次运行摘要）

### fetch_pages_from_db 输出

- `update_news/binance_square_page_dump/*.html`
- `update_news/binance_square_page_dump/fetch_pages_from_db_summary.json`
- `update_news/binance_square_page_dump/fetch_pages_from_db_failures.json`（仅失败时生成）

### parse 输出

- `update_news/parsed_from_html/binance_square_html_parsed.json`

## 已知限制

- `square-home` 是动态推荐流，不是稳定分页，无法保证“单次运行精确命中 N 条新增”。
- 大规模采集建议多次运行累积到目标值（`target-posts`）。
- 评论区展开受登录态、页面渲染与动态加载影响，完整性仍可能波动。
- HTML 解析依赖页面结构与内嵌数据，页面改版可能需要更新规则。

## 建议实践

1. 先用 `crawler_v2.py` 累积索引，再分批下载 HTML，再离线解析。
2. 大批量下载建议使用 `--offset + --limit` 分批执行，便于失败重试。
3. 若空间紧张，优先只存 HTML，不保存截图。
4. 解析前先抽样检查 5-10 个 HTML 的页面完整性。
