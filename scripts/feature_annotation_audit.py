#!/usr/bin/env python3
"""Compare STEP circular-hole inventory and reject automatic grouped callouts."""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import sys

from projection_transform import model_side_names


SUPPLEMENTARY_POLICY = "main_views_only_unless_traceable_and_necessary"
SUPPLEMENTARY_KINDS = {"detail", "section", "auxiliary"}
SUPPLEMENTARY_REASONS = {
    "hidden_feature_not_unambiguously_dimensionable",
    "non_true_shape_feature",
    "internal_feature_requires_section",
    "main_view_annotation_congestion_after_layout_attempts",
}
SUPPLEMENTARY_LABEL_RE = re.compile(r"^\s*(?:K|P|S)\d{2}(?!\d)", re.IGNORECASE)
EXPECTED_VIEWS = {"FRONT", "REAR", "LEFT", "RIGHT", "TOP", "BOTTOM"}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf", type=Path)
    parser.add_argument("--step", required=True, type=Path)
    parser.add_argument("--check", required=True, type=Path)
    parser.add_argument("--vendor-path", type=Path)
    parser.add_argument("--json-out", type=Path)
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fmt(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def load_dependencies(vendor_path: Path | None):
    candidates = [vendor_path] if vendor_path is not None else []
    candidates.extend((Path.cwd() / ".cadlib", Path.cwd() / "vendor"))
    for candidate in candidates:
        if candidate.is_dir():
            sys.path.insert(0, str(candidate.resolve()))
            break
    try:
        import ezdxf
        import FreeCAD as App
        import Part
    except ImportError as exc:
        raise SystemExit("Run with FreeCAD Python and provide an ezdxf vendor path.") from exc
    return ezdxf, App, Part


def canonical_axis(App, vector):
    axis = App.Vector(vector)
    axis.normalize()
    values = (axis.x, axis.y, axis.z)
    dominant = max(range(3), key=lambda i: abs(values[i]))
    return -axis if values[dominant] < 0 else axis


def entity_text(entity) -> str:
    kind = entity.dxftype()
    if kind == "MTEXT":
        try:
            return str(entity.plain_text())
        except Exception:
            return str(entity.dxf.text)
    if kind in {"TEXT", "ATTRIB"}:
        return str(entity.dxf.text)
    return ""


def valid_box(value) -> bool:
    if not isinstance(value, list) or len(value) != 4:
        return False
    if not all(isinstance(item, (int, float)) and not isinstance(item, bool)
               and math.isfinite(float(item)) for item in value):
        return False
    return float(value[0]) < float(value[2]) and float(value[1]) < float(value[3])


def valid_direction(value) -> bool:
    if not isinstance(value, list) or len(value) != 3:
        return False
    if not all(isinstance(item, (int, float)) and not isinstance(item, bool)
               and math.isfinite(float(item)) for item in value):
        return False
    return math.sqrt(sum(float(item) ** 2 for item in value)) > 1e-9


def valid_scale(value) -> bool:
    if not isinstance(value, str):
        return False
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*", value)
    return bool(match and float(match.group(1)) > 0.0 and float(match.group(2)) > 0.0)


def normalized_handle(value) -> str:
    return str(value or "").strip().upper()


def handle_list(value):
    if not isinstance(value, list):
        return []
    return [normalized_handle(item) for item in value if normalized_handle(item)]


def audit_supplementary_views(document, record, main_view_transform_ids=None):
    """Reject untraceable detail/section/auxiliary views in the final DXF."""
    main_view_transform_ids = main_view_transform_ids or {}
    entities = list(document.modelspace())
    by_handle = {normalized_handle(entity.dxf.handle): entity for entity in entities
                 if normalized_handle(entity.dxf.handle)}
    actual_labels = []
    for entity in entities:
        if entity.dxftype() not in {"TEXT", "MTEXT", "ATTRIB"}:
            continue
        text = entity_text(entity).strip()
        layer = str(entity.dxf.layer or "").strip().upper()
        if layer == "SUPPLEMENTARY_LABEL" or SUPPLEMENTARY_LABEL_RE.match(text):
            actual_labels.append({
                "handle": normalized_handle(entity.dxf.handle),
                "layer": layer,
                "text": text,
            })

    per_view = record.get("per_view") if isinstance(record, dict) else None
    per_view = per_view if isinstance(per_view, list) else []
    recorded_count = record.get("declared_count") if isinstance(record, dict) else None
    top_level_ok = (
        isinstance(record, dict)
        and record.get("policy") == SUPPLEMENTARY_POLICY
        and isinstance(recorded_count, int) and not isinstance(recorded_count, bool)
        and recorded_count == len(per_view)
        and record.get("pass") is True
    )

    results = []
    identifiers = []
    declared_label_handles = []
    for entry in per_view:
        entry = entry if isinstance(entry, dict) else {}
        identifier = str(entry.get("id") or "").strip().upper()
        identifiers.append(identifier)
        source_view = str(entry.get("source_main_view") or "").strip().upper()
        source_label = normalized_handle(entry.get("source_label_handle"))
        title_label = normalized_handle(entry.get("title_label_handle"))
        declared_label_handles.extend((source_label, title_label))
        source_boundary = handle_list(entry.get("source_boundary_handles"))
        direction_arrows = handle_list(entry.get("direction_arrow_handles"))
        geometry = handle_list(entry.get("geometry_handles"))
        dimensions = handle_list(entry.get("dimension_handles"))
        all_handles = ([source_label, title_label] + source_boundary
                       + direction_arrows + geometry + dimensions)
        missing_handles = sorted({handle for handle in all_handles
                                  if handle and handle not in by_handle})
        source_text = entity_text(by_handle[source_label]) if source_label in by_handle else ""
        title_text = entity_text(by_handle[title_label]) if title_label in by_handle else ""
        id_pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(identifier)}(?![A-Z0-9])",
                                re.IGNORECASE) if identifier else None
        source_transform = str(entry.get("source_transform_id") or "").strip()
        expected_transform = str(main_view_transform_ids.get(source_view) or "").strip()
        checks = {
            "id": bool(identifier and SUPPLEMENTARY_LABEL_RE.fullmatch(identifier)),
            "kind": entry.get("kind") in SUPPLEMENTARY_KINDS,
            "necessity_reason": entry.get("necessity_reason") in SUPPLEMENTARY_REASONS,
            "source_feature_ids": bool(entry.get("source_feature_ids"))
                                  and isinstance(entry.get("source_feature_ids"), list),
            "source_main_view": source_view in EXPECTED_VIEWS,
            "source_transform": bool(source_transform)
                                and (not expected_transform or source_transform == expected_transform),
            "projection_transform": bool(str(entry.get("projection_transform_id") or "").strip()),
            "source_bounds": valid_box(entry.get("source_bounds_sheet_mm")),
            "view_bounds": valid_box(entry.get("view_bounds_sheet_mm")),
            "view_direction": valid_direction(entry.get("view_direction_3d")),
            "scale": valid_scale(entry.get("scale")),
            "distinct_labels": bool(source_label and title_label and source_label != title_label),
            "source_boundary": bool(source_boundary),
            "direction_arrow": bool(direction_arrows),
            "geometry": bool(geometry),
            "dimension_handles_type": isinstance(entry.get("dimension_handles"), list),
            "handles_exist": not missing_handles and all(all_handles),
            "source_label_text": bool(id_pattern and id_pattern.search(source_text)),
            "title_label_text": bool(id_pattern and id_pattern.search(title_text)),
            "record_pass": entry.get("pass") is True,
        }
        results.append({
            "id": identifier,
            "checks": checks,
            "missing_handles": missing_handles,
            "pass": all(checks.values()),
        })

    duplicate_ids = sorted({item for item in identifiers if item and identifiers.count(item) > 1})
    declared_label_handles = [item for item in declared_label_handles if item]
    duplicate_label_handles = sorted({item for item in declared_label_handles
                                      if declared_label_handles.count(item) > 1})
    actual_handle_set = {item["handle"] for item in actual_labels if item["handle"]}
    declared_handle_set = set(declared_label_handles)
    unregistered = sorted(actual_handle_set - declared_handle_set)
    missing_declared = sorted(declared_handle_set - actual_handle_set)
    passed = (
        top_level_ok
        and not duplicate_ids and not duplicate_label_handles
        and not unregistered and not missing_declared
        and len(actual_labels) == 2 * len(per_view)
        and all(item["pass"] for item in results)
    )
    return {
        "policy": record.get("policy") if isinstance(record, dict) else None,
        "declared_count": recorded_count,
        "recorded_view_count": len(per_view),
        "actual_label_count": len(actual_labels),
        "actual_label_items": actual_labels,
        "duplicate_ids": duplicate_ids,
        "duplicate_declared_label_handles": duplicate_label_handles,
        "unregistered_actual_label_handles": unregistered,
        "missing_declared_label_handles": missing_declared,
        "items": results,
        "pass": passed,
    }


def independent_groups(step: Path, App, Part, axis_view):
    document = App.newDocument("independent_feature_annotation_audit")
    try:
        Part.insert(str(step.resolve()), document.Name)
        document.recompute()
        objects = [obj for obj in document.Objects
                   if hasattr(obj, "Shape") and not obj.Shape.isNull()]
        sheets = [obj for obj in objects
                  if max(obj.Shape.BoundBox.XLength, obj.Shape.BoundBox.YLength,
                         obj.Shape.BoundBox.ZLength) > 50.0]
        physical = defaultdict(list)
        for obj in sheets:
            for index, face in enumerate(obj.Shape.Faces, 1):
                if type(face.Surface).__name__ != "Cylinder":
                    continue
                surface = face.Surface
                axis = canonical_axis(App, surface.Axis)
                center = App.Vector(surface.Center)
                offset = center - axis * center.dot(axis)
                coordinates = [vertex.Point.dot(axis) for vertex in face.Vertexes]
                if not coordinates:
                    continue
                u0, u1, _, _ = face.ParameterRange
                key = (
                    round(axis.x, 5), round(axis.y, 5), round(axis.z, 5),
                    round(offset.x, 3), round(offset.y, 3), round(offset.z, 3),
                    round(min(coordinates), 3), round(max(coordinates), 3),
                    round(2.0 * float(surface.Radius), 2),
                )
                physical[key].append(abs(u1 - u0))
        holes = []
        for key, spans in physical.items():
            if sum(spans) < 2.0 * math.pi - 1e-3:
                continue
            values = key[:3]
            dominant = max(range(3), key=lambda i: abs(values[i]))
            holes.append({"axis": "XYZ"[dominant], "diameter": key[8]})
        grouped = defaultdict(int)
        for hole in holes:
            grouped[(hole["axis"], hole["diameter"])] += 1
        return [{
            "group_id": f"{axis}_D{fmt(diameter)}",
            "axis": axis, "diameter": diameter, "count": count,
            "target_view": axis_view[axis],
        } for (axis, diameter), count in sorted(grouped.items())], len(holes)
    finally:
        App.closeDocument(document.Name)


def main() -> int:
    args = parse_args()
    for path, label in ((args.step, "STEP"), (args.dxf, "DXF"), (args.check, "check")):
        if not path.is_file():
            raise SystemExit(f"{label} does not exist: {path}")
    check = json.loads(args.check.read_text(encoding="utf-8"))
    side_names = model_side_names(str(check.get("front_source") or "FRONT"))
    axis_view = {axis: side_names[index][0] for index, axis in enumerate("XYZ")}
    ezdxf, App, Part = load_dependencies(args.vendor_path)
    expected, feature_count = independent_groups(args.step, App, Part, axis_view)
    expected_by_id = {entry["group_id"]: entry for entry in expected}
    recorded = (check.get("feature_annotation_validation") or {}).get("per_group") or []
    recorded_by_id = {str(entry.get("group_id") or ""): entry for entry in recorded}
    document = ezdxf.readfile(args.dxf)
    audit = document.audit()
    modelspace = document.modelspace()
    forbidden_callout_handles = sorted(
        str(entity.dxf.handle) for entity in modelspace
        if entity.dxf.layer == "FEATURE_CALLOUT"
    )
    results = []
    for group_id, expected_group in expected_by_id.items():
        record = recorded_by_id.get(group_id, {})
        record_ok = all(record.get(key) == expected_group[key]
                        for key in ("axis", "diameter", "count", "target_view"))
        policy_ok = (record.get("annotation_policy") == "no_automatic_grouped_callout"
                     and record.get("displayed") is False
                     and not record.get("text_handle") and not record.get("leader_handle"))
        results.append({"group_id": group_id, "expected": expected_group,
                        "record": record, "record_ok": record_ok,
                        "policy_ok": policy_ok,
                        "pass": record_ok and policy_ok})
    missing_group_ids = sorted(set(expected_by_id) - set(recorded_by_id))
    extra_group_ids = sorted(set(recorded_by_id) - set(expected_by_id))
    step_hash, dxf_hash = sha256(args.step), sha256(args.dxf)
    record_policy_ok = (
        (check.get("feature_annotation_validation") or {}).get("annotation_policy")
        == "no_automatic_grouped_callout"
        and (check.get("feature_annotation_validation") or {}).get(
            "displayed_group_callouts") == 0
    )
    transform_ids = {
        str(name).upper(): str((entry or {}).get("transform_id") or "")
        for name, entry in ((check.get("annotation_validation") or {}).get(
            "per_view_transforms") or {}).items()
    }
    supplementary = audit_supplementary_views(
        document, check.get("supplementary_view_validation") or {}, transform_ids)
    passed = (
        not audit.errors and not audit.fixes
        and check.get("source_sha256") == step_hash
        and (check.get("readback") or {}).get("sha256") == dxf_hash
        and len(recorded_by_id) == len(recorded)
        and not missing_group_ids and not extra_group_ids
        and record_policy_ok and not forbidden_callout_handles
        and all(result["pass"] for result in results)
        and supplementary["pass"]
    )
    result = {
        "schema": "freecad-sheetmetal-drawing/feature-annotation-audit/v2",
        "path": str(args.dxf.resolve()), "sha256": dxf_hash,
        "step_path": str(args.step.resolve()), "step_sha256": step_hash,
        "status": "pass" if passed else "fail", "pass": passed,
        "independent_feature_count": feature_count,
        "independent_group_count": len(expected), "recorded_group_count": len(recorded),
        "missing_group_ids": missing_group_ids, "extra_group_ids": extra_group_ids,
        "annotation_policy": "no_automatic_grouped_callout",
        "record_policy_ok": record_policy_ok,
        "forbidden_callout_handles": forbidden_callout_handles,
        "supplementary_view_validation": supplementary,
        "audit_errors": len(audit.errors), "audit_fixes": len(audit.fixes),
        "groups": results,
    }
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
