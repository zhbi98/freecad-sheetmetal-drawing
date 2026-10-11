#!/usr/bin/env python3
"""Run the independent STEP-to-DXF automatic acceptance loop 2 or 3 times."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", required=True, type=Path)
    parser.add_argument("--dxf", required=True, type=Path)
    parser.add_argument("--check", required=True, type=Path)
    parser.add_argument("--fit", required=True, type=Path)
    parser.add_argument("--preview", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--vendor-path", type=Path)
    parser.add_argument("--runs", type=int, choices=(2, 3), default=3)
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(command: list[str], accepted_codes: set[int]) -> dict[str, Any]:
    completed = subprocess.run(command, capture_output=True)
    stdout = (completed.stdout or b"").decode("utf-8", errors="replace")
    stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
    return {
        "command": command,
        "returncode": completed.returncode,
        "accepted_returncode": completed.returncode in accepted_codes,
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-2000:],
    }


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    args = parse_args()
    required = (args.step, args.dxf, args.check, args.fit, args.preview)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise SystemExit("Missing required input(s): " + ", ".join(missing))

    scripts = Path(__file__).resolve().parent
    args.output_dir.mkdir(parents=True, exist_ok=True)
    source_hash = sha256(args.step)
    dxf_hash = sha256(args.dxf)
    fixed_input_hashes = {
        "step": source_hash,
        "dxf": dxf_hash,
        "check": sha256(args.check),
        "fit": sha256(args.fit),
        "preview": sha256(args.preview),
    }
    runs = []
    previous_acceptance_time = None

    for index in range(1, args.runs + 1):
        run_dir = args.output_dir / f"run_{index:02d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        text_report = run_dir / "dxf_text_audit.json"
        stud_report = run_dir / "stud_annotation_audit.json"
        performance_report = run_dir / "dxf_performance_audit.json"
        feature_report = run_dir / "feature_annotation_audit.json"
        acceptance_report = run_dir / "project_acceptance.json"
        common_vendor = (["--vendor-path", str(args.vendor_path.resolve())]
                         if args.vendor_path is not None else [])
        python = [sys.executable, "-X", "utf8"]

        commands = []
        commands.append(run_command([*python,
            str(scripts / "dxf_text_audit.py"), str(args.dxf.resolve()),
            "--check", str(args.check.resolve()), *common_vendor,
            "--json-out", str(text_report.resolve()),
        ], {0}))
        commands.append(run_command([*python,
            str(scripts / "stud_annotation_audit.py"), str(args.dxf.resolve()),
            "--step", str(args.step.resolve()), "--check", str(args.check.resolve()),
            *common_vendor, "--json-out", str(stud_report.resolve()),
        ], {0}))
        commands.append(run_command([*python,
            str(scripts / "feature_annotation_audit.py"), str(args.dxf.resolve()),
            "--step", str(args.step.resolve()), "--check", str(args.check.resolve()),
            *common_vendor, "--json-out", str(feature_report.resolve()),
        ], {0}))
        commands.append(run_command([*python,
            str(scripts / "dxf_performance_audit.py"), str(args.dxf.resolve()),
            *common_vendor, "--json-out", str(performance_report.resolve()),
        ], {0}))
        commands.append(run_command([*python,
            str(scripts / "project_acceptance.py"),
            "--check", str(args.check.resolve()), "--fit", str(args.fit.resolve()),
            "--performance", str(performance_report.resolve()),
            "--text-audit", str(text_report.resolve()),
            "--stud-audit", str(stud_report.resolve()),
            "--feature-audit", str(feature_report.resolve()),
            "--preview", str(args.preview.resolve()),
            "--output", str(acceptance_report.resolve()),
        ], {0, 3}))

        reports_exist = all(path.is_file() for path in (
            text_report, stud_report, feature_report, performance_report, acceptance_report
        ))
        text_data = read_json(text_report) if text_report.is_file() else {}
        stud_data = read_json(stud_report) if stud_report.is_file() else {}
        feature_data = read_json(feature_report) if feature_report.is_file() else {}
        performance_data = read_json(performance_report) if performance_report.is_file() else {}
        acceptance_data = read_json(acceptance_report) if acceptance_report.is_file() else {}
        acceptance_time = acceptance_data.get("generated_at")
        distinct_sequence = acceptance_time is not None and acceptance_time != previous_acceptance_time
        previous_acceptance_time = acceptance_time
        current_input_hashes = {
            "step": sha256(args.step), "dxf": sha256(args.dxf),
            "check": sha256(args.check), "fit": sha256(args.fit),
            "preview": sha256(args.preview),
        }
        inputs_unchanged = current_input_hashes == fixed_input_hashes

        hashes_pass = (
            stud_data.get("step_sha256") == source_hash
            and stud_data.get("sha256") == dxf_hash
            and text_data.get("sha256") == dxf_hash
            and feature_data.get("step_sha256") == source_hash
            and feature_data.get("sha256") == dxf_hash
            and Path(str(performance_data.get("path", ""))).resolve() == args.dxf.resolve()
            and performance_report.stat().st_mtime_ns >= args.dxf.stat().st_mtime_ns
            and (acceptance_data.get("closed_loop") or {}).get("current_step_sha256") == source_hash
            and (acceptance_data.get("closed_loop") or {}).get("current_dxf_sha256") == dxf_hash
        )
        automatic_pass = (
            reports_exist and all(item["accepted_returncode"] for item in commands)
            and text_data.get("pass") is True
            and stud_data.get("pass") is True
            and stud_data.get("step_geometry_pass") is True
            and feature_data.get("pass") is True
            and not performance_data.get("hard_failures")
            and acceptance_data.get("automatic_gate") == "pass"
            and not (acceptance_data.get("closed_loop") or {}).get("failed_codes")
            and (acceptance_data.get("closed_loop") or {}).get(
                "independent_step_to_dxf_geometry_verified") is True
            and hashes_pass and inputs_unchanged and distinct_sequence
        )
        runs.append({
            "run": index,
            "started_new_processes": len(commands),
            "source_sha256": source_hash,
            "dxf_sha256": dxf_hash,
            "reports": {
                "text": str(text_report.resolve()),
                "stud": str(stud_report.resolve()),
                "feature": str(feature_report.resolve()),
                "performance": str(performance_report.resolve()),
                "acceptance": str(acceptance_report.resolve()),
            },
            "acceptance_generated_at": acceptance_time,
            "distinct_sequence": distinct_sequence,
            "inputs_unchanged": inputs_unchanged,
            "input_hashes": current_input_hashes,
            "hashes_pass": hashes_pass,
            "automatic_gate": acceptance_data.get("automatic_gate"),
            "pending_codes": (acceptance_data.get("closed_loop") or {}).get("pending_codes"),
            "commands": commands,
            "pass": automatic_pass,
        })

    all_pass = len(runs) == args.runs and all(run["pass"] for run in runs)
    summary = {
        "schema": "freecad-sheetmetal-drawing/closed-loop-recheck/v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "required_runs": args.runs,
        "completed_runs": len(runs),
        "source_path": str(args.step.resolve()),
        "source_sha256": source_hash,
        "dxf_path": str(args.dxf.resolve()),
        "dxf_sha256": dxf_hash,
        "fixed_input_hashes": fixed_input_hashes,
        "all_runs_same_source_and_dxf": all(
            run["source_sha256"] == source_hash and run["dxf_sha256"] == dxf_hash
            for run in runs
        ),
        "all_runs_same_inputs": all(run["inputs_unchanged"] for run in runs),
        "status": "pass" if all_pass else "fail",
        "pass": all_pass,
        "runs": runs,
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    print(json.dumps({
        "summary": str(args.summary), "required_runs": args.runs,
        "completed_runs": len(runs), "status": summary["status"],
    }, ensure_ascii=False))
    return 0 if all_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
