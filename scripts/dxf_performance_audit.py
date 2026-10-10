from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import sys
import time


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit a final editable DXF for structural and fragmentation risks."
    )
    parser.add_argument("dxf", type=Path)
    parser.add_argument(
        "--vendor-path",
        type=Path,
        help="Directory containing ezdxf and its compatible dependencies.",
    )
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--max-entities", type=int)
    parser.add_argument("--max-polyline-vertices", type=int)
    parser.add_argument("--max-file-size-mb", type=float)
    parser.add_argument("--max-read-seconds", type=float)
    parser.add_argument("--raw-entities", type=int)
    parser.add_argument("--raw-vertices", type=int)
    return parser.parse_args()


def load_ezdxf(vendor_path: Path | None):
    candidates = []
    if vendor_path is not None:
        candidates.append(vendor_path)
    candidates.extend((Path.cwd() / ".cadlib", Path.cwd() / "vendor"))
    for candidate in candidates:
        if candidate.is_dir():
            sys.path.insert(0, str(candidate.resolve()))
            break
    try:
        import ezdxf
    except ImportError as exc:
        raise SystemExit(
            "ezdxf is required; run in the drawing environment or pass --vendor-path."
        ) from exc
    return ezdxf


def point_key(point: tuple[float, float]) -> tuple[float, float]:
    return round(point[0], 4), round(point[1], 4)


def canonical_polyline(points: list[tuple[float, float]], closed: bool) -> tuple:
    values = tuple(point_key(point) for point in points)
    if closed and len(values) > 1 and values[0] == values[-1]:
        values = values[:-1]
    if not values:
        return closed, values
    if not closed:
        return closed, min(values, tuple(reversed(values)))
    rotations = []
    reverse = tuple(reversed(values))
    for sequence in (values, reverse):
        rotations.extend(sequence[index:] + sequence[:index] for index in range(len(sequence)))
    return closed, min(rotations)


def finite_xy(x: float, y: float) -> bool:
    return math.isfinite(x) and math.isfinite(y)


def canonical_entity(entity) -> tuple | None:
    entity_type = entity.dxftype()
    if entity_type == "LINE":
        start = point_key((float(entity.dxf.start.x), float(entity.dxf.start.y)))
        end = point_key((float(entity.dxf.end.x), float(entity.dxf.end.y)))
        return entity_type, tuple(sorted((start, end)))
    if entity_type == "CIRCLE":
        center = point_key((float(entity.dxf.center.x), float(entity.dxf.center.y)))
        return entity_type, center, round(float(entity.dxf.radius), 4)
    if entity_type == "ARC":
        center = point_key((float(entity.dxf.center.x), float(entity.dxf.center.y)))
        return (
            entity_type,
            center,
            round(float(entity.dxf.radius), 4),
            round(float(entity.dxf.start_angle) % 360.0, 4),
            round(float(entity.dxf.end_angle) % 360.0, 4),
        )
    if entity_type == "LWPOLYLINE":
        points = [(float(x), float(y)) for x, y in entity.get_points("xy")]
        return entity_type, canonical_polyline(points, bool(entity.closed))
    return None


def audit_file(path: Path, ezdxf, args: argparse.Namespace) -> dict:
    started = time.perf_counter()
    doc = ezdxf.readfile(path)
    read_seconds = time.perf_counter() - started
    audit = doc.audit()
    entities = list(doc.modelspace())
    types = Counter(entity.dxftype() for entity in entities)
    layers = Counter(entity.dxf.layer for entity in entities)
    layer_types: dict[str, Counter] = defaultdict(Counter)
    polyline_vertices = 0
    polyline_segments = 0
    line_segments = 0
    tiny_segments = Counter()
    duplicate_keys = Counter()
    visibility_keys: dict[str, set[tuple]] = {"VISIBLE": set(), "HIDDEN": set()}
    nonfinite_points = 0
    min_x = min_y = math.inf
    max_x = max_y = -math.inf

    def include_point(x: float, y: float) -> None:
        nonlocal min_x, min_y, max_x, max_y, nonfinite_points
        if not finite_xy(x, y):
            nonfinite_points += 1
            return
        min_x = min(min_x, x)
        min_y = min(min_y, y)
        max_x = max(max_x, x)
        max_y = max(max_y, y)

    def include_segment(a: tuple[float, float], b: tuple[float, float]) -> None:
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        for limit, label in (
            (1e-6, "lt_0.000001"),
            (1e-4, "lt_0.0001"),
            (1e-3, "lt_0.001"),
            (1e-2, "lt_0.01"),
            (5e-2, "lt_0.05"),
            (1e-1, "lt_0.1"),
        ):
            if length < limit:
                tiny_segments[label] += 1

    for entity in entities:
        entity_type = entity.dxftype()
        layer = entity.dxf.layer
        layer_types[layer][entity_type] += 1
        if layer in visibility_keys:
            geometry_key = canonical_entity(entity)
            if geometry_key is not None:
                visibility_keys[layer].add(geometry_key)
        if entity_type == "LWPOLYLINE":
            points = [(float(x), float(y)) for x, y in entity.get_points("xy")]
            polyline_vertices += len(points)
            duplicate_keys[(layer, canonical_polyline(points, bool(entity.closed)))] += 1
            for point in points:
                include_point(*point)
            pairs = list(zip(points, points[1:]))
            if entity.closed and len(points) > 1:
                pairs.append((points[-1], points[0]))
            polyline_segments += len(pairs)
            for a, b in pairs:
                include_segment(a, b)
        elif entity_type == "LINE":
            start = (float(entity.dxf.start.x), float(entity.dxf.start.y))
            end = (float(entity.dxf.end.x), float(entity.dxf.end.y))
            include_point(*start)
            include_point(*end)
            include_segment(start, end)
            line_segments += 1
        elif entity_type in {"CIRCLE", "ARC"}:
            center = entity.dxf.center
            radius = float(entity.dxf.radius)
            include_point(float(center.x) - radius, float(center.y) - radius)
            include_point(float(center.x) + radius, float(center.y) + radius)
        elif entity_type in {"TEXT", "MTEXT", "INSERT"}:
            point = entity.dxf.insert
            include_point(float(point.x), float(point.y))

    duplicates = sum(count - 1 for count in duplicate_keys.values() if count > 1)
    visible_hidden_overlap = visibility_keys["VISIBLE"] & visibility_keys["HIDDEN"]
    segment_total = polyline_segments + line_segments
    file_size = path.stat().st_size
    global_ltscale = float(doc.header.get("$LTSCALE", 1.0))
    try:
        visible_linetype = str(doc.layers.get("VISIBLE").dxf.linetype).upper()
    except Exception:
        visible_linetype = ""
    try:
        hidden_linetype = str(doc.layers.get("HIDDEN").dxf.linetype).upper()
    except Exception:
        hidden_linetype = ""
    def layer_lineweight(name: str) -> int | None:
        try:
            return int(doc.layers.get(name).dxf.lineweight)
        except Exception:
            return None

    border_lineweight = layer_lineweight("BORDER")
    visible_lineweight = layer_lineweight("VISIBLE")
    hidden_lineweight = layer_lineweight("HIDDEN")
    center_lineweight = layer_lineweight("CENTER")
    dimension_lineweight = layer_lineweight("DIMENSION")
    lineweight_ok = (
        None not in (border_lineweight, visible_lineweight, hidden_lineweight, center_lineweight)
        and border_lineweight >= visible_lineweight > hidden_lineweight >= center_lineweight > 0
    )
    hidden_pattern = []
    try:
        hidden_pattern = list(doc.linetypes.get(hidden_linetype).pattern_tags.compile())
    except Exception:
        pass
    linetype_ok = (
        math.isfinite(global_ltscale)
        and global_ltscale > 0.0
        and visible_linetype == "CONTINUOUS"
        and hidden_linetype not in {"", "CONTINUOUS", "BYLAYER", "BYBLOCK"}
        and len(hidden_pattern) >= 2
        and lineweight_ok
    )
    hard_failures = []
    if audit.errors:
        hard_failures.append("DXF audit contains errors")
    if audit.fixes:
        hard_failures.append("DXF readback required automatic fixes")
    if nonfinite_points:
        hard_failures.append("DXF contains non-finite coordinates")
    if duplicates:
        hard_failures.append("DXF contains exact duplicate polylines on the same layer")
    if visible_hidden_overlap:
        hard_failures.append("DXF contains coincident VISIBLE and HIDDEN geometry")
    if not linetype_ok:
        hard_failures.append("projection layer linetype or lineweight hierarchy is invalid")
    if args.max_entities is not None and len(entities) > args.max_entities:
        hard_failures.append("entity count exceeds the supplied limit")
    if (
        args.max_polyline_vertices is not None
        and polyline_vertices > args.max_polyline_vertices
    ):
        hard_failures.append("polyline vertex count exceeds the supplied limit")
    if args.max_file_size_mb is not None and file_size > args.max_file_size_mb * 1024 * 1024:
        hard_failures.append("file size exceeds the supplied limit")
    if args.max_read_seconds is not None and read_seconds > args.max_read_seconds:
        hard_failures.append("DXF read time exceeds the supplied limit")
    if args.raw_entities is not None and len(entities) > args.raw_entities:
        hard_failures.append("optimized entity count is greater than the recorded raw count")
    if args.raw_vertices is not None and polyline_vertices > args.raw_vertices:
        hard_failures.append("optimized polyline vertex count is greater than the recorded raw count")

    observations = []
    short_ratio = tiny_segments["lt_0.1"] / segment_total if segment_total else 0.0
    if short_ratio > 0.25:
        observations.append("more than 25% of line/polyline segments are shorter than 0.1 mm")
    if not any(
        value is not None
        for value in (
            args.max_entities,
            args.max_polyline_vertices,
            args.max_file_size_mb,
            args.max_read_seconds,
        )
    ):
        observations.append("no project-specific performance limits were supplied")
    observations.append("target CAD reader interaction test is still required")

    def entry(item) -> dict:
        return {
            "code": str(getattr(item, "code", "")),
            "message": str(getattr(item, "message", "")),
            "data": str(getattr(item, "data", "")),
        }

    result = {
        "path": str(path.resolve()),
        "status": "fail" if hard_failures else "structural_pass_target_reader_pending",
        "hard_failures": hard_failures,
        "observations": observations,
        "read_seconds": read_seconds,
        "file_size_bytes": file_size,
        "dxf_version": doc.dxfversion,
        "audit_errors": [entry(item) for item in audit.errors],
        "audit_fixes": [entry(item) for item in audit.fixes],
        "modelspace_entities": len(entities),
        "entity_types": dict(types),
        "layer_entities": dict(layers),
        "layer_entity_types": {layer: dict(value) for layer, value in layer_types.items()},
        "polyline_vertices": polyline_vertices,
        "polyline_segments": polyline_segments,
        "line_segments": line_segments,
        "tiny_segments": dict(tiny_segments),
        "short_segment_ratio_lt_0.1": short_ratio,
        "duplicate_polylines_same_layer": duplicates,
        "visible_hidden_exact_overlap": len(visible_hidden_overlap),
        "linetype_validation": {
            "pass": linetype_ok,
            "global_ltscale": global_ltscale,
            "visible_linetype": visible_linetype,
            "hidden_linetype": hidden_linetype,
            "hidden_pattern": hidden_pattern,
            "lineweights_0_01_mm": {
                "BORDER": border_lineweight,
                "VISIBLE": visible_lineweight,
                "HIDDEN": hidden_lineweight,
                "CENTER": center_lineweight,
                "DIMENSION": dimension_lineweight,
            },
            "lineweight_hierarchy_pass": lineweight_ok,
        },
        "nonfinite_points": nonfinite_points,
        "coordinate_bounds": [min_x, min_y, max_x, max_y],
        "raw_comparison": {
            "raw_entities": args.raw_entities,
            "raw_vertices": args.raw_vertices,
            "entity_reduction_ratio": (
                1.0 - len(entities) / args.raw_entities
                if args.raw_entities
                else None
            ),
            "vertex_reduction_ratio": (
                1.0 - polyline_vertices / args.raw_vertices
                if args.raw_vertices
                else None
            ),
        },
    }
    return result


def main() -> int:
    args = parse_args()
    path = args.dxf.resolve()
    if not path.is_file():
        raise SystemExit(f"DXF does not exist: {path}")
    ezdxf = load_ezdxf(args.vendor_path)
    result = audit_file(path, ezdxf, args)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    print(payload)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(payload + "\n", encoding="utf-8")
    return 2 if result["hard_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
