#!/usr/bin/env python3
"""Audit final-DXF text, font resolution, and table-cell containment."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

REQUIRED_TITLEBLOCK_MERGED_REGIONS = {
    "tolerance_title", "drawing_name", "unit", "unit_value",
    "projection_symbol", "company_name",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf", type=Path)
    parser.add_argument("--check", required=True, type=Path,
                        help="Engineering check JSON containing the text-to-cell manifest.")
    parser.add_argument("--vendor-path", type=Path)
    parser.add_argument("--json-out", type=Path)
    return parser.parse_args()


def load_ezdxf(vendor_path: Path | None):
    if vendor_path:
        sys.path.insert(0, str(vendor_path.resolve()))
    try:
        import ezdxf
        from ezdxf import bbox
        from ezdxf.fonts import fonts
    except ImportError as exc:
        raise SystemExit("ezdxf is required; run in the drawing environment or pass --vendor-path.") from exc
    return ezdxf, bbox, fonts


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def finite_box(values: Any) -> bool:
    return (
        isinstance(values, list) and len(values) == 4
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)
        and values[2] > values[0] and values[3] > values[1]
    )


def segment_intersects_box(start: tuple[float, float], end: tuple[float, float], box: list[float]) -> bool:
    """Liang-Barsky segment/AABB intersection, including touching."""
    x1, y1, x2, y2 = box
    dx, dy = end[0] - start[0], end[1] - start[1]
    p = (-dx, dx, -dy, dy)
    q = (start[0] - x1, x2 - start[0], start[1] - y1, y2 - start[1])
    low, high = 0.0, 1.0
    for pi, qi in zip(p, q):
        if abs(pi) < 1e-12:
            if qi < 0:
                return False
            continue
        ratio = qi / pi
        if pi < 0:
            low = max(low, ratio)
        else:
            high = min(high, ratio)
        if low > high:
            return False
    return True


def segment_crosses_open_box(start: tuple[float, float], end: tuple[float, float], box: list[float]) -> bool:
    """Return True only when a segment enters a merged cell's open interior."""
    x1, y1, x2, y2 = box
    epsilon = max(x2 - x1, y2 - y1, 1.0) * 1e-8
    inner = [x1 + epsilon, y1 + epsilon, x2 - epsilon, y2 - epsilon]
    if inner[2] <= inner[0] or inner[3] <= inner[1]:
        return False
    return segment_intersects_box(start, end, inner)


def main() -> int:
    args = parse_args()
    ezdxf, dxf_bbox, fonts = load_ezdxf(args.vendor_path)
    check = json.loads(args.check.read_text(encoding="utf-8"))
    doc = ezdxf.readfile(args.dxf)
    audit = doc.audit()
    modelspace = doc.modelspace()
    all_text = list(modelspace.query("TEXT MTEXT"))
    table_text = [e for e in all_text if e.dxf.layer in {"TABLE", "TITLEBLOCK"}]

    declared_styles: dict[str, str] = {}
    substitutions: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []
    for entity in all_text:
        style_name = entity.dxf.get("style", "Standard")
        if style_name in declared_styles:
            continue
        style = doc.styles.get(style_name)
        declared = str(style.dxf.font or "")
        declared_styles[style_name] = declared
        try:
            resolved = fonts.get_font_face(declared)
            resolved_name = str(resolved.filename or "")
        except Exception:
            unresolved.append({"style": style_name, "declared": declared})
            continue
        if not resolved_name:
            unresolved.append({"style": style_name, "declared": declared})
        elif Path(resolved_name).name.casefold() != Path(declared).name.casefold():
            substitutions.append({"style": style_name, "declared": declared, "resolved": resolved_name})

    table_layout = check.get("table_layout") or {}
    containment = table_layout.get("text_cell_containment") or {}
    manifest = containment.get("items")
    manifest_ok = isinstance(manifest, list)
    manifest_by_handle = {
        str(item.get("handle", "")).upper(): item
        for item in manifest or [] if isinstance(item, dict) and item.get("handle")
    }
    table_handles = {str(e.dxf.handle).upper() for e in table_text}
    missing_handles = sorted(table_handles - set(manifest_by_handle))
    stale_handles = sorted(set(manifest_by_handle) - table_handles)

    grid_lines = [
        e for e in modelspace.query("LINE")
        if e.dxf.layer in {"TABLE", "TITLEBLOCK"}
    ]
    topology = table_layout.get("titleblock_topology") or {}
    merged_regions = topology.get("merged_regions")
    region_by_name = {
        str(region.get("name", "")): region.get("box")
        for region in merged_regions or []
        if isinstance(region, dict) and region.get("name")
    }
    missing_regions = sorted(REQUIRED_TITLEBLOCK_MERGED_REGIONS - set(region_by_name))
    invalid_regions = sorted(
        name for name, box in region_by_name.items() if not finite_box(box)
    )
    topology_hits: list[dict[str, Any]] = []
    for name, region_box in region_by_name.items():
        if not finite_box(region_box):
            continue
        box = [float(value) for value in region_box]
        hit_handles = []
        for grid in grid_lines:
            if grid.dxf.layer != "TITLEBLOCK":
                continue
            start = (float(grid.dxf.start.x), float(grid.dxf.start.y))
            end = (float(grid.dxf.end.x), float(grid.dxf.end.y))
            if segment_crosses_open_box(start, end, box):
                hit_handles.append(str(grid.dxf.handle))
        if hit_handles:
            topology_hits.append({"name": name, "box": box, "line_handles": hit_handles})
    topology_ok = (
        isinstance(merged_regions, list)
        and not missing_regions and not invalid_regions and not topology_hits
    )
    results: list[dict[str, Any]] = []
    outside = intersections = safety_fail = ratio_fail = bbox_fail = 0
    min_clearance = math.inf
    for entity in table_text:
        handle = str(entity.dxf.handle).upper()
        spec = manifest_by_handle.get(handle)
        if not spec or not finite_box(spec.get("cell")):
            results.append({"handle": handle, "text": entity.dxf.get("text", ""), "pass": False,
                            "error": "missing_or_invalid_cell_manifest"})
            continue
        cell = [float(v) for v in spec["cell"]]
        glyph = dxf_bbox.extents([entity], fast=True)
        if not glyph.has_data:
            bbox_fail += 1
            results.append({"handle": handle, "text": entity.dxf.get("text", ""), "cell": cell,
                            "pass": False, "error": "no_glyph_bbox"})
            continue
        glyph_box = [glyph.extmin.x, glyph.extmin.y, glyph.extmax.x, glyph.extmax.y]
        clearance = [
            glyph_box[0] - cell[0], cell[2] - glyph_box[2],
            glyph_box[1] - cell[1], cell[3] - glyph_box[3],
        ]
        min_clearance = min(min_clearance, *clearance)
        height = float(entity.dxf.get("height", spec.get("height", 0.0)))
        ratio = (glyph_box[3] - glyph_box[1]) / (cell[3] - cell[1])
        is_outside = any(value < 0 for value in clearance)
        safe = (
            height > 0 and clearance[0] >= 0.20 * height and clearance[1] >= 0.20 * height
            and clearance[2] >= 0.10 * height and clearance[3] >= 0.10 * height
        )
        ratio_required = spec.get("ratio_required", True) is not False
        ratio_ok = (not ratio_required) or 0.68 <= ratio <= 0.84
        hit_handles = []
        for grid in grid_lines:
            start = (float(grid.dxf.start.x), float(grid.dxf.start.y))
            end = (float(grid.dxf.end.x), float(grid.dxf.end.y))
            if segment_intersects_box(start, end, glyph_box):
                hit_handles.append(str(grid.dxf.handle))
        outside += int(is_outside)
        safety_fail += int(not safe)
        ratio_fail += int(not ratio_ok)
        intersections += int(bool(hit_handles))
        passed = not is_outside and safe and ratio_ok and not hit_handles
        results.append({
            "handle": handle,
            "text": entity.dxf.get("text", ""),
            "layer": entity.dxf.layer,
            "style": entity.dxf.get("style", "Standard"),
            "height": height,
            "cell": cell,
            "glyph_bbox": glyph_box,
            "clearance_mm": clearance,
            "height_ratio": ratio,
            "ratio_required": ratio_required,
            "intersecting_grid_handles": hit_handles,
            "pass": passed,
        })

    recorded_hash = (check.get("readback") or {}).get("sha256")
    actual_hash = sha256(args.dxf)
    result_pass = (
        not audit.errors and not audit.fixes
        and manifest_ok and len(manifest_by_handle) == len(table_text)
        and not missing_handles and not stale_handles
        and not substitutions and not unresolved
        and topology_ok
        and bbox_fail == outside == intersections == safety_fail == ratio_fail == 0
        and recorded_hash == actual_hash
        and all(item.get("pass") is True for item in results)
    )
    result = {
        "schema": "freecad-sheetmetal-drawing/dxf-text-audit/v1",
        "path": str(args.dxf.resolve()),
        "sha256": actual_hash,
        "recorded_sha256": recorded_hash,
        "status": "pass" if result_pass else "fail",
        "pass": result_pass,
        "all_text_entities": len(all_text),
        "checked_table_text_entities": len(table_text),
        "manifest_entries": len(manifest_by_handle),
        "missing_handles": missing_handles,
        "stale_handles": stale_handles,
        "font_validation": {
            "declared_styles": declared_styles,
            "substitutions": substitutions,
            "unresolved": unresolved,
            "pass": not substitutions and not unresolved,
        },
        "titleblock_topology": {
            "required_merged_regions": sorted(REQUIRED_TITLEBLOCK_MERGED_REGIONS),
            "declared_merged_regions": sorted(region_by_name),
            "missing_merged_regions": missing_regions,
            "invalid_merged_regions": invalid_regions,
            "internal_grid_line_hits": topology_hits,
            "pass": topology_ok,
        },
        "containment": {
            "minimum_actual_clearance_mm": None if math.isinf(min_clearance) else min_clearance,
            "bbox_fail_count": bbox_fail,
            "outside_cell_count": outside,
            "line_intersection_count": intersections,
            "safety_clearance_fail_count": safety_fail,
            "height_ratio_fail_count": ratio_fail,
            "items": results,
            "pass": result_pass,
        },
        "audit_errors": len(audit.errors),
        "audit_fixes": len(audit.fixes),
    }
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json_out:
        args.json_out.write_text(payload, encoding="utf-8")
    print(payload)
    return 0 if result_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
