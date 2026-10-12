#!/usr/bin/env python3
"""Aggregate per-project drawing checks into a deterministic acceptance record."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any


EXPECTED_VIEWS = {"TOP", "BOTTOM", "FRONT", "REAR", "LEFT", "RIGHT"}
EXPECTED_AUTOMATIC_CODES = {f"A{i:02d}" for i in range(1, 12)}
VALID_REVIEW = ("pass", "fail", "pending")


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return data


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def calibrated_transform_ok(entry: Any, tolerance: float) -> bool:
    if not isinstance(entry, dict) or not entry.get("transform_id"):
        return False
    if entry.get("projection_source") != "TechDraw.projectEx calculation marker":
        return False
    matrix = entry.get("model_to_view_matrix")
    offset = entry.get("model_to_view_offset")
    points = entry.get("calibration_points")
    if not (
        isinstance(matrix, list) and len(matrix) == 2
        and all(isinstance(row, list) and len(row) == 3 for row in matrix)
        and isinstance(offset, list) and len(offset) == 2
        and isinstance(points, list) and len(points) >= 3
    ):
        return False
    projected = []
    for point in points:
        actual = point.get("projected_2d") if isinstance(point, dict) else None
        transformed = point.get("transform_2d") if isinstance(point, dict) else None
        residual = point.get("residual_mm") if isinstance(point, dict) else None
        if not (
            isinstance(actual, list) and len(actual) == 2
            and isinstance(transformed, list) and len(transformed) == 2
            and isinstance(residual, (int, float)) and math.isfinite(float(residual))
            and math.hypot(float(actual[0]) - float(transformed[0]),
                           float(actual[1]) - float(transformed[1])) <= tolerance
            and float(residual) <= tolerance
        ):
            return False
        projected.append((float(actual[0]), float(actual[1])))
    noncollinear = any(
        abs((projected[j][0] - projected[i][0]) * (projected[k][1] - projected[i][1])
            - (projected[j][1] - projected[i][1]) * (projected[k][0] - projected[i][0])) > 1e-6
        for i in range(len(projected)) for j in range(i + 1, len(projected))
        for k in range(j + 1, len(projected))
    )
    return (
        noncollinear and isinstance(entry.get("max_residual_mm"), (int, float))
        and 0 <= entry["max_residual_mm"] <= tolerance and entry.get("pass") is True
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", required=True, type=Path)
    parser.add_argument("--fit", required=True, type=Path)
    parser.add_argument("--performance", required=True, type=Path)
    parser.add_argument("--text-audit", required=True, type=Path)
    parser.add_argument("--stud-audit", required=True, type=Path)
    parser.add_argument("--feature-audit", required=True, type=Path)
    parser.add_argument("--preview", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--view-review", choices=VALID_REVIEW, default="pending")
    parser.add_argument("--drawing-review", choices=VALID_REVIEW, default="pending")
    parser.add_argument("--target-reader", choices=VALID_REVIEW, default="pending")
    parser.add_argument("--engineering-approval", choices=VALID_REVIEW, default="pending")
    parser.add_argument("--reviewer", default="")
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    check = load_json(args.check)
    fit = load_json(args.fit)
    perf = load_json(args.performance)
    text_audit = load_json(args.text_audit)
    stud_audit = load_json(args.stud_audit)
    feature_audit = load_json(args.feature_audit)
    dxf_path = Path(perf.get("path", ""))
    dxf_exists = dxf_path.is_file() and dxf_path.stat().st_size > 0
    current_dxf_sha256 = sha256(dxf_path) if dxf_exists else None
    recorded_dxf_sha256 = (check.get("readback") or {}).get("sha256")

    def same_dxf_path(value: Any) -> bool:
        if not value or not dxf_exists:
            return False
        try:
            return Path(str(value)).resolve() == dxf_path.resolve()
        except (OSError, RuntimeError, ValueError):
            return False

    performance_report_current = (
        dxf_exists and same_dxf_path(perf.get("path")) and args.performance.is_file()
        and args.performance.stat().st_mtime_ns >= dxf_path.stat().st_mtime_ns
    )
    preview_current = (
        dxf_exists and args.preview.is_file() and args.preview.stat().st_size > 0
        and args.preview.stat().st_mtime_ns >= dxf_path.stat().st_mtime_ns
    )
    items: list[dict[str, Any]] = []

    def add(code: str, name: str, gate: str, status: str, evidence: Any, action: str) -> None:
        items.append({
            "code": code,
            "name": name,
            "gate": gate,
            "status": status,
            "evidence": evidence,
            "action_if_not_pass": action,
        })

    source_sha256 = check.get("source_sha256")
    source_ok = (
        bool(check.get("source")) and check.get("source_preserved") is True
        and isinstance(source_sha256, str) and len(source_sha256) == 64
        and all(character in "0123456789abcdefABCDEF" for character in source_sha256)
    )
    add("A01", "源模型标识与保留", "automatic", "pass" if source_ok else "fail",
        {"source": check.get("source"), "source_sha256": source_sha256,
         "source_preserved": check.get("source_preserved")},
        "恢复当前 STEP 的可追溯来源并从源模型重新生成。")

    front = check.get("front_source")
    basis = check.get("drawing_basis") or {}
    view_basis_ok = (
        front in EXPECTED_VIEWS
        and check.get("front_source_basis") in {"user_selected", "default_model_front"}
        and bool(check.get("camera_method"))
        and set(basis) == {"X_d", "Y_d", "Z_d"}
    )
    add("A02", "前视图与绘图坐标基", "automatic", "pass" if view_basis_ok else "fail",
        {"front_source": front, "basis": check.get("front_source_basis"),
         "camera_method": check.get("camera_method"), "drawing_basis": basis},
        "修正标准视向解析并重新投影六视图。")

    fasteners = check.get("fasteners") or []
    objects = check.get("objects") or {}
    model_ok = objects.get("sheet_bodies", 0) >= 1 and objects.get("fasteners") == len(fasteners)
    add("A03", "模型与实例盘点", "automatic", "pass" if model_ok else "fail",
        {"sheet_bodies": objects.get("sheet_bodies"), "declared_fasteners": objects.get("fasteners"),
         "recorded_fasteners": len(fasteners)},
        "修正装配树去重、本体识别或压铆实例提取后重新出图。")

    tags = [item.get("tag") for item in fasteners]
    required_fields_ok = all(
        item.get("category") and item.get("tag")
        and isinstance(item.get("outer"), (int, float)) and item["outer"] > 0
        and isinstance(item.get("inner"), (int, float)) and item["inner"] > 0
        and isinstance(item.get("height"), (int, float)) and item["height"] > 0
        and item.get("hole_type") in {"through", "blind"}
        and item.get("bore_continuity_confirmed") is True
        and (item.get("hole_type") == "through"
             or (isinstance(item.get("blind_hole_depth"), (int, float))
                 and item["blind_hole_depth"] > 0))
        and item.get("mark_status") == "confirmed"
        and item.get("manufacturing_hole_kind") in {"threaded", "unthreaded"}
        and isinstance(item.get("auxiliary_mark_text"), str)
        and bool(item["auxiliary_mark_text"].strip())
        and ((item.get("manufacturing_hole_kind") == "threaded"
              and isinstance(item.get("thread_designation"), str)
              and bool(item["thread_designation"]))
             or (item.get("manufacturing_hole_kind") == "unthreaded"
                 and item.get("thread_designation") is None))
        and isinstance(item.get("classification_key"), list)
        and len(item["classification_key"]) == 7
        and item.get("side") in EXPECTED_VIEWS
        and item.get("classification_status") == "confirmed"
        for item in fasteners
    )
    category_total = sum(int(row.get("total", 0)) for row in check.get("category_rows", []))
    side_total = sum(int(value) for value in (check.get("side_counts") or {}).values())
    candidate_summary = check.get("fastener_candidate_summary") or {}
    candidate_values = [
        candidate_summary.get("candidate_count"),
        candidate_summary.get("confirmed_count"),
        candidate_summary.get("rejected_count"),
        candidate_summary.get("pending_count"),
    ]
    candidate_ok = (
        all(isinstance(value, int) and value >= 0 for value in candidate_values)
        and candidate_summary.get("candidate_count") == sum(candidate_values[1:])
        and candidate_summary.get("confirmed_count") == len(fasteners)
        and candidate_summary.get("pending_count") == 0
    )
    annotation = check.get("annotation_validation") or {}
    position_tolerance = annotation.get("position_tolerance_mm")
    transforms = annotation.get("per_view_transforms") or {}
    transforms_ok = (
        isinstance(position_tolerance, (int, float)) and 0 < position_tolerance <= 1.0
        and set(transforms) == EXPECTED_VIEWS
        and all(calibrated_transform_ok(entry, float(position_tolerance)) for entry in transforms.values())
    )
    annotation_items = annotation.get("per_item") or []
    annotation_tags = [item.get("tag") for item in annotation_items]
    annotation_record_ok = (
        annotation.get("pass") is True
        and len(annotation_items) == len(fasteners)
        and len(annotation_tags) == len(set(annotation_tags))
        and set(annotation_tags) == set(tags)
        and all(item.get("pass") is True
                and item.get("hole_type") in {"through", "blind"}
                and item.get("bore_continuity_confirmed") is True
                and item.get("mark_status") == "confirmed"
                and item.get("manufacturing_hole_kind") in {"threaded", "unthreaded"}
                and isinstance(item.get("auxiliary_mark_text"), str)
                and bool(item["auxiliary_mark_text"].strip())
                and ((item.get("manufacturing_hole_kind") == "threaded"
                      and isinstance(item.get("thread_designation"), str)
                      and bool(item["thread_designation"]))
                     or (item.get("manufacturing_hole_kind") == "unthreaded"
                         and item.get("thread_designation") is None))
                and isinstance(item.get("classification_key"), list)
                and len(item["classification_key"]) == 7
                for item in annotation_items)
        and not annotation.get("extra_marker_handles")
        and not annotation.get("duplicate_marker_handles")
    )
    mark_coverage = stud_audit.get("auxiliary_mark_coverage") or {}
    present_mark_kinds = set(mark_coverage.get("present_manufacturing_hole_kinds") or [])
    mark_coverage_ok = (
        mark_coverage.get("pass") is True
        and mark_coverage.get("requires_both_manufacturing_hole_kinds") is False
        and mark_coverage.get("expected_instance_count") == len(fasteners)
        and mark_coverage.get("confirmed_mark_count") == len(fasteners)
        and not mark_coverage.get("invalid_or_missing_sources")
        and present_mark_kinds.issubset({"threaded", "unthreaded"})
        and (not fasteners or bool(present_mark_kinds))
    )
    stud_audit_ok = (
        stud_audit.get("pass") is True and stud_audit.get("status") == "pass"
        and same_dxf_path(stud_audit.get("path"))
        and stud_audit.get("sha256") == recorded_dxf_sha256 == current_dxf_sha256
        and stud_audit.get("source_hash_pass") is True
        and stud_audit.get("step_sha256") == source_sha256
        and stud_audit.get("recorded_step_sha256") == source_sha256
        and stud_audit.get("step_geometry_pass") is True
        and stud_audit.get("classification_pass") is True
        and mark_coverage_ok
        and not stud_audit.get("classification_errors")
        and not stud_audit.get("independent_pending_bore_sources")
        and not stud_audit.get("independent_pending_auxiliary_mark_sources")
        and not stud_audit.get("invalid_auxiliary_mark_inputs")
        and stud_audit.get("independent_step_fastener_count") == len(fasteners)
        and stud_audit.get("recorded_fastener_count") == len(fasteners)
        and not stud_audit.get("missing_step_sources")
        and not stud_audit.get("extra_step_sources")
        and stud_audit.get("expected_items") == len(fasteners)
        and stud_audit.get("checked_items") == len(fasteners)
        and not stud_audit.get("missing_anchor_handles")
        and not stud_audit.get("extra_anchor_handles")
        and not stud_audit.get("missing_tag_handles")
        and not stud_audit.get("extra_tag_handles")
        and not stud_audit.get("duplicate_anchor_handles")
        and not stud_audit.get("duplicate_tag_handles")
        and not stud_audit.get("missing_outline_handles")
        and not stud_audit.get("extra_outline_handles")
        and not stud_audit.get("duplicate_outline_handles")
        and all(item.get("outline_geometry_and_color_pass") is True
                for item in (stud_audit.get("items") or []))
    )
    stud_ok = (
        required_fields_ok and len(tags) == len(set(tags))
        and category_total == len(fasteners) == side_total
        and candidate_ok and transforms_ok and annotation_record_ok and stud_audit_ok
    )
    add("A04", "压铆识别、分类、分面与位号位置", "automatic", "pass" if stud_ok else "fail",
        {"instances": len(fasteners), "unique_tags": len(set(tags)),
         "category_total": category_total, "side_total": side_total,
         "candidate_summary": candidate_summary,
         "position_tolerance_mm": position_tolerance,
         "auxiliary_mark_coverage": mark_coverage,
         "per_view_transforms": transforms,
         "annotation_validation": annotation,
         "independent_final_dxf_stud_audit": stud_audit},
        "若仅漏标则补齐缺失锚点/位号并保留已有正确标注；否则重新确认候选、3D 辅助字符串及其中的螺纹规格、安装孔、柱轴、压铆件自身通孔/盲孔拓扑及盲孔深度、完整分类键、安装板外表面、柱身内外延伸和坐标变换，修复后重新导出并复查。")

    refs = fit.get("projection_reference_points") or {}
    projection = check.get("projection_validation") or {}
    projection_views = projection.get("per_view") or {}
    group_names = projection.get("group_names") or []
    group_count = len(group_names)
    visible_indices = projection.get("visible_group_indices") or []
    hidden_indices = projection.get("hidden_group_indices") or []
    all_group_indices = list(range(group_count))
    group_partition_ok = (
        group_count > 0
        and sorted(visible_indices + hidden_indices) == all_group_indices
        and not set(visible_indices).intersection(hidden_indices)
    )
    hlr_views_ok = set(projection_views) == EXPECTED_VIEWS
    if hlr_views_ok:
        hlr_views_ok = all(
            isinstance(entry, dict)
            and entry.get("pass") is True
            and len(entry.get("group_counts") or []) == group_count
            and all(isinstance(value, int) and value >= 0 for value in (entry.get("group_counts") or []))
            and sorted(entry.get("accounted_group_indices") or []) == all_group_indices
            and not entry.get("missing_functional_features")
            for entry in projection_views.values()
        )
    hlr_ok = (
        projection.get("pass") is True
        and bool(projection.get("interface"))
        and bool(projection.get("freecad_version"))
        and group_partition_ok
        and hlr_views_ok
    )
    layout_ok = False
    if set(refs) == EXPECTED_VIEWS:
        try:
            fx, fy = refs["FRONT"]
            layout_ok = (
                refs["LEFT"][0] < fx < refs["RIGHT"][0] < refs["REAR"][0]
                and refs["BOTTOM"][1] < fy < refs["TOP"][1]
                and all(abs(refs[name][1] - fy) <= 0.05 for name in ("LEFT", "RIGHT", "REAR"))
                and all(abs(refs[name][0] - fx) <= 0.05 for name in ("TOP", "BOTTOM"))
            )
        except (KeyError, TypeError, ValueError):
            layout_ok = False
    add("A05", "六视图布局、投影基准与 HLR 完整性", "automatic",
        "pass" if layout_ok and hlr_ok else "fail",
        {"views": sorted(refs), "projection_reference_points": refs,
         "interface": projection.get("interface"), "freecad_version": projection.get("freecad_version"),
         "group_names": group_names, "visible_group_indices": visible_indices,
         "hidden_group_indices": hidden_indices, "per_view": projection_views},
        "恢复固定第三角法布局和共同投影基准，完整读取全部 HLR 分组并补回缺失的内部功能轮廓后重新生成。")

    selected = fit.get("selected") or {}
    fit_ok = selected.get("pass") is True and bool(selected.get("sheet")) and bool(selected.get("scale"))
    add("A06", "图幅终评估", "automatic", "pass" if fit_ok else "fail",
        {"sheet": selected.get("sheet"), "scale": selected.get("scale"),
         "R_view": selected.get("R_view"), "pass": selected.get("pass")},
        "重新计算完整实体外包络、图幅、比例、净距和碰撞。")

    dimensions = check.get("dimension_validation") or {}
    dimension_views = dimensions.get("per_view") or {}
    style_validation = dimensions.get("style_validation") or {}
    expected_dimension_entities = style_validation.get("expected_dimension_entities")
    native_dimension_entities = style_validation.get("native_dimension_entities")
    native_style_ok = (
        style_validation.get("pass") is True
        and style_validation.get("entity_type") == "DIMENSION"
        and isinstance(expected_dimension_entities, int)
        and not isinstance(expected_dimension_entities, bool)
        and expected_dimension_entities > 0
        and native_dimension_entities == expected_dimension_entities
        and style_validation.get("manual_composed_dimensions") == 0
        and style_validation.get("closed_filled_arrows") is True
        and style_validation.get("extension_lines") is True
        and bool(style_validation.get("dimstyle_names"))
    )
    feature_record = check.get("feature_annotation_validation") or {}
    feature_audit_ok = (
        feature_audit.get("pass") is True and feature_audit.get("status") == "pass"
        and same_dxf_path(feature_audit.get("path"))
        and feature_audit.get("sha256") == recorded_dxf_sha256 == current_dxf_sha256
        and feature_audit.get("step_sha256") == source_sha256
        and feature_audit.get("independent_feature_count") == feature_record.get("source_feature_count")
        and feature_audit.get("independent_group_count") == feature_record.get("group_count")
        and feature_audit.get("recorded_group_count") == feature_record.get("group_count")
        and not feature_audit.get("missing_group_ids") and not feature_audit.get("extra_group_ids")
        and feature_record.get("annotation_policy") == "no_automatic_grouped_callout"
        and feature_record.get("displayed_group_callouts") == 0
        and feature_audit.get("record_policy_ok") is True
        and not feature_audit.get("forbidden_callout_handles")
    )
    dimensions_ok = (dimensions.get("pass") is True and native_style_ok
                     and set(dimension_views) == EXPECTED_VIEWS and feature_record.get("pass") is True
                     and feature_audit_ok)
    if dimensions_ok:
        dimensions_ok = all(
            isinstance(entry, dict)
            and int(entry.get("dimension_entities", 0)) > 0
            and not entry.get("missing")
            and entry.get("pass") is True
            for entry in dimension_views.values()
        )
    add("A07", "六视图尺寸覆盖与模型回算", "automatic", "pass" if dimensions_ok else "fail",
        {"views": sorted(dimension_views), "per_view": dimension_views,
         "style_validation": style_validation,
         "feature_annotation_validation": feature_record,
         "independent_step_to_dxf_feature_audit": feature_audit,
         "violations": dimensions.get("violations"), "pass": dimensions.get("pass")},
        "若仅漏标则补齐缺失的原生 DIMENSION/孔标并保留已有正确标注；否则按特征清单修正样式、定位、数值或上游识别后重新导出。")

    table = check.get("table_layout") or {}
    edge_attachment = table.get("edge_attachment") or {}
    height_ratio = table.get("text_cell_height_ratio") or {}
    containment = table.get("text_cell_containment") or {}
    checked_text_entities = table.get("checked_text_entities")
    checked_text_count_ok = (
        isinstance(checked_text_entities, int)
        and not isinstance(checked_text_entities, bool)
        and checked_text_entities > 0
        and containment.get("checked_text_entities") == checked_text_entities
    )
    table_ok = (
        edge_attachment.get("pass") is True
        and checked_text_count_ok
        and text_audit.get("pass") is True
        and text_audit.get("status") == "pass"
        and same_dxf_path(text_audit.get("path"))
        and text_audit.get("sha256") == recorded_dxf_sha256 == current_dxf_sha256
        and text_audit.get("checked_table_text_entities") == checked_text_entities
        and text_audit.get("manifest_entries") == checked_text_entities
        and not text_audit.get("missing_handles")
        and not text_audit.get("stale_handles")
        and (text_audit.get("font_validation") or {}).get("pass") is True
        and (text_audit.get("containment") or {}).get("pass") is True
        and (text_audit.get("containment") or {}).get("bbox_fail_count") == 0
        and (text_audit.get("containment") or {}).get("outside_cell_count") == 0
        and (text_audit.get("containment") or {}).get("line_intersection_count") == 0
        and (text_audit.get("containment") or {}).get("safety_clearance_fail_count") == 0
        and (text_audit.get("containment") or {}).get("height_ratio_fail_count") == 0
    )
    add("A08", "表格文字与单元格", "automatic", "pass" if table_ok else "fail",
        {"checked_text_entities": checked_text_entities,
         "minimum_actual_padding_mm": table.get("minimum_actual_padding_mm"),
         "edge_attachment": edge_attachment, "text_cell_height_ratio": height_ratio,
         "text_cell_containment": containment,
          "independent_final_dxf_text_audit": text_audit,
         "violations": table.get("violations")},
        "把表格重新锚定到图框边，给表名设置独立合并行，并按最终字体字形包围框重算基线、字号、行高、列宽和合并网格，确保文字不压线或跨格。")

    readback = check.get("readback") or {}
    dxf_ok = (
        dxf_exists
        and readback.get("audit_errors") == 0 and readback.get("audit_fixes") == 0
        and readback.get("sha256") == current_dxf_sha256
    )
    add("A09", "最终 DXF 回读与文件一致性", "automatic", "pass" if dxf_ok else "fail",
        {"path": str(dxf_path), "file_exists": dxf_path.is_file(),
         "audit_errors": readback.get("audit_errors"), "audit_fixes": readback.get("audit_fixes"),
         "recorded_sha256": readback.get("sha256")},
        "关闭源文档，重新导出并回读最终 DXF，禁止直接修补交付文件。")

    perf_ok = (
        performance_report_current
        and not perf.get("hard_failures") and not perf.get("audit_errors") and not perf.get("audit_fixes")
        and perf.get("duplicate_polylines_same_layer") == 0 and perf.get("nonfinite_points") == 0
        and perf.get("visible_hidden_exact_overlap") == 0
        and (perf.get("linetype_validation") or {}).get("pass") is True
    )
    geometry_export = projection.get("geometry_export") or {}
    linetype = projection.get("linetype_validation") or {}
    projection_structure_ok = (
        geometry_export.get("pass") is True
        and geometry_export.get("indiscriminate_discretization") is False
        and geometry_export.get("unqualified_discretized_edges") == 0
        and geometry_export.get("broken_closed_loops") == 0
        and linetype.get("pass") is True
        and linetype.get("explicit_global_scale") is True
        and linetype.get("explicit_entity_scales") is True
    )
    add("A10", "DXF 结构、解析实体与碎片性能", "automatic",
        "pass" if perf_ok and projection_structure_ok else "fail",
        {"status": perf.get("status"), "entities": perf.get("modelspace_entities"),
         "vertices": perf.get("polyline_vertices"), "file_size": perf.get("file_size_bytes"),
         "duplicate_polylines": perf.get("duplicate_polylines_same_layer"),
         "visible_hidden_exact_overlap": perf.get("visible_hidden_exact_overlap"),
         "independent_linetype_validation": perf.get("linetype_validation"),
         "nonfinite_points": perf.get("nonfinite_points"), "hard_failures": perf.get("hard_failures"),
         "geometry_export": geometry_export, "linetype_validation": linetype},
        "改为解析 LINE/ARC/CIRCLE，合并连续物理边并显式设置线型比例，然后重新导出、审计。")

    line_review = projection.get("readback_line_review") or {}
    preview_ok = (
        preview_current
        and line_review.get("source") == "independent_final_dxf_preview_review"
        and line_review.get("preview_sha256") == sha256(args.preview)
        and bool(line_review.get("reviewer"))
        and line_review.get("pass") is True
        and line_review.get("visible_contour_breaks") == 0
        and line_review.get("missing_internal_features") == 0
        and line_review.get("hidden_pattern_issues") == 0
    )
    add("A11", "最终 DXF 回读预览", "automatic", "pass" if preview_ok else "fail",
        {"path": str(args.preview), "exists": args.preview.is_file(),
         "size": args.preview.stat().st_size if args.preview.is_file() else 0,
         "actual_preview_sha256": sha256(args.preview) if args.preview.is_file() else None,
         "readback_line_review": line_review},
        "从当前最终 DXF 重新生成高分辨率预览，修复可见轮廓断口、点状虚线或内部轮廓缺失后复查。")

    pending_inputs = [
        value for value in (check.get("pending") or [])
        if "阅读器" not in str(value) and "reader" not in str(value).lower()
    ]
    add("I01", "项目输入完整性", "project_input", "pending" if pending_inputs else "pass",
        {"pending": pending_inputs}, "取得设计确认值，更新生成源并重新出图验收。")
    add("M01", "视向、镜像和特征人工核对", "manual", args.view_review,
        {"reviewer": args.reviewer}, "设计人员按 STEP 与最终 DXF 逐视图核对后返工或确认。")
    add("M02", "尺寸、压铆标注和纸面表达人工核对", "manual", args.drawing_review,
        {"reviewer": args.reviewer}, "设计人员按最终 DXF 和实际打印比例审图，问题回到生成源修复。")
    add("M03", "目标 CAD 交互兼容性", "manual", args.target_reader,
        {"declared_reader": check.get("target_reader")}, "在指定 CAD 中完成打开、缩放、图层、打印和保存副本测试。")
    add("P01", "工程批准/投产放行", "approval", args.engineering_approval,
        {"reviewer": args.reviewer}, "由企业授权人员按项目流程批准，生成程序不得自行放行。")

    failed = [item for item in items if item["status"] == "fail"]
    pending = [item for item in items if item["status"] == "pending"]
    drawing_items = [item for item in items if item["gate"] != "approval"]
    drawing_failed = [item for item in drawing_items if item["status"] == "fail"]
    drawing_pending = [item for item in drawing_items if item["status"] == "pending"]
    approval = next(item for item in items if item["gate"] == "approval")
    automatic = [item for item in items if item["gate"] == "automatic"]
    automatic_codes = {item["code"] for item in automatic}
    no_skipped_automatic_checks = (
        automatic_codes == EXPECTED_AUTOMATIC_CODES
        and all(item["status"] in {"pass", "fail"} for item in automatic)
    )
    auto_status = "pass" if no_skipped_automatic_checks and all(item["status"] == "pass" for item in automatic) else "fail"
    if drawing_failed:
        overall = "rework_required"
    elif drawing_pending:
        overall = "awaiting_required_input_or_manual_acceptance"
    else:
        overall = "drawing_acceptance_passed"
    if overall != "drawing_acceptance_passed":
        production_release = "not_eligible"
    elif approval["status"] == "pass":
        production_release = "released"
    elif approval["status"] == "fail":
        production_release = "rejected"
    else:
        production_release = "pending"

    failed_codes = [item["code"] for item in failed]
    pending_codes = [item["code"] for item in pending]
    if drawing_failed:
        loop_action = "supplement_isolated_missing_annotations_or_repair_affected_source_then_reexport_and_repeat_all_checks"
    elif drawing_pending:
        loop_action = "complete_required_input_and_manual_checks_then_rerun_acceptance"
    else:
        loop_action = "drawing_checks_complete"

    report = {
        "schema": "freecad-sheetmetal-drawing/project-acceptance/v9",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project": {"source": check.get("source"), "dxf": str(dxf_path)},
        "automatic_gate": auto_status,
        "overall_status": overall,
        "production_release": production_release,
        "closed_loop": {
            "no_skipped_automatic_checks": no_skipped_automatic_checks,
            "expected_automatic_codes": sorted(EXPECTED_AUTOMATIC_CODES),
            "executed_automatic_codes": sorted(automatic_codes),
            "current_dxf_sha256": current_dxf_sha256,
            "current_step_sha256": source_sha256,
            "independent_step_to_dxf_geometry_verified": stud_audit_ok,
            "all_independent_artifacts_bound_to_current_dxf": (
                performance_report_current
                and same_dxf_path(text_audit.get("path"))
                and text_audit.get("sha256") == current_dxf_sha256
                and same_dxf_path(stud_audit.get("path"))
                and stud_audit.get("sha256") == current_dxf_sha256
                and same_dxf_path(feature_audit.get("path"))
                and feature_audit.get("sha256") == current_dxf_sha256
                and feature_audit.get("step_sha256") == source_sha256
                and preview_current
            ),
            "failed_codes": failed_codes,
            "pending_codes": pending_codes,
            "next_action": loop_action,
            "drawing_checks_complete": overall == "drawing_acceptance_passed",
        },
        "drawing_summary": {
            "pass": sum(i["status"] == "pass" for i in drawing_items),
            "fail": len(drawing_failed),
            "pending": len(drawing_pending),
            "not_applicable": sum(i["status"] == "not_applicable" for i in drawing_items),
        },
        "summary": {"pass": sum(i["status"] == "pass" for i in items),
                    "fail": len(failed), "pending": len(pending),
                    "not_applicable": sum(i["status"] == "not_applicable" for i in items)},
        "items": items,
        "next_actions": [item["action_if_not_pass"] for item in failed + pending],
        "reviewer": args.reviewer,
        "notes": args.notes,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "automatic_gate": auto_status,
                      "overall_status": overall, "production_release": production_release,
                      "drawing_summary": report["drawing_summary"]}, ensure_ascii=False))
    if drawing_failed:
        return 2
    if drawing_pending:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
