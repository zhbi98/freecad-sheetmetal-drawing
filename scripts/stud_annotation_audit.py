#!/usr/bin/env python3
"""Independently rebuild STEP stud geometry and compare it with the final DXF."""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any


EXPECTED_VIEWS = {"TOP", "BOTTOM", "FRONT", "REAR", "LEFT", "RIGHT"}
VIEW_DIRECTIONS = {
    "FRONT": (0.0, -1.0, 0.0), "REAR": (0.0, 1.0, 0.0),
    "RIGHT": (1.0, 0.0, 0.0), "LEFT": (-1.0, 0.0, 0.0),
    "TOP": (0.0, 0.0, 1.0), "BOTTOM": (0.0, 0.0, -1.0),
}
ENABLED_HLR_GROUPS = {0, 3, 5, 8}
ANCHOR_TYPES = {"POINT", "CIRCLE", "INSERT"}
TAG_TYPES = {"TEXT", "MTEXT", "ATTRIB"}
DIMENSION_TOLERANCE_MM = 0.03
GEOMETRY_TOLERANCE_MM = 0.03


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf", type=Path)
    parser.add_argument("--step", required=True, type=Path,
                        help="Original STEP/STP source; reopened for independent validation.")
    parser.add_argument("--check", required=True, type=Path,
                        help="Engineering check JSON containing handles and layout metadata.")
    parser.add_argument("--vendor-path", type=Path)
    parser.add_argument("--json-out", type=Path)
    return parser.parse_args()


def load_dependencies(vendor_path: Path | None):
    candidates = [vendor_path] if vendor_path is not None else []
    candidates.extend((Path.cwd() / ".cadlib", Path.cwd() / "vendor"))
    for candidate in candidates:
        if candidate.is_dir():
            sys.path.insert(0, str(candidate.resolve()))
            break
    try:
        import ezdxf
        from ezdxf import bbox as ezbbox
    except ImportError as exc:
        raise SystemExit("ezdxf is required; pass --vendor-path if necessary.") from exc
    try:
        import FreeCAD as App
        import Part
        import TechDraw
    except ImportError as exc:
        raise SystemExit("Run this audit with FreeCAD's Python.") from exc
    return ezdxf, ezbbox, App, Part, TechDraw


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def finite_box(value: Any) -> bool:
    return (isinstance(value, (list, tuple)) and len(value) == 4
            and all(isinstance(v, (int, float)) and math.isfinite(float(v)) for v in value)
            and float(value[0]) <= float(value[2]) and float(value[1]) <= float(value[3]))


def distance3(a: Any, b: Any) -> float | None:
    if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))
            and len(a) == len(b) == 3):
        return None
    try:
        return math.sqrt(sum((float(a[i]) - float(b[i])) ** 2 for i in range(3)))
    except (TypeError, ValueError):
        return None


def side_from_vector(vector) -> str:
    values = (float(vector.x), float(vector.y), float(vector.z))
    axis = max(range(3), key=lambda i: abs(values[i]))
    names = (("RIGHT", "LEFT"), ("REAR", "FRONT"), ("TOP", "BOTTOM"))
    return names[axis][0 if values[axis] > 0 else 1]


def canonical_axis(App, vector):
    result = App.Vector(vector)
    result.normalize()
    values = (result.x, result.y, result.z)
    dominant = max(range(3), key=lambda i: abs(values[i]))
    return -result if values[dominant] < 0 else result


def point_on_axis_at(origin, axis, coordinate: float):
    return origin + axis * (coordinate - origin.dot(axis))


def cylinder_surfaces(shape):
    return [face.Surface for face in shape.Faces
            if type(face.Surface).__name__ == "Cylinder"
            and 0.5 < float(face.Surface.Radius) < 10.0]


def rebuild_step_evidence(step_path: Path, App, Part):
    """Reopen STEP and derive stud evidence without consuming generator decisions."""
    document = App.newDocument("independent_step_stud_audit")
    try:
        Part.insert(str(step_path.resolve()), document.Name)
        document.recompute()
        objects = [obj for obj in document.Objects
                   if hasattr(obj, "Shape") and not obj.Shape.isNull()]
        sheets = [obj for obj in objects
                  if max(obj.Shape.BoundBox.XLength, obj.Shape.BoundBox.YLength,
                         obj.Shape.BoundBox.ZLength) > 50.0]
        candidates = [obj for obj in objects if obj not in sheets]
        if not sheets:
            raise RuntimeError("No sheet body could be independently identified in STEP")
        sheet_cylinders = []
        for sheet in sheets:
            for face_index, face in enumerate(sheet.Shape.Faces, 1):
                if type(face.Surface).__name__ == "Cylinder":
                    sheet_cylinders.append((sheet.Name, face_index, face.Surface, face))

        confirmed, rejected = [], []
        for obj in candidates:
            groups = defaultdict(list)
            for surface in cylinder_surfaces(obj.Shape):
                axis = canonical_axis(App, surface.Axis)
                center = App.Vector(surface.Center)
                offset = center - axis * center.dot(axis)
                key = (round(axis.x, 5), round(axis.y, 5), round(axis.z, 5),
                       round(offset.x, 3), round(offset.y, 3), round(offset.z, 3))
                groups[key].append(surface)
            if not groups:
                rejected.append({"source": obj.Name, "reason": "no_coaxial_cylindrical_feature"})
                continue
            _, coaxial = max(groups.items(), key=lambda entry: len(entry[1]))
            radii = sorted({round(float(surface.Radius), 5) for surface in coaxial})
            if len(coaxial) < 2 or len(radii) < 2:
                rejected.append({"source": obj.Name, "reason": "outer_and_inner_cylinders_not_confirmed"})
                continue
            axis = canonical_axis(App, coaxial[0].Axis)
            origin = App.Vector(coaxial[0].Center)
            coordinates = [vertex.Point.dot(axis) for vertex in obj.Shape.Vertexes]
            if not coordinates:
                rejected.append({"source": obj.Name, "reason": "empty_fastener_vertices"})
                continue
            body_min, body_max = min(coordinates), max(coordinates)
            matches = []
            for sheet_name, face_index, surface, face in sheet_cylinders:
                sheet_axis = canonical_axis(App, surface.Axis)
                if abs(abs(sheet_axis.dot(axis)) - 1.0) >= 1e-5:
                    continue
                delta = App.Vector(surface.Center) - origin
                radial_error = (delta - axis * delta.dot(axis)).Length
                if radial_error > 0.08:
                    continue
                face_coordinates = [vertex.Point.dot(axis) for vertex in face.Vertexes]
                if not face_coordinates:
                    continue
                sheet_min, sheet_max = min(face_coordinates), max(face_coordinates)
                overlap = max(0.0, min(body_max, sheet_max) - max(body_min, sheet_min))
                gap = max(sheet_min - body_max, body_min - sheet_max, 0.0)
                if overlap > 0.02 or gap <= 0.08:
                    matches.append({"sheet": sheet_name, "face": face_index,
                                    "radius": float(surface.Radius), "radial_error": radial_error,
                                    "sheet_min": sheet_min, "sheet_max": sheet_max,
                                    "overlap": overlap, "gap": gap})
            if not matches:
                rejected.append({"source": obj.Name, "reason": "no_coaxial_sheet_installation_hole"})
                continue
            matches.sort(key=lambda match: (-match["overlap"], match["gap"],
                                             match["radial_error"], match["sheet"], match["face"]))
            match = matches[0]
            sheet_min, sheet_max = match["sheet_min"], match["sheet_max"]
            midplane = (sheet_min + sheet_max) / 2.0
            low_protrusion = max(0.0, sheet_min - body_min)
            high_protrusion = max(0.0, body_max - sheet_max)
            if max(low_protrusion, high_protrusion) <= 0.05 or abs(low_protrusion - high_protrusion) <= 0.05:
                rejected.append({"source": obj.Name, "reason": "ambiguous_body_side"})
                continue
            body_direction = -axis if low_protrusion > high_protrusion else axis
            anchor = point_on_axis_at(origin, axis, midplane)
            confirmed.append({
                "source": obj.Name, "product_name": obj.Label,
                "axis_origin": [origin.x, origin.y, origin.z],
                "axis_direction": [axis.x, axis.y, axis.z],
                "anchor_3d": [anchor.x, anchor.y, anchor.z],
                "outer": round(2.0 * max(float(s.Radius) for s in coaxial), 2),
                "inner": round(2.0 * min(float(s.Radius) for s in coaxial), 2),
                "height": round(body_max - body_min, 3),
                "side": side_from_vector(body_direction),
                "body_side_vector": [body_direction.x, body_direction.y, body_direction.z],
                "body_axial_range": [body_min, body_max],
                "low_side_protrusion_mm": low_protrusion,
                "high_side_protrusion_mm": high_protrusion,
                "installation_hole_id": f"{match['sheet']}.Face{match['face']}",
                "installation_hole_radius": match["radius"],
                "installation_hole_radial_error_mm": match["radial_error"],
                "installation_hole_axial_overlap_mm": match["overlap"],
                "installation_hole_axial_gap_mm": match["gap"],
                "installation_hole_axial_range": [sheet_min, sheet_max],
                "installation_midplane_coordinate": midplane,
            })

        simple_shapes = [sheet.Shape.removeSplitter() for sheet in sheets]
        for item in confirmed:
            axis = App.Vector(*item["axis_direction"])
            origin = App.Vector(*item["axis_origin"])
            base = point_on_axis_at(origin, axis, item["body_axial_range"][0])
            outer = Part.makeCylinder(item["outer"] / 2.0, item["height"], base, axis)
            inner = Part.makeCylinder(item["inner"] / 2.0, item["height"], base, axis)
            simple_shapes.append(outer.cut(inner))
        return {"objects": len(objects), "sheets": [obj.Name for obj in sheets],
                "candidate_count": len(candidates), "confirmed": confirmed,
                "rejected": rejected, "compound": Part.makeCompound(simple_shapes)}
    except Exception:
        App.closeDocument(document.Name)
        raise


def project_point(Part, TechDraw, App, view: str, point: list[float]):
    groups = TechDraw.projectEx(Part.makeSphere(0.05, App.Vector(*point)),
                                App.Vector(*VIEW_DIRECTIONS[view]))
    centers = [(float(edge.Curve.Center.x), float(edge.Curve.Center.y))
               for group in groups for edge in group.Edges
               if type(edge.Curve).__name__ == "Circle"]
    if not centers:
        raise RuntimeError(f"Cannot independently project marker in {view}: {point}")
    return (sum(p[0] for p in centers) / len(centers),
            sum(p[1] for p in centers) / len(centers))


def project_bbox(TechDraw, App, shape, view: str):
    groups = TechDraw.projectEx(shape, App.Vector(*VIEW_DIRECTIONS[view]))
    points = []
    for group_index in ENABLED_HLR_GROUPS:
        for edge in groups[group_index].Edges:
            points.extend((float(v.Point.x), float(v.Point.y)) for v in edge.Vertexes)
            if not edge.Vertexes:
                points.extend((float(p.x), float(p.y)) for p in edge.discretize(Deflection=0.16))
    if not points:
        raise RuntimeError(f"No independent projected geometry in {view}")
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def entity_position(entity):
    kind = entity.dxftype()
    point = (entity.dxf.location if kind == "POINT" else entity.dxf.center if kind == "CIRCLE"
             else entity.dxf.insert if kind == "INSERT" else None)
    if point is None:
        return None
    x, y = float(point.x), float(point.y)
    return (x, y) if math.isfinite(x) and math.isfinite(y) else None


def entity_text(entity):
    return entity.text if entity.dxftype() == "MTEXT" else (
        str(entity.dxf.text) if entity.dxftype() in {"TEXT", "ATTRIB"} else None)


def text_position(entity):
    if entity.dxftype() not in TAG_TYPES:
        return None
    point = entity.dxf.insert
    return (float(point.x), float(point.y))


def dxf_view_bbox(modelspace, ezbbox, recorded_box):
    if not finite_box(recorded_box):
        return None
    selected = []
    for entity in modelspace:
        if entity.dxf.layer not in {"VISIBLE", "HIDDEN"}:
            continue
        try:
            extent = ezbbox.extents([entity], fast=True)
        except Exception:
            continue
        if not extent.has_data:
            continue
        cx = (float(extent.extmin.x) + float(extent.extmax.x)) / 2.0
        cy = (float(extent.extmin.y) + float(extent.extmax.y)) / 2.0
        if (float(recorded_box[0]) - 1.0 <= cx <= float(recorded_box[2]) + 1.0
                and float(recorded_box[1]) - 1.0 <= cy <= float(recorded_box[3]) + 1.0):
            selected.append(entity)
    if not selected:
        return None
    extent = ezbbox.extents(selected, fast=True)
    return ([float(extent.extmin.x), float(extent.extmin.y),
             float(extent.extmax.x), float(extent.extmax.y)] if extent.has_data else None)


def max_box_error(a, b):
    return max(abs(float(a[i]) - float(b[i])) for i in range(4)) if finite_box(a) and finite_box(b) else None


def close_number(a: Any, b: Any, tolerance: float) -> bool:
    try:
        return math.isfinite(float(a)) and math.isfinite(float(b)) and abs(float(a) - float(b)) <= tolerance
    except (TypeError, ValueError):
        return False


def main() -> int:
    args = parse_args()
    for path, label in ((args.dxf, "DXF"), (args.step, "STEP/STP"), (args.check, "check record")):
        if not path.is_file():
            raise SystemExit(f"{label} does not exist: {path}")
    check = json.loads(args.check.read_text(encoding="utf-8"))
    validation = check.get("annotation_validation") or {}
    records = validation.get("per_item") or []
    bounds = validation.get("view_bounds_sheet_mm") or {}
    anchor_layer = str(validation.get("anchor_layer") or "STUD_ANCHOR")
    tolerance = validation.get("position_tolerance_mm")
    tolerance_ok = isinstance(tolerance, (int, float)) and 0 < float(tolerance) <= 1.0
    tolerance_value = float(tolerance) if tolerance_ok else math.nan
    ezdxf, ezbbox, App, Part, TechDraw = load_dependencies(args.vendor_path)
    step_hash = sha256(args.step)
    recorded_step_hash = check.get("source_sha256")
    source_hash_pass = isinstance(recorded_step_hash, str) and recorded_step_hash == step_hash
    step_evidence = rebuild_step_evidence(args.step, App, Part)
    try:
        independent_items = step_evidence["confirmed"]
        independent_by_source = {item["source"]: item for item in independent_items}
        record_by_source = {str(item.get("source") or ""): item for item in records}
        independent_sources, recorded_sources = set(independent_by_source), {s for s in record_by_source if s}
        missing_step_sources = sorted(recorded_sources - independent_sources)
        extra_step_sources = sorted(independent_sources - recorded_sources)
        independent_local_bounds = {view: project_bbox(TechDraw, App, step_evidence["compound"], view)
                                    for view in EXPECTED_VIEWS}
        doc = ezdxf.readfile(args.dxf)
        dxf_audit = doc.audit()
        modelspace = doc.modelspace()
        dxf_hash = sha256(args.dxf)
        recorded_dxf_hash = (check.get("readback") or {}).get("sha256")
        actual_view_bounds = {view: dxf_view_bbox(modelspace, ezbbox, bounds.get(view))
                              for view in EXPECTED_VIEWS}
        view_mapping = {}
        for view in EXPECTED_VIEWS:
            local_box, actual_box, record_box = independent_local_bounds[view], actual_view_bounds[view], bounds.get(view)
            wl, hl = local_box[2] - local_box[0], local_box[3] - local_box[1]
            # Use the recorded view frame only after independently checking it
            # against final-DXF geometry.  Analytic ARC extents may exceed the
            # generator's sampled HLR envelope by a small chord tolerance.
            wa = float(record_box[2]) - float(record_box[0]) if finite_box(record_box) else math.nan
            ha = float(record_box[3]) - float(record_box[1]) if finite_box(record_box) else math.nan
            sx, sy = wa / wl if wl > 1e-9 else math.nan, ha / hl if hl > 1e-9 else math.nan
            scale, box_error = (sx + sy) / 2.0, max_box_error(actual_box, record_box)
            view_mapping[view] = {
                "independent_local_bbox": local_box, "actual_dxf_geometry_bbox": actual_box,
                "recorded_view_bbox": record_box, "recorded_bbox_error_mm": box_error,
                "scale_x": sx, "scale_y": sy, "scale": scale,
                "pass": (math.isfinite(sx) and math.isfinite(sy) and abs(sx - sy) <= 0.002
                         and box_error is not None and box_error <= 0.25),
            }

        all_anchor_handles = {str(e.dxf.handle) for e in modelspace
                              if e.dxf.layer == anchor_layer and e.dxftype() in ANCHOR_TYPES}
        all_tag_handles = {str(e.dxf.handle) for e in modelspace
                           if e.dxf.layer == "STUD" and e.dxftype() in TAG_TYPES}
        all_outline_handles = {str(e.dxf.handle) for e in modelspace
                               if e.dxf.layer == "STUD_OUTLINE" and e.dxftype() == "CIRCLE"}
        expected_anchor_handles, expected_tag_handles, expected_outline_handles, results = [], [], [], []
        fasteners_by_source = {item.get("source"): item for item in (check.get("fasteners") or [])}
        for record in records:
            source, tag = str(record.get("source") or ""), str(record.get("tag") or "")
            independent = independent_by_source.get(source)
            independent_view = independent.get("side") if independent else None
            anchor_handle, tag_handle = str(record.get("anchor_entity_handle") or ""), str(record.get("tag_text_handle") or "")
            outline_handles = [str(handle) for handle in (record.get("outline_entity_handles") or [])]
            anchor_entity = doc.entitydb.get(anchor_handle) if anchor_handle else None
            tag_entity = doc.entitydb.get(tag_handle) if tag_handle else None
            actual_anchor = entity_position(anchor_entity) if anchor_entity is not None else None
            actual_tag = text_position(tag_entity) if tag_entity is not None else None
            expected_anchor, projection_error, inside, tag_inside = None, None, False, False
            if independent is not None and independent_view in EXPECTED_VIEWS:
                mapping = view_mapping[independent_view]
                local_anchor = project_point(Part, TechDraw, App, independent_view, independent["anchor_3d"])
                local_box, actual_box = mapping["independent_local_bbox"], mapping["actual_dxf_geometry_bbox"]
                paper_box = mapping["recorded_view_bbox"]
                if mapping["pass"] and finite_box(actual_box) and finite_box(paper_box):
                    expected_anchor = [
                        (paper_box[0] + paper_box[2]) / 2.0 + (local_anchor[0] - (local_box[0] + local_box[2]) / 2.0) * mapping["scale"],
                        (paper_box[1] + paper_box[3]) / 2.0 + (local_anchor[1] - (local_box[1] + local_box[3]) / 2.0) * mapping["scale"],
                    ]
                    if actual_anchor is not None:
                        projection_error = math.hypot(actual_anchor[0] - expected_anchor[0], actual_anchor[1] - expected_anchor[1])
                    if tolerance_ok:
                        inside = actual_anchor is not None and paper_box[0] - tolerance_value <= actual_anchor[0] <= paper_box[2] + tolerance_value and paper_box[1] - tolerance_value <= actual_anchor[1] <= paper_box[3] + tolerance_value
                        tag_inside = actual_tag is not None and paper_box[0] - tolerance_value <= actual_tag[0] <= paper_box[2] + tolerance_value and paper_box[1] - tolerance_value <= actual_tag[1] <= paper_box[3] + tolerance_value
            recorded_fastener = fasteners_by_source.get(source, {})
            dimension_pass = independent is not None and all(close_number(recorded_fastener.get(key), independent.get(key), DIMENSION_TOLERANCE_MM)
                                                             for key in ("outer", "inner", "height"))
            anchor_3d_error = distance3(record.get("anchor_3d"), independent.get("anchor_3d")) if independent else None
            hole_range_error = None
            if independent is not None:
                rr, ir = record.get("installation_hole_axial_range"), independent.get("installation_hole_axial_range")
                if isinstance(rr, (list, tuple)) and len(rr) == 2 and isinstance(ir, (list, tuple)) and len(ir) == 2:
                    hole_range_error = max(abs(float(rr[i]) - float(ir[i])) for i in range(2))
            source_geometry_pass = (
                independent is not None and dimension_pass and record.get("target_view") == independent_view
                and anchor_3d_error is not None and anchor_3d_error <= GEOMETRY_TOLERANCE_MM
                and hole_range_error is not None and hole_range_error <= GEOMETRY_TOLERANCE_MM
                and close_number(record.get("installation_hole_axial_overlap_mm"), independent.get("installation_hole_axial_overlap_mm"), GEOMETRY_TOLERANCE_MM)
                and close_number(record.get("installation_hole_axial_gap_mm"), independent.get("installation_hole_axial_gap_mm"), GEOMETRY_TOLERANCE_MM)
                and close_number(record.get("installation_hole_radial_error_mm"), independent.get("installation_hole_radial_error_mm"), GEOMETRY_TOLERANCE_MM))
            anchor_ok = anchor_entity is not None and anchor_entity.dxftype() in ANCHOR_TYPES and anchor_entity.dxf.layer == anchor_layer and actual_anchor is not None
            tag_ok = tag_entity is not None and tag_entity.dxftype() in TAG_TYPES and tag_entity.dxf.layer == "STUD" and entity_text(tag_entity) == tag
            outline_entities = [doc.entitydb.get(handle) for handle in outline_handles]
            outline_color = record.get("outline_color")
            expected_radii = (sorted([float(independent["inner"]), float(independent["outer"])])
                              if independent is not None else [])
            actual_radii = []
            outline_geometry_ok = (independent is not None and independent_view in EXPECTED_VIEWS
                                   and len(outline_entities) == 2
                                   and all(entity is not None for entity in outline_entities))
            if outline_geometry_ok:
                for entity in outline_entities:
                    center = entity.dxf.center
                    actual_radii.append(float(entity.dxf.radius) / view_mapping[independent_view]["scale"] * 2.0)
                    outline_geometry_ok = outline_geometry_ok and (
                        entity.dxftype() == "CIRCLE" and entity.dxf.layer == "STUD_OUTLINE"
                        and int(entity.dxf.color) == int(outline_color)
                        and actual_anchor is not None
                        and math.hypot(float(center.x) - actual_anchor[0], float(center.y) - actual_anchor[1]) <= tolerance_value
                    )
                outline_geometry_ok = outline_geometry_ok and all(
                    abs(actual - expected) <= DIMENSION_TOLERANCE_MM
                    for actual, expected in zip(sorted(actual_radii), expected_radii)
                )
            passed = (tolerance_ok and source_geometry_pass and anchor_ok and tag_ok
                      and outline_geometry_ok
                      and independent_view in EXPECTED_VIEWS and view_mapping[independent_view]["pass"]
                      and projection_error is not None and projection_error <= tolerance_value and inside and tag_inside)
            if anchor_handle: expected_anchor_handles.append(anchor_handle)
            if tag_handle: expected_tag_handles.append(tag_handle)
            expected_outline_handles.extend(outline_handles)
            results.append({"source": source, "tag": tag,
                            "recorded_target_view": record.get("target_view"),
                            "independent_step_target_view": independent_view,
                            "independent_step_dimensions_mm": ({key: independent[key] for key in ("inner", "outer", "height")} if independent else None),
                            "dimension_pass": dimension_pass, "anchor_3d_error_mm": anchor_3d_error,
                            "installation_hole_range_error_mm": hole_range_error,
                            "independent_expected_sheet_position_mm": expected_anchor,
                            "actual_sheet_position_mm": list(actual_anchor) if actual_anchor is not None else None,
                            "actual_tag_position_mm": list(actual_tag) if actual_tag is not None else None,
                            "position_error_mm": projection_error,
                            "inside_independent_view_bounds": inside,
                            "tag_inside_independent_view_bounds": tag_inside,
                            "outline_handles": outline_handles,
                            "outline_color": outline_color,
                            "outline_actual_diameters_mm": sorted(actual_radii),
                            "outline_geometry_and_color_pass": outline_geometry_ok,
                            "source_geometry_pass": source_geometry_pass, "pass": passed})

        duplicate_anchor_handles = sorted({h for h in expected_anchor_handles if expected_anchor_handles.count(h) > 1})
        duplicate_tag_handles = sorted({h for h in expected_tag_handles if expected_tag_handles.count(h) > 1})
        missing_anchor_handles = sorted(set(expected_anchor_handles) - all_anchor_handles)
        extra_anchor_handles = sorted(all_anchor_handles - set(expected_anchor_handles))
        missing_tag_handles = sorted(set(expected_tag_handles) - all_tag_handles)
        extra_tag_handles = sorted(all_tag_handles - set(expected_tag_handles))
        duplicate_outline_handles = sorted({h for h in expected_outline_handles if expected_outline_handles.count(h) > 1})
        missing_outline_handles = sorted(set(expected_outline_handles) - all_outline_handles)
        extra_outline_handles = sorted(all_outline_handles - set(expected_outline_handles))
        step_geometry_pass = (source_hash_pass and not missing_step_sources and not extra_step_sources
                              and len(record_by_source) == len(records)
                              and all(mapping["pass"] for mapping in view_mapping.values())
                              and (not records or all(result["source_geometry_pass"] for result in results)))
        no_fasteners_ok = (not records and not independent_items and not all_anchor_handles
                           and not all_tag_handles and not all_outline_handles)
        result_pass = (not dxf_audit.errors and not dxf_audit.fixes and recorded_dxf_hash == dxf_hash
                       and tolerance_ok and step_geometry_pass and not duplicate_anchor_handles
                       and not duplicate_tag_handles and not missing_anchor_handles and not extra_anchor_handles
                       and not missing_tag_handles and not extra_tag_handles
                       and not duplicate_outline_handles and not missing_outline_handles and not extra_outline_handles
                       and (no_fasteners_ok or (bool(records) and all(result["pass"] for result in results))))
        result = {
            "schema": "freecad-sheetmetal-drawing/stud-annotation-audit/v2",
            "path": str(args.dxf.resolve()), "sha256": dxf_hash, "recorded_sha256": recorded_dxf_hash,
            "step_path": str(args.step.resolve()), "step_sha256": step_hash,
            "recorded_step_sha256": recorded_step_hash, "source_hash_pass": source_hash_pass,
            "status": "pass" if result_pass else "fail", "pass": result_pass,
            "step_geometry_pass": step_geometry_pass,
            "independent_step_object_count": step_evidence["objects"],
            "independent_sheet_objects": step_evidence["sheets"],
            "independent_candidate_count": step_evidence["candidate_count"],
            "independent_step_fastener_count": len(independent_items),
            "independent_rejected_candidates": step_evidence["rejected"],
            "recorded_fastener_count": len(records),
            "missing_step_sources": missing_step_sources, "extra_step_sources": extra_step_sources,
            "view_mapping": view_mapping, "anchor_layer": anchor_layer,
            "position_tolerance_mm": tolerance, "expected_items": len(records),
            "checked_items": len(results), "all_anchor_entities": len(all_anchor_handles),
            "all_tag_entities": len(all_tag_handles),
            "all_outline_entities": len(all_outline_handles),
            "missing_anchor_handles": missing_anchor_handles, "extra_anchor_handles": extra_anchor_handles,
            "missing_tag_handles": missing_tag_handles, "extra_tag_handles": extra_tag_handles,
            "duplicate_anchor_handles": duplicate_anchor_handles,
            "duplicate_tag_handles": duplicate_tag_handles,
            "missing_outline_handles": missing_outline_handles,
            "extra_outline_handles": extra_outline_handles,
            "duplicate_outline_handles": duplicate_outline_handles,
            "audit_errors": len(dxf_audit.errors), "audit_fixes": len(dxf_audit.fixes),
            "items": results,
        }
        payload = json.dumps(result, ensure_ascii=False, indent=2)
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(payload + "\n", encoding="utf-8")
        print(payload)
        return 0 if result_pass else 2
    finally:
        App.closeDocument("independent_step_stud_audit")


if __name__ == "__main__":
    raise SystemExit(main())
