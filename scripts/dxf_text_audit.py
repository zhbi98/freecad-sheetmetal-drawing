#!/usr/bin/env python3
"""Audit final-DXF fixed notes, font resolution, and table-cell containment."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

REQUIRED_TITLEBLOCK_MERGED_REGIONS = {
    "tolerance_title", "drawing_name", "unit", "unit_value",
    "projection_symbol", "company_name",
}
DEFAULT_UNNOTED_TOLERANCE_TEXTS = (
    "未注公差按以下公差",
    "≤10", "±0.1", "≤90°", "±0.5°",
    ">10–30", "±0.15", ">90°–150°", "±1°",
    ">30–120", "±0.2", ">150°", "±1.5°",
    ">120–400", "±0.25", "单位", "mm",
    ">400", "±0.3",
)
DEFAULT_UNNOTED_TOLERANCE_LAYOUT_TEXTS = (
    "尺寸段", "尺寸公差", "尺寸段", "角度公差",
)
DEFAULT_TECHNICAL_REQUIREMENTS = (
    "技术要求：",
    "1. 端面平直无明显变形；",
    "2. 表面无划痕、无磕碰、锈迹等缺陷；",
    "3. 边角表面光滑，切口要平整无利边、毛刺、批锋、锐角；",
    "4. 带*号的尺寸为重点把控尺寸；",
    "5. 四周折弯边做焊接处理；",
)


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


def entity_plain_text(entity) -> str:
    if entity.dxftype() == "MTEXT":
        return str(entity.plain_text()).strip()
    return str(entity.dxf.get("text", "")).strip()


def entity_anchor(entity) -> tuple[float, float]:
    point = entity.dxf.get("insert", (0.0, 0.0, 0.0))
    if entity.dxftype() == "TEXT" and (
        entity.dxf.get("halign", 0) or entity.dxf.get("valign", 0)
    ):
        point = entity.dxf.get("align_point", point)
    return float(point.x), float(point.y)


def compact_logical_text(value: str) -> str:
    """Ignore display wrapping/spacing while preserving wording and punctuation."""
    return re.sub(r"\s+", "", value)


def box_inside(inner: list[float], outer: list[float], tolerance: float = 1e-6) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def main() -> int:
    args = parse_args()
    ezdxf, dxf_bbox, fonts = load_ezdxf(args.vendor_path)
    check = json.loads(args.check.read_text(encoding="utf-8"))
    doc = ezdxf.readfile(args.dxf)
    audit = doc.audit()
    modelspace = doc.modelspace()
    all_text = list(modelspace.query("TEXT MTEXT"))
    table_text = [e for e in all_text if e.dxf.layer in {"TABLE", "TITLEBLOCK"}]
    notes_text = [e for e in all_text if e.dxf.layer == "NOTES"]
    technical_notes_text = [
        entity for entity in notes_text
        if re.match(
            r"^(?:技术要求：|[1-9]\d*\.(?!\d))",
            compact_logical_text(entity_plain_text(entity)),
        )
    ]

    technical_requirement_items = []
    for entity in technical_notes_text:
        x, y = entity_anchor(entity)
        technical_requirement_items.append({
            "handle": str(entity.dxf.handle).upper(),
            "text": entity_plain_text(entity),
            "insert": [x, y],
        })
    technical_requirement_items.sort(
        key=lambda item: (-item["insert"][1], item["insert"][0], item["handle"])
    )
    expected_technical_compact = "".join(
        compact_logical_text(value) for value in DEFAULT_TECHNICAL_REQUIREMENTS
    )
    actual_technical_compact = "".join(
        compact_logical_text(item["text"])
        for item in technical_requirement_items
    )
    missing_technical_requirements = [
        value for value in DEFAULT_TECHNICAL_REQUIREMENTS
        if compact_logical_text(value) not in actual_technical_compact
    ]
    duplicate_technical_requirements = [
        value for value in DEFAULT_TECHNICAL_REQUIREMENTS
        if actual_technical_compact.count(compact_logical_text(value)) > 1
    ]
    technical_requirements_ok = actual_technical_compact == expected_technical_compact

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
    declared_tolerance_definition = table_layout.get("unnoted_tolerance")
    tolerance_definition = (
        declared_tolerance_definition
        if isinstance(declared_tolerance_definition, dict) else {}
    )
    tolerance_declaration_present = bool(tolerance_definition.get("source"))
    tolerance_source = tolerance_definition.get("source")
    if tolerance_source in {None, "skill_default"}:
        expected_tolerance_texts = list(DEFAULT_UNNOTED_TOLERANCE_TEXTS)
        tolerance_source_ok = (
            tolerance_declaration_present
            and tolerance_source == "skill_default"
            and (
                not tolerance_definition.get("expected_texts")
                or tolerance_definition.get("expected_texts") == expected_tolerance_texts
            )
        )
    elif tolerance_source == "confirmed_project_or_vendor_template":
        declared_tolerance_texts = tolerance_definition.get("expected_texts")
        tolerance_source_ok = (
            tolerance_definition.get("confirmed") is True
            and isinstance(declared_tolerance_texts, list)
            and bool(declared_tolerance_texts)
            and all(isinstance(value, str) and value.strip()
                    for value in declared_tolerance_texts)
        )
        expected_tolerance_texts = (
            declared_tolerance_texts if isinstance(declared_tolerance_texts, list) else []
        )
    else:
        expected_tolerance_texts = []
        tolerance_source_ok = False
    tolerance_title_box = region_by_name.get("tolerance_title")
    unit_box = region_by_name.get("unit")
    unit_value_box = region_by_name.get("unit_value")
    tolerance_box = None
    if all(finite_box(box) for box in (tolerance_title_box, unit_box, unit_value_box)):
        tolerance_box = [
            float(tolerance_title_box[0]),
            min(float(unit_box[1]), float(unit_value_box[1])),
            float(tolerance_title_box[2]),
            float(tolerance_title_box[3]),
        ]
    table_text_by_handle = {
        str(entity.dxf.handle).upper(): entity for entity in table_text
    }
    tolerance_items: list[dict[str, str]] = []
    if tolerance_box is not None:
        for handle, spec in manifest_by_handle.items():
            cell = spec.get("cell") if isinstance(spec, dict) else None
            entity = table_text_by_handle.get(handle)
            if entity is None or not finite_box(cell):
                continue
            cell_box = [float(value) for value in cell]
            if box_inside(cell_box, tolerance_box):
                tolerance_items.append({
                    "handle": handle,
                    "text": entity_plain_text(entity),
                })
    actual_tolerance_texts = [item["text"] for item in tolerance_items]
    expected_counter = Counter(expected_tolerance_texts)
    actual_counter = Counter(actual_tolerance_texts)
    if tolerance_source == "skill_default":
        expected_layout_texts = list(DEFAULT_UNNOTED_TOLERANCE_LAYOUT_TEXTS)
        expected_layout_counter = Counter(expected_layout_texts)
        actual_layout_counter = actual_counter & expected_layout_counter
        missing_layout_texts = sorted(
            (expected_layout_counter - actual_counter).elements()
        )
        comparable_actual_counter = actual_counter - expected_layout_counter
    else:
        expected_layout_texts = []
        actual_layout_counter = Counter()
        missing_layout_texts = []
        comparable_actual_counter = actual_counter
    missing_tolerance_texts = sorted(
        (expected_counter - comparable_actual_counter).elements()
    )
    extra_tolerance_texts = sorted(
        (comparable_actual_counter - expected_counter).elements()
    )
    pending_tolerance_items = [
        item for item in tolerance_items if "待确认" in item["text"]
    ]
    tolerance_ok = (
        tolerance_source_ok
        and tolerance_box is not None
        and not missing_layout_texts
        and not missing_tolerance_texts
        and not extra_tolerance_texts
        and not pending_tolerance_items
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
        and technical_requirements_ok
        and topology_ok
        and tolerance_ok
        and bbox_fail == outside == intersections == safety_fail == ratio_fail == 0
        and recorded_hash == actual_hash
        and all(item.get("pass") is True for item in results)
    )
    result = {
        "schema": "freecad-sheetmetal-drawing/dxf-text-audit/v6",
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
        "technical_requirements_validation": {
            "layer": "NOTES",
            "all_notes_entity_count": len(notes_text),
            "matched_technical_requirement_entity_count": len(technical_notes_text),
            "required_logical_item_count": len(DEFAULT_TECHNICAL_REQUIREMENTS),
            "expected_items": list(DEFAULT_TECHNICAL_REQUIREMENTS),
            "actual_entities": technical_requirement_items,
            "missing_items": missing_technical_requirements,
            "duplicate_items": duplicate_technical_requirements,
            "exact_ordered_content_match": technical_requirements_ok,
            "pass": technical_requirements_ok,
        },
        "titleblock_topology": {
            "required_merged_regions": sorted(REQUIRED_TITLEBLOCK_MERGED_REGIONS),
            "declared_merged_regions": sorted(region_by_name),
            "missing_merged_regions": missing_regions,
            "invalid_merged_regions": invalid_regions,
            "internal_grid_line_hits": topology_hits,
            "pass": topology_ok,
        },
        "unnoted_tolerance_validation": {
            "source": tolerance_source,
            "source_declaration_present": tolerance_declaration_present,
            "source_declaration_pass": tolerance_source_ok,
            "box": tolerance_box,
            "required_content_count": len(expected_tolerance_texts),
            "expected_texts": expected_tolerance_texts,
            "required_layout_count": len(expected_layout_texts),
            "expected_layout_texts": expected_layout_texts,
            "actual_layout_texts": sorted(actual_layout_counter.elements()),
            "missing_layout_texts": missing_layout_texts,
            "actual_items": tolerance_items,
            "missing_texts": missing_tolerance_texts,
            "extra_texts": extra_tolerance_texts,
            "pending_placeholder_items": pending_tolerance_items,
            "pass": tolerance_ok,
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
