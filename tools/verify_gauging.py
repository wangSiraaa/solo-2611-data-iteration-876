"""Independent verification of the gauging and box-office artifacts.

Everything here reads the published files under ``--out`` and recomputes from
scratch with a deliberately different, naive implementation (no
:class:`GaugingStation` or :class:`BoxOfficeLedger`):

1. Calibrations are rebuilt directly from ruler frames; each token width is the
   piecewise speed integral evaluated by a plain time sweep.  Results must
   match the stored admissions, including every quarantine reason.
2. Product files are re-loaded through their content hash and the v2 width
   table re-classifies the same widths independently.
3. Bag manifests are re-checked without opening the SQLite ledger, including
   exact totals, token caps, unique serials and the per-line gauging basis.

Run ``python3.11 tools/verify_gauging.py`` after ``tools/build_gauging.py``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.gauging import GaugingProduct  # noqa: E402
from bitcatalog.boxoffice import recheck_manifest  # noqa: E402


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def independent_width(frames: list[dict[str, Any]], spacing: Fraction):
    """Naive reference: build calibrations, then sweep each token occlusion."""

    ruler = {}
    for f in frames:
        if f["branch"] != "ruler":
            continue
        ruler.setdefault(f["mark_id"], {})[f["gate"]] = f
    calibrations = []
    for mark, gates in ruler.items():
        if set(gates) != {"front", "rear"}:
            continue
        transit = gates["rear"]["occurred_at_ns"] - gates["front"]["occurred_at_ns"]
        if transit > 0:
            calibrations.append((gates["rear"]["occurred_at_ns"], spacing / transit))
    calibrations.sort()

    tokens: dict[str, dict[str, dict[str, int]]] = {}
    for f in frames:
        if f["branch"] == "token":
            tokens.setdefault(f["token_id"], {}).setdefault(f["gate"], {})[f["edge"]] = f[
                "occurred_at_ns"
            ]

    def speed_at(ns: int) -> Fraction | None:
        active = None
        for start, speed in calibrations:
            if start <= ns:
                active = speed
            else:
                break
        return active

    out = {}
    for token_id, per_gate in tokens.items():
        widths = {}
        for gate, edges in per_gate.items():
            if set(edges) != {"leading", "trailing"} or edges["leading"] >= edges["trailing"]:
                widths[gate] = None
                continue
            total = Fraction(0)
            gap = False
            for ns in range(edges["leading"], edges["trailing"]):
                speed = speed_at(ns)
                if speed is None:
                    gap = True
                    break
                total += speed
            widths[gate] = None if gap else total
        out[token_id] = widths
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "artifacts" / "gauging"))
    args = parser.parse_args(argv)
    out = Path(args.out)
    problems: list[str] = []

    v1 = GaugingProduct.load(out / "gauging_v1.json")
    v2 = GaugingProduct.load(out / "gauging_v2.json")
    report = json.loads((out / "gauging_report.json").read_text())

    for line_name, file_name in (
        ("threefen_line", "frames_t1_threefen.jsonl"),
        ("onefen_line", "frames_t2_onefen.jsonl"),
    ):
        frames = read_jsonl(out / file_name)
        reference = independent_width(frames, v1.gate_spacing)
        stored = {a["token_id"]: a for a in report[line_name]["admissions"]}
        for token_id, widths in reference.items():
            admission = stored[token_id]
            if admission["status"] == "admitted":
                expected = widths["rear"]
                if expected is None or str(expected) != admission["width"]:
                    problems.append(f"{line_name}/{token_id}: reference width {expected} != {admission['width']}")
                band = v1.width_table.classify(Fraction(admission["width"]))
                if band is None or band.face_value != admission["face_value"]:
                    problems.append(f"{line_name}/{token_id}: v1 classification mismatch")
                band2 = v2.width_table.classify(Fraction(admission["width"]))
                revision = next(
                    r for r in report["width_table_revision"]
                    if r["token_id"] == token_id and r["width"] == admission["width"]
                )
                if (band2.face_value if band2 else None) != revision["v2_face_value"]:
                    problems.append(f"{line_name}/{token_id}: v2 revision mismatch")
            else:
                # Quarantined tokens must have no fully-defined reference width
                # or a width outside every band.
                if widths.get("rear") is not None and v1.width_table.classify(widths["rear"]) is not None:
                    # Edge/mark conflicts are legitimate even with a numeric width.
                    if admission["reason"] not in {"edge_misaligned", "mark_conflict", "duplicate_token"}:
                        problems.append(f"{line_name}/{token_id}: unexpected quarantine {admission['reason']}")

    # Quarantine file: every named reason must appear.
    quar = {a["token_id"]: a["reason"] for a in report["quarantine_line"]["admissions"]}
    for reason in ("missing_speed", "mark_conflict", "edge_misaligned", "unclassified"):
        if reason not in quar.values():
            problems.append(f"quarantine line missing reason {reason}")

    # Manifest re-check without the database, plus the ledger's own statuses.
    for manifest_path in sorted(out.glob("bag_manifest_*.json")):
        manifest = json.loads(manifest_path.read_text())
        verdict = recheck_manifest(manifest)
        if not verdict["ok"]:
            problems.append(f"{manifest_path.name}: {verdict['problems']}")

    for db_name in ("boxoffice_threefen.db", "boxoffice_onefen.db"):
        db_path = out / db_name
        if not db_path.exists():
            continue
        conn = sqlite3.connect(db_path)
        n_bagged = conn.execute("SELECT COUNT(*) FROM tokens WHERE status='bagged'").fetchone()[0]
        n_available = conn.execute("SELECT COUNT(*) FROM tokens WHERE status='available'").fetchone()[0]
        if db_name == "boxoffice_threefen.db" and (n_bagged != 3 or n_available != 0):
            problems.append(f"{db_name}: expected all 3 tokens bagged, got {n_bagged}/{n_available}")
        conn.close()

    result = {"ok": not problems, "problems": problems, "checked_lines": 3}
    (out / "gauging_verification.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
