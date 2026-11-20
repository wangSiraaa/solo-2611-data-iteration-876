"""Batch bagging, ledger atomicity and offline manifest re-check tests."""

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

from bitcatalog.boxoffice import (  # noqa: E402
    BLOCKER_BATCH,
    BLOCKER_DENOMINATION,
    BLOCKER_STOCK,
    BagOrder,
    BoxOfficeError,
    BoxOfficeLedger,
    InventoryToken,
    plan_batch,
    recheck_manifest,
)


def token(serial, face, width="2", product_sha256="deadbeef"):
    return InventoryToken(
        serial=serial,
        face_value=face,
        width=Fraction(width),
        width_table_version="width-v1",
        product_sha256=product_sha256,
        frame_event_ids=(f"{serial}-L", f"{serial}-T"),
    )


def order(oid, amount, max_tokens, values):
    return {"order_id": oid, "amount": amount, "max_tokens": max_tokens, "accepted_values": values}


class SolverTests(unittest.TestCase):
    def test_narrative_two_amount2_orders_only_one_bag(self):
        stock = [token("L1", 1), token("L2", 1)]
        plan = plan_batch(stock, [order("A", 2, 2, [1, 2]), order("B", 2, 2, [1, 2])])
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.blocker.code, BLOCKER_BATCH)

    def test_narrative_threefen_makes_whole_batch(self):
        stock = [token("L1", 1), token("L2", 1), token("T1", 3, "4")]
        plan = plan_batch(
            stock,
            [order("A", 3, 2, [1, 3]), order("B", 2, 2, [1, 2])],
        )
        self.assertTrue(plan.feasible)
        bags = {b.order_id: b for b in plan.bags}
        self.assertEqual(bags["A"].total, 3)
        self.assertEqual([l.serial for l in bags["A"].lines], ["T1"])
        self.assertEqual(bags["B"].total, 2)
        self.assertEqual({l.serial for l in bags["B"].lines}, {"L1", "L2"})
        # every physical token used at most once across the batch
        serials = [l.serial for b in plan.bags for l in b.lines]
        self.assertEqual(len(serials), len(set(serials)))

    def test_narrative_onefen_cannot_fill_both(self):
        stock = [token("L1", 1), token("L2", 1), token("T2", 1)]
        plan = plan_batch(
            stock,
            [order("A", 3, 2, [1, 3]), order("B", 2, 2, [1, 2])],
        )
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.blocker.code, BLOCKER_DENOMINATION)
        self.assertEqual(plan.blocker.order_id, "A")

    def test_denomination_unreachable_even_with_unlimited_stock(self):
        plan = plan_batch(
            [token("t", 2)],
            [order("A", 3, 5, [2])],
        )
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.blocker.code, BLOCKER_DENOMINATION)

    def test_insufficient_stock_distinct_from_denomination(self):
        # amount 4 reachable with four 1s but only two exist.
        plan = plan_batch(
            [token("a", 1), token("b", 1)],
            [order("A", 4, 4, [1])],
        )
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.blocker.code, BLOCKER_STOCK)

    def test_max_tokens_limit_is_enforced(self):
        plan = plan_batch(
            [token(f"t{i}", 1) for i in range(10)],
            [order("A", 3, 2, [1])],
        )
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.blocker.code, BLOCKER_DENOMINATION)

    def test_each_bag_fillable_alone_but_not_together(self):
        # Both bags need the single three-fen token to reach their amounts
        # within the token cap; alone each works, jointly they contend.
        stock = [token("only3", 3), token("x", 1), token("y", 1)]
        orders = [
            order("A", 3, 1, [3]),
            order("B", 4, 2, [1, 3]),
        ]
        alone_a = plan_batch(stock, [orders[0]])
        alone_b = plan_batch(stock, [orders[1]])
        self.assertTrue(alone_a.feasible and alone_b.feasible)
        joint = plan_batch(stock, orders)
        self.assertFalse(joint.feasible)
        self.assertEqual(joint.blocker.code, BLOCKER_BATCH)

    def test_duplicate_serial_rejected(self):
        with self.assertRaises(BoxOfficeError):
            plan_batch([token("x", 1), token("x", 1)], [order("A", 1, 1, [1])])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "office.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _product(self):
        from bitcatalog.gauging import GaugingProduct

        return GaugingProduct.build(
            gate_spacing="1",
            width_table={"version": "width-v1", "bands": [
                {"face_value": 1, "lo": "1", "hi": "3"},
                {"face_value": 3, "lo": "3", "hi": "5"},
            ]},
        )

    def test_confirm_deducts_atomically_and_keeps_gauging_basis(self):
        product = self._product()
        ledger = BoxOfficeLedger(self.db)
        ledger.register_gauging_product(product)
        sha = product.fingerprint
        ledger.admit_tokens([
            token("L1", 1, product_sha256=sha),
            token("L2", 1, product_sha256=sha),
            token("T1", 3, "4", product_sha256=sha),
        ])
        self.assertEqual(ledger.stock_counts(), {1: 2, 3: 1})
        plan = ledger.plan([order("A", 3, 2, [1, 3]), order("B", 2, 2, [1, 2])])
        self.assertTrue(plan.feasible)
        manifest = ledger.confirm(plan, bag_batch_id="batch-1", created_at="2026-11-20T08:30:00Z")
        self.assertEqual(ledger.stock_counts(), {})
        # each line retains the width basis and source frames
        for bag in manifest["plan"]["bags"]:
            for line in bag["lines"]:
                self.assertEqual(line["product_sha256"], product.fingerprint)
                self.assertTrue(line["frame_event_ids"])
        # a second confirmation cannot reuse the same physical tokens
        again = ledger.plan([order("C", 3, 2, [1, 3])])
        self.assertFalse(again.feasible)
        with self.assertRaises(BoxOfficeError):
            ledger.confirm(plan, bag_batch_id="batch-1", created_at="2026-11-20T09:00:00Z")
        ledger.close()

    def test_reopen_and_offline_manifest_recheck(self):
        product = self._product()
        sha = product.fingerprint
        tokens = [token("L1", 1, product_sha256=sha), token("T1", 3, "4", product_sha256=sha)]
        with BoxOfficeLedger(self.db) as ledger:
            ledger.register_gauging_product(product)
            ledger.admit_tokens(tokens)
            plan = ledger.plan([order("A", 4, 2, [1, 3])])
            self.assertTrue(plan.feasible)
            ledger.confirm(plan, bag_batch_id="batch-x", created_at="2026-11-20T08:30:00Z")
            manifest_path = ledger.export_manifest("batch-x", Path(self.tmp.name) / "bag.json")
        with BoxOfficeLedger(self.db) as ledger:
            self.assertEqual(ledger.stock_counts(), {})
            self.assertEqual(ledger.manifests(), ["batch-x"])
        manifest = json.loads(Path(manifest_path).read_text())
        inventory = [t.to_json() for t in tokens]
        verdict = recheck_manifest(manifest, inventory=inventory)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["bags_checked"], 1)
        # tampering with a line breaks the content hash
        manifest["plan"]["bags"][0]["lines"][0]["face_value"] = 99
        bad = recheck_manifest(manifest, inventory=inventory)
        self.assertFalse(bad["ok"])
        self.assertIn("hash", bad["problems"][0])

    def test_infeasible_plan_cannot_be_confirmed(self):
        product = self._product()
        with BoxOfficeLedger(self.db) as ledger:
            ledger.register_gauging_product(product)
            ledger.admit_tokens([token("L1", 1, product_sha256=product.fingerprint)])
            plan = ledger.plan([order("A", 5, 2, [1, 3])])
            self.assertFalse(plan.feasible)
            with self.assertRaises(BoxOfficeError) as ctx:
                ledger.confirm(plan, bag_batch_id="nope", created_at="2026-11-20T08:30:00Z")
            self.assertEqual(ctx.exception.code, "infeasible_plan")
            self.assertEqual(ledger.stock_counts(), {1: 1})


if __name__ == "__main__":
    unittest.main()
