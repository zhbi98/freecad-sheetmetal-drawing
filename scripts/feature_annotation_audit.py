#!/usr/bin/env python3
"""Independently compare STEP circular-hole groups with final-DXF callouts."""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import sys


AXIS_VIEW = {"X": "RIGHT", "Y": "FRONT", "Z": "TOP"}


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


def independent_groups(step: Path, App, Part):
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
            "target_view": AXIS_VIEW[axis],
            "label": f"{count}×⌀{fmt(diameter)} THRU",
        } for (axis, diameter), count in sorted(grouped.items())], len(holes)
    finally:
        App.closeDocument(document.Name)


def text_value(entity):
    return entity.text if entity.dxftype() == "MTEXT" else str(entity.dxf.text)


def main() -> int:
    args = parse_args()
    for path, label in ((args.step, "STEP"), (args.dxf, "DXF"), (args.check, "check")):
        if not path.is_file():
            raise SystemExit(f"{label} does not exist: {path}")
    ezdxf, App, Part = load_dependencies(args.vendor_path)
    expected, feature_count = independent_groups(args.step, App, Part)
    expected_by_id = {entry["group_id"]: entry for entry in expected}
    check = json.loads(args.check.read_text(encoding="utf-8"))
    recorded = (check.get("feature_annotation_validation") or {}).get("per_group") or []
    recorded_by_id = {str(entry.get("group_id") or ""): entry for entry in recorded}
    document = ezdxf.readfile(args.dxf)
    audit = document.audit()
    modelspace = document.modelspace()
    actual_text_handles = {str(entity.dxf.handle) for entity in modelspace
                           if entity.dxf.layer == "FEATURE_CALLOUT"
                           and entity.dxftype() in {"TEXT", "MTEXT"}}
    actual_leader_handles = {str(entity.dxf.handle) for entity in modelspace
                             if entity.dxf.layer == "FEATURE_CALLOUT"
                             and entity.dxftype() in {"LEADER", "MULTILEADER"}}
    expected_text_handles, expected_leader_handles, results = [], [], []
    for group_id, expected_group in expected_by_id.items():
        record = recorded_by_id.get(group_id, {})
        text_handle = str(record.get("text_handle") or "")
        leader_handle = str(record.get("leader_handle") or "")
        text_entity = document.entitydb.get(text_handle) if text_handle else None
        leader_entity = document.entitydb.get(leader_handle) if leader_handle else None
        text_ok = (text_entity is not None and text_entity.dxf.layer == "FEATURE_CALLOUT"
                   and text_entity.dxftype() in {"TEXT", "MTEXT"}
                   and text_value(text_entity) == expected_group["label"])
        leader_ok = (leader_entity is not None and leader_entity.dxf.layer == "FEATURE_CALLOUT"
                     and leader_entity.dxftype() in {"LEADER", "MULTILEADER"})
        record_ok = all(record.get(key) == expected_group[key]
                        for key in ("axis", "diameter", "count", "target_view", "label"))
        if text_handle:
            expected_text_handles.append(text_handle)
        if leader_handle:
            expected_leader_handles.append(leader_handle)
        results.append({"group_id": group_id, "expected": expected_group,
                        "record": record, "text_ok": text_ok,
                        "leader_ok": leader_ok, "record_ok": record_ok,
                        "pass": text_ok and leader_ok and record_ok})
    missing_group_ids = sorted(set(expected_by_id) - set(recorded_by_id))
    extra_group_ids = sorted(set(recorded_by_id) - set(expected_by_id))
    missing_text_handles = sorted(set(expected_text_handles) - actual_text_handles)
    extra_text_handles = sorted(actual_text_handles - set(expected_text_handles))
    missing_leader_handles = sorted(set(expected_leader_handles) - actual_leader_handles)
    extra_leader_handles = sorted(actual_leader_handles - set(expected_leader_handles))
    step_hash, dxf_hash = sha256(args.step), sha256(args.dxf)
    passed = (
        not audit.errors and not audit.fixes
        and check.get("source_sha256") == step_hash
        and (check.get("readback") or {}).get("sha256") == dxf_hash
        and len(recorded_by_id) == len(recorded)
        and not missing_group_ids and not extra_group_ids
        and not missing_text_handles and not extra_text_handles
        and not missing_leader_handles and not extra_leader_handles
        and feature_count > 0 and all(result["pass"] for result in results)
    )
    result = {
        "schema": "freecad-sheetmetal-drawing/feature-annotation-audit/v1",
        "path": str(args.dxf.resolve()), "sha256": dxf_hash,
        "step_path": str(args.step.resolve()), "step_sha256": step_hash,
        "status": "pass" if passed else "fail", "pass": passed,
        "independent_feature_count": feature_count,
        "independent_group_count": len(expected), "recorded_group_count": len(recorded),
        "missing_group_ids": missing_group_ids, "extra_group_ids": extra_group_ids,
        "missing_text_handles": missing_text_handles, "extra_text_handles": extra_text_handles,
        "missing_leader_handles": missing_leader_handles, "extra_leader_handles": extra_leader_handles,
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
