"""Interactive walkthrough of the gauging line and ticket office.

Run ``python3.11 tools/demo_gauging.py`` after ``tools/build_gauging.py``.  The
demo reads the published products and frame samples under ``artifacts/gauging``
and prints the intermediate numbers (calibrations, per-segment width basis,
admission verdicts, the two bagging outcomes) rather than asserting silently.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.gauging import GaugingProduct, GaugingStation  # noqa: E402
from bitcatalog.boxoffice import BoxOfficeLedger, plan_batch  # noqa: E402


def header(text: str) -> None:
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78)


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "artifacts" / "gauging"))
    args = parser.parse_args(argv)
    out = Path(args.out)
    v1 = GaugingProduct.load(out / "gauging_v1.json")
    v2 = GaugingProduct.load(out / "gauging_v2.json")

    header("1. published gauging conventions (content-addressed)")
    print(f"v1 {v1.fingerprint[:16]}... bands "
          f"{[(b.face_value, str(b.lo), str(b.hi)) for b in v1.width_table.bands]}")
    print(f"v2 {v2.fingerprint[:16]}... bands "
          f"{[(b.face_value, str(b.lo), str(b.hi)) for b in v2.width_table.bands]}")

    for title, file_name in (
        ("2a. variable speed: width 4 -> three-fen", "frames_t1_threefen.jsonl"),
        ("2b. self-check only: old speed holds, width 2 -> one-fen", "frames_t2_onefen.jsonl"),
    ):
        header(title)
        report = GaugingStation(v1).process(read_jsonl(out / file_name))
        for cal in report.calibrations:
            print(f"calibration {cal.mark_id}: active from {cal.start_ns}, speed {cal.speed} "
                  f"(transit {cal.rear_valid_ns - cal.front_valid_ns} ns)")
        for admission in report.admissions:
            if admission.status == "admitted":
                pieces = ", ".join(
                    f"[{s.start_ns},{s.end_ns})@{s.speed}" for s in admission.segments
                )
                print(f"  {admission.token_id}: width={admission.width} face={admission.face_value}  {pieces}")

    header("3. quarantine samples (never enter the catalog)")
    quar = GaugingStation(v1).process(read_jsonl(out / "frames_quarantine.jsonl"))
    for admission in quar.admissions:
        print(f"  {admission.token_id}: {admission.status} reason={admission.reason}")

    header("4. ticket office: per-bag feasibility is not batch feasibility")
    office = json.loads((out / "boxoffice_report.json").read_text())
    for label in ("threefen_scenario", "onefen_scenario"):
        scenario = office[label]
        plan = scenario["simultaneous_plan"]
        print(f"\n{label}: stock {scenario['stock_counts']}")
        base = scenario["baseline_two_amount2_orders"]
        print(f"  two amount-2 orders -> {base['blocker']['code']}")
        if plan["feasible"]:
            for bag in plan["bags"]:
                print(f"  {bag['order_id']}: {[(l['serial'], l['face_value']) for l in bag['lines']]} "
                      f"= {bag['total']}")
        else:
            print(f"  simultaneous orders blocked: {plan['blocker']['code']} -- {plan['blocker']['message']}")

    header("5. ledger: confirmed tokens are physically deducted")
    with BoxOfficeLedger(out / "boxoffice_threefen.db") as ledger:
        print("threefen available stock after confirmation:", ledger.stock_counts())
        print("stored batches:", ledger.manifests())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
