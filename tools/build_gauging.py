"""End-to-end build for the dual-gate gauging line and ticket-office batches.

Deliverables under ``--out`` (default ``artifacts/gauging``):

gauging convention
------------------
* ``gauging_v1.json`` / ``gauging_v2.json`` -- publishable, content-addressed
  speed/width products (v2 revises the half-open width bands).
* ``frames_t1_threefen.jsonl`` -- the worked line: variable belt speed, ruler
  marks on both gates, a self-check frame inside the token interval; the new
  token gauges to width 4 -> three-fen.
* ``frames_t2_onefen.jsonl`` -- the same token frames but the second speed
  segment carries only self-check frames; the old calibration stays valid and
  the token gauges to width 2 -> one-fen.
* ``frames_quarantine.jsonl`` -- local samples exercising every isolation
  reason (missing speed, mark conflict, edge misalignment, unclassified width).
* ``gauging_report.json`` -- calibrations, per-segment width basis, admissions
  and quarantines for every frame file, plus the v1/v2 reclassification view.

ticket office
-------------
* ``boxoffice_threefen.db`` / ``boxoffice_onefen.db`` -- append-only ledgers
  holding the physical tokens and the confirmed bag batches (atomic deduction,
  each line retaining the token's gauging basis).
* ``bag_manifest_*.json`` -- self-contained, hash-sealed bag manifests.
* ``boxoffice_report.json`` -- the simultaneous-batch outcomes: one feasible
  whole batch and the two stock/denomination blockers from the narrative.
* ``offline_recheck.json`` -- database-free re-check of every manifest and
  admission, including the width-table revision on a second offline computer.
"""

from __future__ import annotations

import argparse
import json
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.gauging import (  # noqa: E402
    BRANCH_RULER,
    BRANCH_SELFCHECK,
    BRANCH_TOKEN,
    EDGE_LEADING,
    EDGE_TRAILING,
    GATE_FRONT,
    GATE_REAR,
    GaugingProduct,
    GaugingStation,
    recheck_admission,
)
from bitcatalog.boxoffice import (  # noqa: E402
    BoxOfficeLedger,
    InventoryToken,
    plan_batch,
    recheck_manifest,
)
from tools.belt_kinematics import front_edge_time  # noqa: E402

GATE_SPACING = Fraction(1)
NS_PER_UNIT = 3  # 1 narrative tick = 3 ns; slow speed 1 unit/tick = 1/3 unit per ns

# Time origin shift so every stored nanosecond timestamp is positive, including
# the pre-calibration quarantine sample.
ORIGIN = 60

# Piecewise speed line, scenario A (variable belt).  Narrative speeds 1 and 3
# units per tick become 1/3 and 1 unit per ns; the fast segment therefore
# crosses the unit gate spacing in exactly one nanosecond.
SPEED_SLOW = Fraction(1, 3)
SPEED_FAST = Fraction(1)
SWITCH_NS = ORIGIN + 3 * NS_PER_UNIT  # narrative tick 3, where m2 takes effect


def t(unit_time: int) -> int:
    """Convert narrative unit-time ticks to integer nanoseconds."""

    return ORIGIN + unit_time * NS_PER_UNIT


# ---------------------------------------------------------------------------
# frame construction
# ---------------------------------------------------------------------------


def ruler_frame(event_id: str, unit_time: int, gate: str, mark: str, seq: int | None = None) -> dict[str, Any]:
    frame = {
        "frame_event_id": event_id,
        "occurred_at_ns": t(unit_time),
        "gate": gate,
        "branch": BRANCH_RULER,
        "mark_id": mark,
    }
    if seq is not None:
        frame["gate_seq"] = seq
    return frame


def selfcheck_frame(event_id: str, unit_time: int, gate: str, seq: int) -> dict[str, Any]:
    return {
        "frame_event_id": event_id,
        "occurred_at_ns": t(unit_time),
        "gate": gate,
        "branch": BRANCH_SELFCHECK,
        "gate_seq": seq,
    }


def token_frames(
    pieces: list[tuple[int, Fraction]],
    token_id: str,
    rear_leading_unit: int,
    rear_trailing_unit: int,
) -> list[dict[str, Any]]:
    """Both gates' edge frames, front times inverted from the belt motion."""

    rear_leading = t(rear_leading_unit)
    rear_trailing = t(rear_trailing_unit)
    front_leading = front_edge_time(pieces, rear_leading, GATE_SPACING)
    front_trailing = front_edge_time(pieces, rear_trailing, GATE_SPACING)
    return [
        {
            "frame_event_id": f"{token_id}-A-L",
            "occurred_at_ns": front_leading,
            "gate": GATE_FRONT,
            "branch": BRANCH_TOKEN,
            "edge": EDGE_LEADING,
            "token_id": token_id,
        },
        {
            "frame_event_id": f"{token_id}-A-T",
            "occurred_at_ns": front_trailing,
            "gate": GATE_FRONT,
            "branch": BRANCH_TOKEN,
            "edge": EDGE_TRAILING,
            "token_id": token_id,
        },
        {
            "frame_event_id": f"{token_id}-B-L",
            "occurred_at_ns": rear_leading,
            "gate": GATE_REAR,
            "branch": BRANCH_TOKEN,
            "edge": EDGE_LEADING,
            "token_id": token_id,
        },
        {
            "frame_event_id": f"{token_id}-B-T",
            "occurred_at_ns": rear_trailing,
            "gate": GATE_REAR,
            "branch": BRANCH_TOKEN,
            "edge": EDGE_TRAILING,
            "token_id": token_id,
        },
    ]


def speed_pieces_a() -> list[tuple[int, Fraction]]:
    # Calibrations become active at rear-gate ruler events: m0 t(-6)=12,
    # m1 t(1)=33 (both slow); m2 rear event at SWITCH_NS=39 switches to fast.
    return [
        (t(-6), SPEED_SLOW),
        (SWITCH_NS, SPEED_FAST),
    ]


def speed_pieces_b() -> list[tuple[int, Fraction]]:
    return [(t(-6), SPEED_SLOW)]


def ruler_marks(include_m2: bool) -> list[dict[str, Any]]:
    # At the slow speed (1/3 unit per ns) crossing the unit gate spacing takes
    # exactly one tick (3 ns), so each slow mark's two gates are one tick
    # apart; the derived calibration speed then matches the belt kinematics.
    marks = [
        ruler_frame("m0-A", -7, GATE_FRONT, "m0", 1),
        ruler_frame("m0-B", -6, GATE_REAR, "m0", 1),
        ruler_frame("m1-A", 0, GATE_FRONT, "m1", 2),
        ruler_frame("m1-B", 1, GATE_REAR, "m1", 2),
    ]
    if include_m2:
        # 1 ns transit at the fast speed: front SWITCH_NS-1, rear SWITCH_NS.
        marks.extend(
            [
                {
                    "frame_event_id": "m2-A",
                    "occurred_at_ns": SWITCH_NS - 1,
                    "gate": GATE_FRONT,
                    "branch": BRANCH_RULER,
                    "mark_id": "m2",
                    "gate_seq": 3,
                },
                ruler_frame("m2-B", 3, GATE_REAR, "m2", 3),
            ]
        )
    return marks


def build_main_frames(include_m2: bool, token_id: str) -> list[dict[str, Any]]:
    pieces = speed_pieces_a() if include_m2 else speed_pieces_b()
    frames = ruler_marks(include_m2)
    # Legacy one-fen stock, fully inside the slow segment (width 2 each).
    frames.extend(token_frames(pieces, "L1", -3, -1))
    frames.extend(token_frames(pieces, "L2", -1, 1))
    # New token: rear-gate occlusion [2, 4) in unit time.
    frames.extend(token_frames(pieces, token_id, 2, 4))
    # Self-check frame inside the occlusion interval; never a calibration.
    frames.append(selfcheck_frame("sc-B-1", 3, GATE_REAR, 9))
    frames.append(selfcheck_frame("sc-A-1", 2, GATE_FRONT, 9))
    frames.sort(key=lambda f: (f["occurred_at_ns"], f["frame_event_id"]))
    return frames


def build_quarantine_frames() -> list[dict[str, Any]]:
    pieces = speed_pieces_a()
    frames = ruler_marks(include_m2=True)

    # Q1: rear-gate occlusion finishes before the first calibration (m0 active
    # at t(-6)=12); the width integral has no in-force speed -> missing_speed.
    # Front frames are hand-laid because the belt kinematics themselves are
    # undefined before the first speed piece.
    q1_edges = [
        ("Q1-A-L", t(-15), GATE_FRONT, EDGE_LEADING),
        ("Q1-A-T", t(-13), GATE_FRONT, EDGE_TRAILING),
        ("Q1-B-L", t(-12), GATE_REAR, EDGE_LEADING),
        ("Q1-B-T", t(-10), GATE_REAR, EDGE_TRAILING),
    ]
    for event_id, ns, gate, edge in q1_edges:
        frames.append(
            {
                "frame_event_id": event_id,
                "occurred_at_ns": ns,
                "gate": gate,
                "branch": BRANCH_TOKEN,
                "edge": edge,
                "token_id": "Q1",
            }
        )

    # Q2: duplicate ruler observation on the rear gate -> mark_conflict; the
    # token overlaps the conflict window and is isolated even though its own
    # edges are well-formed.
    frames.append(ruler_frame("mX-A", 4, GATE_FRONT, "mX", 5))
    frames.append(ruler_frame("mX-B1", 5, GATE_REAR, "mX", 5))
    frames.append(ruler_frame("mX-B2", 6, GATE_REAR, "mX", 5))
    frames.extend(token_frames(pieces, "Q2", 5, 7))

    # Q3: trailing edge missing on the rear gate -> edge_misaligned.
    q3 = token_frames(pieces, "Q3", 7, 8)
    frames.extend(f for f in q3 if f["frame_event_id"] != "Q3-B-T")

    # QU: width 6 fits no v1 band -> unclassified.  Rear occlusion [8, 14).
    frames.extend(token_frames(pieces, "QU", 8, 14))

    frames.sort(key=lambda f: (f["occurred_at_ns"], f["frame_event_id"]))
    return frames


# ---------------------------------------------------------------------------
# products
# ---------------------------------------------------------------------------


def build_products() -> tuple[GaugingProduct, GaugingProduct]:
    v1 = GaugingProduct.build(
        gate_spacing="1",
        valid_gate=GATE_REAR,
        valid_edge=EDGE_LEADING,
        calibration_ttl_ns=None,
        width_table={
            "version": "width-v1",
            "bands": [
                {"face_value": 1, "lo": "1", "hi": "3"},
                {"face_value": 3, "lo": "3", "hi": "5"},
            ],
        },
    )
    v2 = GaugingProduct.build(
        gate_spacing="1",
        valid_gate=GATE_REAR,
        valid_edge=EDGE_LEADING,
        calibration_ttl_ns=None,
        width_table={
            "version": "width-v2",
            "bands": [
                {"face_value": 1, "lo": "1", "hi": "2"},
                {"face_value": 2, "lo": "2", "hi": "4"},
                {"face_value": 3, "lo": "4", "hi": "6"},
            ],
        },
    )
    return v1, v2


def write_jsonl(path: Path, frames: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for frame in frames:
            fh.write(json.dumps(frame, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


# ---------------------------------------------------------------------------
# ticket-office scenarios
# ---------------------------------------------------------------------------

ORDERS = [
    {"order_id": "bag-A", "amount": 3, "max_tokens": 2, "accepted_values": [1, 3]},
    {"order_id": "bag-B", "amount": 2, "max_tokens": 2, "accepted_values": [1, 2]},
]

# Narrative baseline: two orders of amount 2, only the two legacy one-fen
# tokens exist -> exactly one bag can be made.
BASELINE_ORDERS = [
    {"order_id": "base-A", "amount": 2, "max_tokens": 2, "accepted_values": [1, 2]},
    {"order_id": "base-B", "amount": 2, "max_tokens": 2, "accepted_values": [1, 2]},
]


def run_ledger(
    db_path: Path,
    product: GaugingProduct,
    admissions: list[dict[str, Any]],
    *,
    label: str,
) -> dict[str, Any]:
    if db_path.exists():
        db_path.unlink()
    ledger = BoxOfficeLedger(db_path)
    ledger.register_gauging_product(product)
    tokens = [InventoryToken.from_admission(a) for a in admissions]
    added = ledger.admit_tokens(tokens)

    baseline = plan_batch(ledger.available_tokens(), BASELINE_ORDERS)
    plan = ledger.plan(ORDERS)
    result: dict[str, Any] = {
        "label": label,
        "tokens_admitted": added,
        "stock_counts": {str(k): v for k, v in sorted(ledger.stock_counts().items())},
        "baseline_two_amount2_orders": baseline.to_json(),
        "orders": ORDERS,
        "simultaneous_plan": plan.to_json(),
    }
    if plan.feasible:
        manifest = ledger.confirm(
            plan,
            bag_batch_id=f"batch-{label}",
            created_at="2026-11-20T08:30:00Z",
        )
        manifest_path = db_path.parent / f"bag_manifest_{label}.json"
        ledger.export_manifest("batch-" + label, manifest_path)
        result["confirmed_batch"] = manifest["bag_batch_id"]
        result["stock_after_confirm"] = {
            str(k): v for k, v in sorted(ledger.stock_counts().items())
        }
        result["manifest_file"] = manifest_path.name
    ledger.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "artifacts" / "gauging"))
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    v1, v2 = build_products()
    v1.save(out / "gauging_v1.json")
    v2.save(out / "gauging_v2.json")

    frames_three = build_main_frames(include_m2=True, token_id="T1")
    frames_one = build_main_frames(include_m2=False, token_id="T2")
    frames_quar = build_quarantine_frames()
    write_jsonl(out / "frames_t1_threefen.jsonl", frames_three)
    write_jsonl(out / "frames_t2_onefen.jsonl", frames_one)
    write_jsonl(out / "frames_quarantine.jsonl", frames_quar)

    def gauging_summary(frames: list[dict[str, Any]]) -> dict[str, Any]:
        report = GaugingStation(v1).process(frames)
        return report

    report_three = gauging_summary(frames_three)
    report_one = gauging_summary(frames_one)
    report_quar = gauging_summary(frames_quar)

    # v2 revision view: same widths, versioned half-open bands re-classify.
    revision_rows = []
    for report in (report_three, report_one):
        for admission in report.admissions:
            if admission.width is None:
                continue
            band_v2 = v2.width_table.classify(admission.width)
            revision_rows.append(
                {
                    "token_id": admission.token_id,
                    "width": str(admission.width),
                    "v1_face_value": admission.face_value,
                    "v2_face_value": None if band_v2 is None else band_v2.face_value,
                    "v2_band": None
                    if band_v2 is None
                    else [str(band_v2.lo), str(band_v2.hi)],
                }
            )

    gauging_report = {
        "products": {"v1": v1.fingerprint, "v2": v2.fingerprint},
        "threefen_line": report_three.to_json(),
        "onefen_line": report_one.to_json(),
        "quarantine_line": report_quar.to_json(),
        "width_table_revision": revision_rows,
    }
    (out / "gauging_report.json").write_text(
        json.dumps(gauging_report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Ticket-office ledgers for the two mutually exclusive new-token outcomes.
    three_admissions = report_three.inventory()
    one_admissions = report_one.inventory()
    office_three = run_ledger(
        out / "boxoffice_threefen.db", v1, three_admissions, label="threefen"
    )
    office_one = run_ledger(
        out / "boxoffice_onefen.db", v1, one_admissions, label="onefen"
    )
    office_report = {
        "orders": ORDERS,
        "threefen_scenario": office_three,
        "onefen_scenario": office_one,
    }
    (out / "boxoffice_report.json").write_text(
        json.dumps(office_report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Offline, database-free re-check on a second computer.
    offline: dict[str, Any] = {"manifests": [], "admissions": [], "revision": revision_rows}
    for label, frames, report in (
        ("threefen", frames_three, report_three),
        ("onefen", frames_one, report_one),
    ):
        manifest_path = out / f"bag_manifest_{label}.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            inventory = [
                InventoryToken.from_admission(a.to_json()).to_json()
                for a in report.admitted()
            ]
            offline["manifests"].append(
                {
                    "label": label,
                    **recheck_manifest(manifest, inventory=inventory),
                }
            )
        for admission in report.admitted():
            offline["admissions"].append(
                {
                    "line": label,
                    **recheck_admission(v1, frames, admission.to_json()),
                }
            )
    (out / "offline_recheck.json").write_text(
        json.dumps(offline, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    console = {
        "threefen": [
            (a.token_id, a.status, str(a.width) if a.width is not None else None, a.face_value, a.reason)
            for a in report_three.admissions
        ],
        "onefen": [
            (a.token_id, a.status, str(a.width) if a.width is not None else None, a.face_value, a.reason)
            for a in report_one.admissions
        ],
        "quarantine": [
            (a.token_id, a.status, a.reason) for a in report_quar.admissions if a.status == "quarantined"
        ],
        "office_threefen_plan": office_three["simultaneous_plan"]["feasible"],
        "office_onefen_blocker": office_one["simultaneous_plan"]["blocker"],
        "offline_ok": all(m["ok"] for m in offline["manifests"])
        and all(a["ok"] for a in offline["admissions"]),
    }
    print(json.dumps(console, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
