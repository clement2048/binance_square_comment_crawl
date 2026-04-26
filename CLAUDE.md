# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Binance Square (币安广场) content crawler and parser. Three main pipelines:

1. **User Posts (v2/incremental)** — `crawler_v2.py` → `fetch_pages_from_db.py` → `parse_binance_square_html_final.py`
2. **Coin-specific News** — `crawler_coin.py` (BAPI) → `fetch_coin_pages.py` → `parse_article.py`
3. **Legacy** — `binance_square_crawler.py` / `fetch_comment.py` / `parse_binance_square_txt.py`

## Commands

### Setup
```bash
pip install -r requirements.txt
playwright install chromium
```

### User Posts Pipeline (v2 — recommended)
```bash
# Step 1: Incremental crawl post URLs to SQLite
python crawler_v2.py --lang en --target-posts 5000 --max-scroll-rounds 20000 --idle-stop-rounds 200 --wait-for-login --output-dir update_news_v2

# Step 2: Download HTML pages from DB
python fetch_pages_from_db.py --db-path update_news_v2/square_posts_v2.db --output-dir update_news/binance_square_page_dump --limit 200 --headless

# Step 3: Parse HTML to structured JSON
python parse_binance_square_html_final.py --batch --input update_news/binance_square_page_dump --output update_news/parsed_from_html/binance_square_html_parsed.json

# One-step: crawl + fetch HTML
python crawler_v2.py --lang en --target-posts 500 --fetch-html --html-limit 200 --headless
```

### Coin-Specific News Pipeline
```bash
# Check hit rate
python crawler_coin.py --check-only --symbols BTC,ETH,SOL --trust-env-proxy

# Crawl by symbols
python crawler_coin.py --symbols BTC,ETH,SOL --max-posts 200 --trust-env-proxy

# Crawl + download HTML
python crawler_coin.py --symbols BTC --max-posts 100 --fetch-html --headless --trust-env-proxy
```

### Data Cleaning
```bash
python clean_labeled_data.py --input <path> --drop-no-products --drop-label-error --min-comment-total 1
```

## Key Architecture

### Data Flow (v2 Pipeline)
```
Binance Square (infinite scroll)
       ↓ Playwright scroll + URL collection
crawler_v2.py → square_posts_v2.db (SQLite, deduplicated post index)
                      ↓ Playwright page navigation
       fetch_pages_from_db.py → *.html files
                      ↓ offline regex/JSON extraction
       parse_binance_square_html_final.py → structured JSON (post_id, author, content, time, product, comments)
```

### SQLite Schema
- **posts**: `post_id (TEXT PK)`, `link (TEXT UNIQUE)`, `first_seen_at`, `last_seen_at`, `seen_count`
- **runs**: per-run metadata (started_at, lang, target_posts, rounds_done, new_added, stop_reason)

### Key Scripts

| Script | Function |
|---|---|
| `crawler_v2.py` | Incremental post URL collector — scrolls Square home page, inserts unique URLs into SQLite |
| `fetch_pages_from_db.py` | Reads DB → downloads full HTML via Playwright (supports pre-filtering by age/comments/products) |
| `parse_binance_square_html_final.py` | Extracts post metadata + comments from HTML (APP_DATA JSON + DOM fallback) |
| `crawler_coin.py` | BAPI-based news crawler with client-side keyword matching (SYMBOL_ALIASES dict) |
| `fetch_coin_pages.py` | HTML downloader for coin posts (parallel to fetch_pages_from_db) |
| `parse_article.py` | Parses official news article HTML (relative time handling, different DOM structure) |
| `config.py` | Centralized CLI argument definitions for both crawler v1 and v2 |
| `crawler_util.py` | Shared utilities: `clean_text()`, `is_meaningful_comment()`, `extract_first_string()`, `ensure_dir()` |
| `crawler_comment.py` | Comment payload extraction helpers (recursive key walk, `looks_like_comment_node()`) |
| `clean_labeled_data.py` | Post-processing filter (drop posts with no products, label errors, low comment count) |

### Output Layout
- `update_news_v2/` — crawler_v2 outputs (DB, CSV, JSON, last_run.json)
- `update_news/binance_square_page_dump/` — downloaded HTML pages + fetch summaries/failures/filtered JSONs
- `update_news/parsed_from_html/` — parsed JSON output
- `crawler_coin_output/` — coin crawler DB, CSV, JSON
- `tmp_chrome_profile/` — persistent Chromium profile (for login session reuse)

### Key Design Decisions
- **Incremental + deduplicated**: URLs stored in SQLite with `first_seen_at`/`last_seen_at`/`seen_count`; re-running adds only new posts
- **Three-phase decoupling**: crawl (index only) → download HTML → parse offline. Each phase can be run separately, enables batch/retry
- **Client-side coin matching**: BAPI doesn't support server-side coin filtering, so all posts are fetched and matched locally via keyword aliases
- **Dual extraction**: `extract_app_data()` tries embedded JSON first, falls back to DOM text search for comments
- **Persistent browser profile**: Chromium user data dir (`tmp_chrome_profile/`) preserves login cookies across runs
