# Binance Square 抓取与 HTML 解析

本项目当前推荐流程为两步：

1. 先抓取页面并保存帖子详情 HTML（可选同时抓评论调试信息）
2. 再离线解析 HTML，生成结构化 JSON

不包含 TXT 解析工作流。

## 项目脚本

- 主抓取脚本：binance_square_crawler.py
- HTML 解析脚本：parse_binance_square_html_final.py

## 环境准备

建议 Python 3.10+。

```bash
pip install requests playwright
playwright install chromium
```

## 主流程命令

### 1) 抓取页面（推荐）

```bash
python binance_square_crawler.py --source square-home --max-posts 20 --wait-for-login --dump-page
```

说明：
- 先打开 Binance Square 首页采集帖子链接
- 进入帖子页并保存页面 dump（HTML + 截图）
- 如未加 --skip-comments，会尝试抓评论（仅内存统计，不导出评论 CSV）

### 2) 批量解析 HTML

```bash
python parse_binance_square_html_final.py --input update_news/binance_square_page_dump --output update_news/parsed_from_html/binance_square_html_parsed.json --batch
```

## 英文页面抓取示例

```bash
python binance_square_crawler.py --source square-home --lang en --skip-comments --dump-page --wait-for-login
```

## 连通性检查命令

### square-home 检查（浏览器链路）

```bash
python binance_square_crawler.py --source square-home --check-only
```

### news 检查（可选代理环境变量）

```bash
python binance_square_crawler.py --source news --check-only --trust-env-proxy
```

说明：--trust-env-proxy 仅在依赖系统代理环境变量访问网络时使用。

## HTML 解析命令

### 解析单个 HTML 文件

```bash
python parse_binance_square_html_final.py --input update_news/binance_square_page_dump/你的帖子文件.html --output update_news/parsed_from_html/single_post.json
```

### 批量解析目录

```bash
python parse_binance_square_html_final.py --input update_news/binance_square_page_dump --output update_news/parsed_from_html/binance_square_html_parsed.json --batch
```

## 关键参数（精简）

### binance_square_crawler.py

- --source
  - news 或 square-home（当前主流程推荐 square-home）
- --max-posts
  - 最多处理帖子数
- --max-comments
  - 每帖最多抓取评论数
- --wait-for-login
  - 打开页面后等待手动登录再继续
- --dump-page
  - 保存帖子页 dump（当前流程使用 HTML + 截图）
- --skip-comments
  - 跳过评论抓取
- --lang
  - 页面语言，如 zh-CN、en
- --check-only
  - 只做连通性/可运行性检查，不写抓取结果
- --trust-env-proxy
  - 允许读取系统代理环境变量（按需）
- --save-comment-debug
  - 保存评论相关接口响应调试文件

### parse_binance_square_html_final.py

- --input
  - 单个 HTML 文件或目录（默认 update_news/binance_square_page_dump）
- --output
  - 输出 JSON 文件路径（默认 update_news/parsed_from_html/binance_square_html_parsed.json）
- --batch
  - 按目录批量处理

## 输出说明（当前准确状态）

### 爬虫输出

- update_news/binance_square_posts.csv（帖子汇总 CSV）
- update_news/binance_square_posts_raw.json（帖子原始/标准化 JSON）
- update_news/binance_square_page_dump/（页面 dump 目录，当前流程使用 HTML + 截图）
- update_news/binance_square_comment_debug/（仅在 --save-comment-debug 时生成）

### HTML 解析输出

- update_news/parsed_from_html/binance_square_html_parsed.json（批量解析默认输出）
- 或通过 --output 指定的任意 JSON 文件

补充：
- 抓取脚本当前已移除评论 CSV 与 merged CSV 导出
- 运行结束会打印评论抓取条数，但不会写 comments.csv 或 merged CSV

## 已知限制

- square-home 依赖页面结构与滚动加载，页面改版可能影响帖子链接采集
- 评论抓取受登录状态、页面展开状态、动态加载时机影响，完整性不保证
- HTML 解析依赖 DOM 与页面内嵌数据，若页面内容缺失或脚本数据变化，字段可能为空
- news 模式连通性受网络与地区限制影响，必要时使用 --trust-env-proxy

## 建议实践

如果你的目标是稳定离线分析，建议优先使用：

1. square-home 抓取 + --wait-for-login
2. --dump-page --skip-comments
3. 先用单文件测试 parse_binance_square_html_final.py，再做目录批量解析
