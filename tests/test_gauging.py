"""Gauging product, calibration line and width-integration tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

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
    GaugingError,
    GaugingProduct,
    GaugingStation,
    recheck_admission,
)
from tools.belt_kinematics import front_edge_time  # noqa: E402

WIDTH_V1 = {
    "version": "width-v1",
    "bands": [
        {"face_value": 1, "lo": "1", "hi": "3"},
        {"face_value": 3, "lo": "3", "hi": "5"},
    ],
}

SLOW = Fraction(1, 3)
FAST = Fraction(1)
SWITCH_NS = 24
PIECES_A = [(12, SLOW), (SWITCH_NS, FAST)]
PIECES_B = [(12, SLOW)]


def product(ttl=None):
    return GaugingProduct.build(
        gate_spacing="1",
        valid_gate=GATE_REAR,
        valid_edge=EDGE_LEADING,
        calibration_ttl_ns=ttl,
        width_table=WIDTH_V1,
    )


def ruler(eid, ns, gate, mark):
    return {
        "frame_event_id": eid,
        "occurred_at_ns": ns,
        "gate": gate,
        "branch": BRANCH_RULER,
        "mark_id": mark,
    }


def token(eid, ns, gate, edge, token_id):
    return {
        "frame_event_id": eid,
        "occurred_at_ns": ns,
        "gate": gate,
        "branch": BRANCH_TOKEN,
        "edge": edge,
        "token_id": token_id,
    }


def selfcheck(eid, ns, gate="rear"):
    return {"frame_event_id": eid, "occurred_at_ns": ns, "gate": gate, "branch": BRANCH_SELFCHECK}


def kinematic_token(token_id, rear_leading, rear_trailing, pieces=PIECES_A):
    """Both-gate edge frames consistent with the piecewise belt motion."""

    front_leading = front_edge_time(pieces, rear_leading, Fraction(1))
    front_trailing = front_edge_time(pieces, rear_trailing, Fraction(1))
    return [
        token(f"{token_id}-A-L", front_leading, GATE_FRONT, EDGE_LEADING, token_id),
        token(f"{token_id}-A-T", front_trailing, GATE_FRONT, EDGE_TRAILING, token_id),
        token(f"{token_id}-B-L", rear_leading, GATE_REAR, EDGE_LEADING, token_id),
        token(f"{token_id}-B-T", rear_trailing, GATE_REAR, EDGE_TRAILING, token_id),
    ]


# Slow calibration: 3 ns transit over unit spacing -> speed 1/3 per ns.
# Fast calibration: 1 ns transit -> speed 1 per ns; rear event at SWITCH_NS.
def speed_line_frames():
    return [
        ruler("m0-A", 9, GATE_FRONT, "m0"),
        ruler("m0-B", 12, GATE_REAR, "m0"),
        ruler("m1-A", 20, GATE_FRONT, "m1"),
        ruler("m1-B", 23, GATE_REAR, "m1"),
        ruler("m2-A", SWITCH_NS - 1, GATE_FRONT, "m2"),
        ruler("m2-B", SWITCH_NS, GATE_REAR, "m2"),
    ]


def slow_line_frames():
    return [f for f in speed_line_frames() if f["mark_id"] != "m2"]


class ProductTests(unittest.TestCase):
    def test_content_hash_detects_tampering_and_roundtrip(self):
        p = product()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "g.json"
            p.save(path)
            loaded = GaugingProduct.load(path)
            self.assertEqual(loaded.fingerprint, p.fingerprint)
            text = path.read_text()
            doc = json.loads(text)
            doc["content"]["gate_spacing"] = "2"
            path.write_text(json.dumps(doc))
            with self.assertRaises(GaugingError) as ctx:
                GaugingProduct.load(path)
            self.assertEqual(ctx.exception.code, "bad_product_hash")

    def test_band_overlap_and_duplicate_face_rejected(self):
        with self.assertRaises(GaugingError) as ctx:
            GaugingProduct.build(
                gate_spacing="1",
                width_table={"version": "x", "bands": [
                    {"face_value": 1, "lo": "1", "hi": "3"},
                    {"face_value": 2, "lo": "2", "hi": "4"},
                ]},
            )
        self.assertEqual(ctx.exception.code, "band_overlap")
        with self.assertRaises(GaugingError):
            GaugingProduct.build(
                gate_spacing="1",
                width_table={"version": "x", "bands": [
                    {"face_value": 1, "lo": "1", "hi": "2"},
                    {"face_value": 1, "lo": "2", "hi": "3"},
                ]},
            )

    def test_half_open_boundaries_and_selector_bridge(self):
        p = product()
        self.assertEqual(p.width_table.classify(Fraction(3)).face_value, 3)
        self.assertEqual(p.width_table.classify(Fraction(1)).face_value, 1)
        self.assertIsNone(p.width_table.classify(Fraction(5)))
        msg = p.bridge_message()
        self.assertEqual(msg["selector"]["name"], "branch")
        self.assertEqual({s["name"] for s in msg["signals"]},
                         {"ruler_mark", "token_ref", "selfcheck_seq"})
        # The bridge message is itself a valid bitcatalog product.
        from bitcatalog.catalog import build_product

        build_product([msg])


class CalibrationAndWidthTests(unittest.TestCase):
    def test_piecewise_width_four_is_threefen(self):
        frames = speed_line_frames()
        # rear occlusion [18, 26): slow 6 ns (width 2) + fast 2 ns (width 2) = 4
        frames += kinematic_token("T", 18, 26)
        frames.append(selfcheck("sc", 22))
        report = GaugingStation(product()).process(frames)
        admission = next(a for a in report.admissions if a.token_id == "T")
        self.assertEqual(admission.status, "admitted")
        self.assertEqual(admission.width, Fraction(4))
        self.assertEqual(admission.face_value, 3)
        # m0 and m1 share the slow speed; m2 brings the fast piece.
        self.assertEqual(len(admission.segments), 3)
        self.assertEqual([s.speed for s in admission.segments], [SLOW, SLOW, FAST])
        self.assertEqual(
            [s.calibration_mark for s in admission.segments], ["m0", "m1", "m2"]
        )

    def test_selfcheck_does_not_replace_calibration_width_two_onefen(self):
        frames = slow_line_frames()
        frames += kinematic_token("T", 18, 24, pieces=PIECES_B)
        frames += [selfcheck("sc1", 22), selfcheck("sc2", 23)]
        report = GaugingStation(product()).process(frames)
        admission = next(a for a in report.admissions if a.token_id == "T")
        self.assertEqual(admission.status, "admitted")
        self.assertEqual(admission.width, Fraction(2))
        self.assertEqual(admission.face_value, 1)
        # m1 re-calibrates with the same slow speed, so the interval splits at
        # 23 ns but every piece still carries the slow calibration speed.
        self.assertTrue(all(seg.speed == SLOW for seg in admission.segments))
        self.assertEqual(
            {seg.calibration_mark for seg in admission.segments}, {"m0", "m1"}
        )

    def test_missing_speed_before_first_calibration_isolates(self):
        frames = [
            token("T-A-L", 1, GATE_FRONT, EDGE_LEADING, "T"),
            token("T-A-T", 4, GATE_FRONT, EDGE_TRAILING, "T"),
            token("T-B-L", 2, GATE_REAR, EDGE_LEADING, "T"),
            token("T-B-T", 5, GATE_REAR, EDGE_TRAILING, "T"),
            ruler("m0-A", 9, GATE_FRONT, "m0"),
            ruler("m0-B", 12, GATE_REAR, "m0"),
        ]
        report = GaugingStation(product()).process(frames)
        admission = report.admissions[0]
        self.assertEqual(admission.status, "quarantined")
        self.assertEqual(admission.reason, "missing_speed")

    def test_ttl_expiry_isolates_but_valid_until_cutoff(self):
        p = product(ttl=10)
        frames = [
            ruler("m0-A", 9, GATE_FRONT, "m0"),
            ruler("m0-B", 12, GATE_REAR, "m0"),
        ]
        # occlusion ending exactly at ttl boundary 22: valid [18, 22)
        frames += kinematic_token("T1", 18, 22, pieces=[(12, SLOW)])
        # occlusion crossing the ttl boundary -> no in-force speed after 22
        frames += kinematic_token("T2", 20, 24, pieces=[(12, SLOW)])
        by_id = {a.token_id: a for a in GaugingStation(p).process(frames).admissions}
        self.assertEqual(by_id["T1"].status, "admitted")
        self.assertEqual(by_id["T2"].reason, "missing_speed")

    def test_mark_conflict_duplicate_gate_observation_isolates_token(self):
        frames = speed_line_frames() + [
            ruler("mX-A", 25, GATE_FRONT, "mX"),
            ruler("mX-B1", 26, GATE_REAR, "mX"),
            ruler("mX-B2", 27, GATE_REAR, "mX"),
        ]
        frames += kinematic_token("T", 25, 27)
        report = GaugingStation(product()).process(frames)
        conflict = next(c for c in report.mark_conflicts if c.mark_id == "mX")
        self.assertEqual(conflict.reason, "duplicate_gate_observation")
        self.assertEqual(report.admissions[0].reason, "mark_conflict")

    def test_single_gate_mark_is_conflict(self):
        frames = [
            ruler("m0-A", 9, GATE_FRONT, "m0"),
        ]
        frames += kinematic_token("T", 18, 24)
        report = GaugingStation(product()).process(frames)
        self.assertEqual(report.mark_conflicts[0].reason, "single_gate_observation")

    def test_edge_misalignment_missing_and_duplicate_edge(self):
        base = speed_line_frames()
        good = kinematic_token("T", 18, 24)
        missing = base + [f for f in good if f["frame_event_id"] != "T-B-T"]
        self.assertEqual(GaugingStation(product()).process(missing).admissions[0].reason, "edge_misaligned")
        duplicate = base + good + [token("T-B-L2", 19, GATE_REAR, EDGE_LEADING, "T")]
        self.assertEqual(GaugingStation(product()).process(duplicate).admissions[0].reason, "edge_misaligned")

    def test_cross_gate_width_disagreement_isolates(self):
        frames = speed_line_frames()
        good = kinematic_token("T", 18, 24)
        # move the front trailing edge so the front-gate width no longer matches
        tampered = [
            dict(f, occurred_at_ns=f["occurred_at_ns"] - 3) if f["frame_event_id"] == "T-A-T" else f
            for f in good
        ]
        frames += tampered
        admission = GaugingStation(product()).process(frames).admissions[0]
        self.assertEqual(admission.reason, "edge_misaligned")

    def test_unclassified_width_isolates(self):
        # Fast-only interval [24, 30) at speed 1 gives width 6, outside both
        # v1 bands [1,3) and [3,5).
        frames = speed_line_frames() + kinematic_token("T", 24, 30)
        admission = GaugingStation(product()).process(frames).admissions[0]
        self.assertEqual(admission.reason, "unclassified")
        self.assertEqual(admission.width, Fraction(6))

    def test_late_ruler_never_revises_prior_admission(self):
        p = product()
        station = GaugingStation(p)
        first_frames = speed_line_frames() + kinematic_token("T", 18, 26)
        admitted = station.process(first_frames).admissions[0]
        self.assertEqual(admitted.width, Fraction(4))
        # A ruler frame older than the watermark arrives late and never
        # becomes a calibration that could rewrite the admission.
        late = station.process([ruler("mLATE-A", 10, GATE_FRONT, "mLATE")])
        self.assertIn("mLATE-A", late.late_frames)
        self.assertEqual(late.calibrations, ())
        # A fresh, timely frame carrying an already-admitted serial cannot
        # enter the catalog a second time; the duplicate guard takes
        # precedence over the new frame's own (missing-speed) verdict.
        retry = station.process(kinematic_token("T", 27, 29))
        self.assertEqual(retry.admissions[0].reason, "duplicate_token")

    def test_bad_frames_are_rejected_not_silently_dropped(self):
        report = GaugingStation(product()).process(
            [
                {"frame_event_id": "bad1", "occurred_at_ns": 1, "gate": "rear", "branch": "token"},
                {"frame_event_id": "bad2", "occurred_at_ns": 1, "gate": "moon", "branch": "selfcheck"},
                selfcheck("ok", 2),
            ]
        )
        codes = {f["code"] for f in report.rejected_frames}
        self.assertEqual(codes, {"bad_frame"})
        self.assertEqual(len(report.rejected_frames), 2)

    def test_offline_recheck_recomputes_width_basis(self):
        frames = speed_line_frames() + kinematic_token("T", 18, 26)
        p = product()
        admission = GaugingStation(p).process(frames).admissions[0].to_json()
        verdict = recheck_admission(p, frames, admission)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["recomputed_width"], "4")
        other = GaugingProduct.build(gate_spacing="2", width_table=WIDTH_V1)
        mismatch = recheck_admission(other, frames, admission)
        self.assertFalse(mismatch["ok"])
        self.assertEqual(mismatch["reason"], "product_mismatch")


if __name__ == "__main__":
    unittest.main()
