#!/usr/bin/env python3
"""Record an independent review of the final DXF readback preview."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", required=True, type=Path)
    parser.add_argument("--preview", required=True, type=Path)
    parser.add_argument("--visible-contour-breaks", required=True, type=int)
    parser.add_argument("--missing-internal-features", required=True, type=int)
    parser.add_argument("--hidden-pattern-issues", required=True, type=int)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    if not args.check.is_file():
        raise SystemExit(f"check record does not exist: {args.check}")
    if not args.preview.is_file() or args.preview.stat().st_size == 0:
        raise SystemExit(f"preview does not exist or is empty: {args.preview}")
    counts = (
        args.visible_contour_breaks,
        args.missing_internal_features,
        args.hidden_pattern_issues,
    )
    if any(value < 0 for value in counts):
        raise SystemExit("review counts must be non-negative")
    if not args.reviewer.strip():
        raise SystemExit("reviewer must not be empty")

    data = json.loads(args.check.read_text(encoding="utf-8"))
    projection = data.setdefault("projection_validation", {})
    passed = all(value == 0 for value in counts)
    projection["readback_line_review"] = {
        "source": "independent_final_dxf_preview_review",
        "preview_path": str(args.preview.resolve()),
        "preview_sha256": sha256(args.preview),
        "visible_contour_breaks": args.visible_contour_breaks,
        "missing_internal_features": args.missing_internal_features,
        "hidden_pattern_issues": args.hidden_pattern_issues,
        "reviewer": args.reviewer.strip(),
        "notes": args.notes,
        "pass": passed,
    }
    args.check.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
