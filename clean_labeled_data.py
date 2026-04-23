#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean parsed Binance Square JSON by filtering low-quality records."
    )
    parser.add_argument(
        "--input",
        default="update_news/parsed_from_html/binance_square_html_parsed_price.json",
        help="Input JSON path (list of post objects)",
    )
    parser.add_argument(
        "--output",
        default="update_news/parsed_from_html/binance_square_html_parsed_price_clean.json",
        help="Output JSON path for kept records",
    )
    parser.add_argument(
        "--dropped-output",
        default="update_news/parsed_from_html/binance_square_html_parsed_price_dropped.json",
        help="Output JSON path for dropped records with drop reasons",
    )
    parser.add_argument(
        "--report-output",
        default="update_news/parsed_from_html/binance_square_html_parsed_price_clean_report.json",
        help="Output report JSON path",
    )
    parser.add_argument(
        "--min-comment-total",
        type=int,
        default=1,
        help="Keep posts with comment_total_num >= this threshold",
    )
    parser.add_argument(
        "--drop-no-products",
        action="store_true",
        help="Drop posts whose products is empty",
    )
    parser.add_argument(
        "--drop-label-error",
        action="store_true",
        help="Drop posts whose label_error is non-empty",
    )
    parser.add_argument(
        "--keep-comment-error-posts",
        action="store_true",
        help="If set, do NOT drop posts just because comments contain comment_error",
    )
    parser.add_argument(
        "--max-preview",
        type=int,
        default=20,
        help="Max number of dropped post ids in report preview",
    )
    return parser.parse_args()


def load_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Input JSON must be a list of post objects")
    return [item for item in data if isinstance(item, dict)]


def has_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def post_has_comment_error(post: dict[str, Any]) -> bool:
    comments = post.get("comments", [])
    if not isinstance(comments, list):
        return False

    def walk(nodes: list[dict[str, Any]]) -> bool:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if has_non_empty_string(node.get("comment_error")):
                return True
            replies = node.get("replies", [])
            if isinstance(replies, list) and walk(replies):
                return True
        return False

    typed_comments = [c for c in comments if isinstance(c, dict)]
    return walk(typed_comments)


def clean_posts(posts: list[dict[str, Any]], args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []

    reason_counter = {
        "low_comment_total": 0,
        "no_products": 0,
        "label_error": 0,
        "comment_error": 0,
    }

    for post in posts:
        reasons: list[str] = []

        comment_total = int(post.get("comment_total_num") or 0)
        if comment_total < int(args.min_comment_total):
            reasons.append("low_comment_total")

        products = post.get("products", [])
        if args.drop_no_products and (not isinstance(products, list) or len(products) == 0):
            reasons.append("no_products")

        if args.drop_label_error and has_non_empty_string(post.get("label_error")):
            reasons.append("label_error")

        if (not args.keep_comment_error_posts) and post_has_comment_error(post):
            reasons.append("comment_error")

        if reasons:
            for reason in sorted(set(reasons)):
                reason_counter[reason] += 1
            dropped.append(
                {
                    "post_id": post.get("post_id", ""),
                    "source_file": post.get("source_file", ""),
                    "drop_reasons": sorted(set(reasons)),
                    "post": post,
                }
            )
        else:
            kept.append(post)

    report = {
        "total_input": len(posts),
        "total_kept": len(kept),
        "total_dropped": len(dropped),
        "reason_counter": reason_counter,
        "params": {
            "min_comment_total": int(args.min_comment_total),
            "drop_no_products": bool(args.drop_no_products),
            "drop_label_error": bool(args.drop_label_error),
            "keep_comment_error_posts": bool(args.keep_comment_error_posts),
        },
        "dropped_post_ids_preview": [
            str(item.get("post_id", "")) for item in dropped[: max(0, int(args.max_preview))]
        ],
    }

    return kept, dropped, report


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    dropped_path = Path(args.dropped_output)
    report_path = Path(args.report_output)

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    posts = load_json(input_path)
    kept, dropped, report = clean_posts(posts, args)

    write_json(output_path, kept)
    write_json(dropped_path, dropped)
    write_json(report_path, report)

    print(
        f"[clean] input={report['total_input']} kept={report['total_kept']} dropped={report['total_dropped']}"
    )
    print(f"[clean] reasons={report['reason_counter']}")
    print(f"[clean] output={output_path}")
    print(f"[clean] dropped_output={dropped_path}")
    print(f"[clean] report={report_path}")


if __name__ == "__main__":
    main()
