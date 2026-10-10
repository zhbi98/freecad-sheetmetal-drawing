#!/usr/bin/env python3
"""Aggregate per-project drawing checks into a deterministic acceptance record."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


EXPECTED_VIEWS = {"TOP", "BOTTOM", "FRONT", "REAR", "LEFT", "RIGHT"}
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", required=True, type=Path)
    parser.add_argument("--fit", required=True, type=Path)
    parser.add_argument("--performance", required=True, type=Path)
    parser.add_argument("--text-audit", required=True, type=Path)
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

    source_ok = bool(check.get("source")) and check.get("source_preserved") is True
    add("A01", "源模型标识与保留", "automatic", "pass" if source_ok else "fail",
        {"source": check.get("source"), "source_preserved": check.get("source_preserved")},
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
        and item.get("side") in EXPECTED_VIEWS
        for item in fasteners
    )
    category_total = sum(int(row.get("total", 0)) for row in check.get("category_rows", []))
    side_total = sum(int(value) for value in (check.get("side_counts") or {}).values())
    stud_ok = (
        required_fields_ok and len(tags) == len(set(tags))
        and category_total == len(fasteners) == side_total
    )
    add("A04", "压铆分类、位号与数量", "automatic", "pass" if stud_ok else "fail",
        {"instances": len(fasteners), "unique_tags": len(set(tags)),
         "category_total": category_total, "side_total": side_total},
        "重新识别和分类压铆件，修复位号或数量来源后整图重生成。")

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
    dimensions_ok = dimensions.get("pass") is True and native_style_ok and set(dimension_views) == EXPECTED_VIEWS
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
         "violations": dimensions.get("violations"), "pass": dimensions.get("pass")},
        "按特征尺寸清单补齐六个视图尺寸，改用原生 DIMENSION 和统一 DIMSTYLE，修正箭头、尺寸界线、符号、定位或数值后从模型重新生成。")

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
        and text_audit.get("sha256") == (check.get("readback") or {}).get("sha256")
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

    dxf_path = Path(perf.get("path", ""))
    readback = check.get("readback") or {}
    dxf_ok = (
        dxf_path.is_file() and dxf_path.stat().st_size > 0
        and readback.get("audit_errors") == 0 and readback.get("audit_fixes") == 0
        and readback.get("sha256") == sha256(dxf_path)
    )
    add("A09", "最终 DXF 回读与文件一致性", "automatic", "pass" if dxf_ok else "fail",
        {"path": str(dxf_path), "file_exists": dxf_path.is_file(),
         "audit_errors": readback.get("audit_errors"), "audit_fixes": readback.get("audit_fixes"),
         "recorded_sha256": readback.get("sha256")},
        "关闭源文档，重新导出并回读最终 DXF，禁止直接修补交付文件。")

    perf_ok = (
        not perf.get("hard_failures") and not perf.get("audit_errors") and not perf.get("audit_fixes")
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
        args.preview.is_file() and args.preview.stat().st_size > 0
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
    auto_status = "pass" if all(item["status"] == "pass" for item in automatic) else "fail"
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

    report = {
        "schema": "freecad-sheetmetal-drawing/project-acceptance/v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project": {"source": check.get("source"), "dxf": str(dxf_path)},
        "automatic_gate": auto_status,
        "overall_status": overall,
        "production_release": production_release,
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
