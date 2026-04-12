# Binance Square Crawl

这个项目用于抓取 Binance Square 帖子页，并将帖子信息、评论信息整理成结构化文件，方便后续做情绪分析、文本分析或数据清洗。

当前项目包含两条主要处理链路：

1. 在线抓取页面  
   使用 [binance_square_crawler.py](/E:/code/sentiment/binance_square_crawler.py) 打开 Binance Square，采集帖子链接、保存页面 HTML/TXT/截图、尝试抓取评论接口与页面评论。
2. 离线解析文本  
   使用 [parse_binance_square_txt.py](/E:/code/sentiment/parse_binance_square_txt.py) 解析已经保存下来的帖子 `txt` 文件，提取帖子作者、正文、评论作者、评论内容、相关币种等字段，并导出 CSV。

## 项目结构

- [binance_square_crawler.py](/E:/code/sentiment/binance_square_crawler.py)：主抓取脚本
- [parse_binance_square_txt.py](/E:/code/sentiment/parse_binance_square_txt.py)：离线解析 `txt` 文件的脚本
- [update_news](/E:/code/sentiment/update_news)：运行输出目录
- [tmp_chrome_profile](/E:/code/sentiment/tmp_chrome_profile)：浏览器持久化登录目录

## 环境准备

建议使用 Python 3.10+。

安装依赖：

```bash
pip install requests playwright
playwright install chromium
```

## 推荐运行流程

推荐分成两步跑。

### 第一步：打开帖子页并保存页面内容

```bash
python binance_square_crawler.py --source square-home --max-posts 1 --max-comments 10 --wait-for-login --save-comment-debug --dump-page
```

这一步会做这些事：

- 打开 Binance Square 首页
- 等你手动登录
- 采集帖子链接
- 打开帖子详情页
- 保存页面 HTML、TXT、截图
- 尝试抓评论接口
- 尝试直接从页面抓评论

如果你主要想先把页面内容保存下来做离线解析，这一步最关键。

### 第二步：解析导出的 TXT

```bash
python parse_binance_square_txt.py --input update_news/binance_square_page_dump
```

这一步会从 `update_news/binance_square_page_dump` 中读取 `txt` 文件，并输出：

- `update_news/parsed_from_txt/binance_square_posts_from_txt.csv`
- `update_news/parsed_from_txt/binance_square_comments_from_txt.csv`
- `update_news/parsed_from_txt/binance_square_posts_from_txt.json`

## 主抓取脚本说明

脚本文件：[binance_square_crawler.py](/E:/code/sentiment/binance_square_crawler.py)

### 常用命令

只测试新闻流接口是否可达：

```bash
python binance_square_crawler.py --check-only --trust-env-proxy
```

抓 Binance Square 首页帖子，并等待手动登录：

```bash
python binance_square_crawler.py --source square-home --max-posts 20 --max-comments 20 --wait-for-login
```

抓帖子并保存页面 dump：

```bash
python binance_square_crawler.py --source square-home --max-posts 5 --max-comments 10 --wait-for-login --dump-page --save-comment-debug
```

### 参数说明

- `--source`
  帖子来源。
  `news` 表示使用新闻流接口。
  `square-home` 表示从 Binance Square 首页滚动采集帖子。

- `--pages`
  抓取新闻流时的页数，默认 `3`。

- `--page-size`
  每页帖子数，默认 `20`。

- `--max-posts`
  最多处理多少条帖子，默认 `50`。

- `--max-comments`
  每条帖子最多处理多少条评论，默认 `30`。

- `--min-comment-count`
  仅在 `news` 模式下使用，只有帖子元数据里的评论数不小于该值时才进入评论抓取。默认 `1`。

- `--lang`
  页面语言，默认 `zh-CN`。

- `--headless`
  是否启用无头浏览器。默认关闭，便于观察登录和页面变化。

- `--skip-comments`
  只抓帖子，不抓评论。

- `--output-dir`
  输出目录，默认 `update_news`。

- `--request-timeout`
  HTTP 请求超时时间，默认 `20` 秒。

- `--pause-seconds`
  每个帖子之间的等待时间，默认 `1.2` 秒。

- `--user-data-dir`
  Chromium 用户目录，默认 `tmp_chrome_profile`，用于保存登录态。

- `--wait-for-login`
  打开页面后暂停，等待你登录并回车后再继续。

- `--trust-env-proxy`
  默认脚本不读取系统代理环境变量。如果你访问 Binance 必须走代理，就加上这个参数。

- `--news-api`
  自定义新闻流接口地址，默认：

```text
https://www.binance.com/bapi/composite/v4/friendly/pgc/feed/news/list
```

- `--retries`
  接口失败自动重试次数，默认 `2`。

- `--check-only`
  只检查接口是否可达，不真正抓取。

- `--save-comment-debug`
  保存命中的评论相关接口响应到：
  `update_news/binance_square_comment_debug`

- `--dump-page`
  保存帖子详情页的 HTML、TXT、截图到：
  `update_news/binance_square_page_dump`

## 离线解析脚本说明

脚本文件：[parse_binance_square_txt.py](/E:/code/sentiment/parse_binance_square_txt.py)

### 常用命令

解析默认目录中的页面文本：

```bash
python parse_binance_square_txt.py --input update_news/binance_square_page_dump
```

解析单个 txt 文件：

```bash
python parse_binance_square_txt.py --input update_news/binance_square_page_dump/311465597934610_after_login.txt
```

### 参数说明

- `--input`
  输入文件或目录。可以传单个 `.txt` 文件，也可以传目录。

- `--output-dir`
  输出目录，默认 `update_news/parsed_from_txt`。

## 当前可导出的字段

### 帖子字段

- `post_id`
  帖子 ID。通常来自帖子链接末尾，例如：
  `https://www.binance.com/zh-CN/square/post/311560174559922`
  中的 `311560174559922`。

- `post_author`
  发帖人的展示名称，也就是页面上直接看到的作者昵称。

- `post_author_username`
  发帖人的用户名或主页标识。通常来自 `@username` 或作者主页链接。

- `post_time`
  发帖时间文本。当前通常保留页面原始展示形式，例如 `3小时`、`15分钟`。

- `post_content`
  帖子正文内容，不包含评论区内容。

- `disclaimer`
  帖子中出现的免责声明文本，例如“含第三方意见，不构成财务建议”等内容。

- `trade_pair`
  帖子中识别出的交易对，例如 `BTCUSDT`、`ARIAUSDT`。如果帖子里没有明确交易对，则可能为空。

- `related_symbols`
  从帖子正文、页面文本或相关链接中提取到的币种符号列表，多个币种用逗号拼接，例如：
  `BTC,ETH,ARIAUSDT`。

- `comment_count_hint`
  页面文本中识别到的评论数提示值。它是页面展示值，不一定等于最终实际抓到的评论条数。

- `like_count_hint`
  页面文本中识别到的点赞数提示值。它是页面展示值，不一定是严格意义上的接口原始值。

- `view_count_hint`
  页面文本中识别到的浏览量或曝光量提示值，例如 `15.5k`。

- `reply_count_hint`
  页面文本中识别到的“回复”提示值，例如 `回复 99` 中的 `99`。
  这个字段更接近“页面提示还有多少回复/评论”，不一定等于最终实际展开后的数量。

- `quote_count_hint`
  页面文本中识别到的引用数提示值，例如 `引用 2` 中的 `2`。

- `has_folded_comments`
  是否检测到“展示被折叠的评论”这类提示。
  `True` 表示页面上可能还有未展开的评论。
  `False` 表示当前页面文本里没有识别到该提示。

### 评论字段

- `comment_id`
  评论 ID。
  在离线 `txt` 解析模式下，这通常是程序按顺序生成的 ID，例如 `帖子ID_1`、`帖子ID_2`。
  它主要用于区分同一帖子下的不同评论，不一定是 Binance 官方评论 ID。

- `comment_author`
  评论作者的展示名称，也就是评论区显示出来的昵称。

- `comment_time`
  评论时间文本。当前通常保留页面原始展示形式，例如 `8分钟`、`2小时`。

- `comment_text`
  评论正文文本。

## 字段说明补充

- 名称里带 `hint` 的字段，表示它们来自页面展示文本的“提示值”或“近似值”，适合做分析参考，但不应直接当成严格精确统计值。
- 当前 `txt` 解析脚本主要处理“已经在页面上显示出来的内容”。如果页面里存在折叠评论、更多回复、楼中楼未展开，那么这些内容不会自动进入结果。
- 如果后续增强 crawler 自动展开“展示被折叠的评论”或“回复 99”，这些字段会更完整。

## 输出目录说明

主抓取脚本常见输出：

- [update_news/binance_square_posts.csv](/E:/code/sentiment/update_news/binance_square_posts.csv)
- [update_news/binance_square_comments.csv](/E:/code/sentiment/update_news/binance_square_comments.csv)
- [update_news/binance_square_posts_comments.csv](/E:/code/sentiment/update_news/binance_square_posts_comments.csv)
- [update_news/binance_square_posts_raw.json](/E:/code/sentiment/update_news/binance_square_posts_raw.json)
- [update_news/binance_square_comment_debug](/E:/code/sentiment/update_news/binance_square_comment_debug)
- [update_news/binance_square_page_dump](/E:/code/sentiment/update_news/binance_square_page_dump)

离线解析脚本输出：

- [update_news/parsed_from_txt](/E:/code/sentiment/update_news/parsed_from_txt)

## 已知限制

- Binance Square 评论区对登录态依赖较强，未登录时常无法完整看到评论。
- 当前评论接口未完全稳定，很多帖子仍需要依赖页面 DOM 或页面文本解析。
- `txt` 解析只能处理已经显示出来的评论；如果页面里存在“展示被折叠的评论”或“回复 99”等未展开内容，需要先在抓取阶段展开后再解析。
- 页面文案和结构可能会随 Binance 前端更新而变化，解析规则需要按实际页面持续调整。

## 建议使用方式

如果你的目标是先稳定拿到可分析数据，建议优先使用这套流程：

1. 用 `binance_square_crawler.py` 打开帖子页并保存 `txt`
2. 用 `parse_binance_square_txt.py` 解析 `txt`
3. 对导出的 CSV 继续做清洗、去重和情绪分析

这样比直接依赖网页内部接口更稳，也更容易排查问题。
