"""Scan audit artifacts without printing sensitive matches."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
import json
from pathlib import Path
import re


CATEGORY_PATTERNS = {
    "absolute_path": re.compile(
        r"(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/]|\\\\|/(?:Users|home|mnt|var|tmp|etc|Volumes)/)",
        re.IGNORECASE,
    ),
    "database_artifact": re.compile(r"\.db(?:-wal|-shm)?\b", re.IGNORECASE),
    "raw_wecom_id": re.compile(r"\b[SRMOY]:\d+\b"),
    "secret_like": re.compile(
        r"(?:access[_-]?token|api[_-]?key|apikey|token|secret|password|key)"
        r"\s*[:=]\s*[^,\s}]+",
        re.IGNORECASE,
    ),
    "raw_content_sentinel": re.compile(
        r"(?:秘密|正文|private|message body|raw content)",
        re.IGNORECASE,
    ),
}
TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".txt"}


def scan_file(path: Path) -> set[str]:
    """Return privacy categories detected in one file."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    return {
        category
        for category, pattern in CATEGORY_PATTERNS.items()
        if pattern.search(text)
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan audit artifacts and report only file/category findings."
    )
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)

    root = Path.cwd()
    findings = []
    paths = args.paths or [Path("tests/fixtures"), Path("output")]
    for path in _iter_files(paths):
        for category in sorted(scan_file(path)):
            try:
                display_path = str(path.resolve().relative_to(root.resolve()))
            except ValueError:
                display_path = path.name
            findings.append(
                {"file": display_path.replace("\\", "/"), "category": category}
            )

    for finding in findings:
        print(json.dumps(finding, ensure_ascii=False, sort_keys=True))
    return 1 if findings else 0


def _iter_files(paths: list[Path]) -> Iterator[Path]:
    for path in paths:
        if not path.exists():
            continue
        if path.is_file():
            if path.suffix.lower() in TEXT_SUFFIXES:
                yield path
            continue
        for candidate in path.rglob("*"):
            if candidate.is_file() and candidate.suffix.lower() in TEXT_SUFFIXES:
                yield candidate


if __name__ == "__main__":
    raise SystemExit(main())
