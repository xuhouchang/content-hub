#!/usr/bin/env python3
"""Immediately save available filtered collector output.

This is the non-polling companion to ``poll_and_save.py``.  The actual merge,
fetch, Markdown, registry, and summary logic lives there so manual saves and
the background polling path cannot drift apart again.
"""

import argparse
import datetime
import json
from pathlib import Path

from poll_and_save import SOURCE_CONFIGS, TMP_DIR, merge_with_raw


def _load_json(path: Path) -> list:
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, list):
        raise TypeError(f"Expected a JSON list in {path}")
    return data


def save_available(source: str, date_str: str) -> int:
    """Save already available data for one source without polling."""
    config = SOURCE_CONFIGS[source]
    source_dir = TMP_DIR / source
    raw_path = source_dir / config["raw_pattern"].format(date=date_str)
    filtered_path = source_dir / config["filter_pattern"].format(date=date_str)

    if not raw_path.exists():
        print(f"No {source} data for {date_str}")
        return 0

    try:
        raw = _load_json(raw_path)
        if filtered_path.exists():
            filtered = _load_json(filtered_path)
            items = merge_with_raw(filtered, raw)
            print(
                f"Agent-filtered: {len(items)} merged from "
                f"{len(filtered)} verdicts / {len(raw)} raw"
            )
        else:
            items = [item for item in raw if item.get("_relevance_quick") == "pass"]
            print(f"Quick-pass fallback: {len(items)} items")
    except (OSError, TypeError, json.JSONDecodeError) as error:
        print(f"Invalid {source} data: {error}")
        return 1

    if not items:
        print("No items passed filter, nothing to save")
        return 0

    config["save_fn"](items, date_str)
    return 0


def main(source: str | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    if source is None:
        parser.add_argument("source", choices=sorted(SOURCE_CONFIGS))
    parser.add_argument(
        "--date",
        default=datetime.datetime.now().astimezone().date().isoformat(),
    )
    args = parser.parse_args()
    return save_available(source or args.source, args.date)


if __name__ == "__main__":
    raise SystemExit(main())
