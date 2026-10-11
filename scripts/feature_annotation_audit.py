#!/usr/bin/env python3
"""Compare STEP circular-hole inventory and reject automatic grouped callouts."""

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
        } for (axis, diameter), count in sorted(grouped.items())], len(holes)
    finally:
        App.closeDocument(document.Name)


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
    passed = (
        not audit.errors and not audit.fixes
        and check.get("source_sha256") == step_hash
        and (check.get("readback") or {}).get("sha256") == dxf_hash
        and len(recorded_by_id) == len(recorded)
        and not missing_group_ids and not extra_group_ids
        and record_policy_ok and not forbidden_callout_handles
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
        "annotation_policy": "no_automatic_grouped_callout",
        "record_policy_ok": record_policy_ok,
        "forbidden_callout_handles": forbidden_callout_handles,
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
