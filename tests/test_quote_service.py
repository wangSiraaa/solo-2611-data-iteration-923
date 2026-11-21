"""Tests for snapshots, quote replay/conflicts and quote immutability."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from basket_service import open_service
from basket_service.errors import BasketError

BASKET = [
    {"unit_id": "a", "sku": "A", "price": 100},
    {"unit_id": "b", "sku": "B", "price": 100},
    {"unit_id": "c", "sku": "C", "price": 100},
]
BEFORE = [{"coupon_id": "AB", "D": 150, "requires": {"A": 1, "B": 1}}]
ADDED = [
    {"coupon_id": "A", "D": 80, "requires": {"A": 1}},
    {"coupon_id": "BC", "D": 100, "requires": {"B": 1, "C": 1}},
]


class QuoteServiceTests(unittest.TestCase):
    def test_case_material_before_and_added(self):
        svc = open_service(":memory:")
        before = svc.publish_snapshot(
            [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}],
            BEFORE,
        )
        q_before = svc.quote(BASKET, [], "q-before", before)
        self.assertEqual(q_before["total"], 150)  # AB discount 150
        added = svc.publish_snapshot(
            [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}],
            BEFORE + ADDED,
        )
        self.assertNotEqual(before, added)
        q_added = svc.quote(BASKET, [], "q-added", added)
        # A(80) + BC(100) = 180 -> 120 beats AB(150) -> 150.
        self.assertEqual(q_added["total"], 120)
        # Old quote is unchanged by the new snapshot.
        self.assertEqual(svc.get_quote("q-before"), q_before)
        self.assertEqual(q_before["total"], 150)

    def test_replay_and_request_conflict(self):
        svc = open_service(":memory:")
        snap = svc.publish_snapshot(None, BEFORE)
        q1 = svc.quote(BASKET, [], "r1", snap)
        q2 = svc.quote(list(reversed(BASKET)), [], "r1", snap)
        self.assertEqual(q1, q2)
        with self.assertRaises(BasketError) as ctx:
            svc.quote(BASKET[:2], [], "r1", snap)
        self.assertEqual(ctx.exception.code, "REQUEST_CONFLICT")

    def test_snapshot_conflict(self):
        svc = open_service(":memory:")
        sid = svc.publish_snapshot(None, BEFORE, snapshot_id="fixed")
        self.assertEqual(svc.publish_snapshot(None, BEFORE, snapshot_id="fixed"), "fixed")
        with self.assertRaises(BasketError) as ctx:
            svc.publish_snapshot(None, ADDED, snapshot_id="fixed")
        self.assertEqual(ctx.exception.code, "SNAPSHOT_CONFLICT")

    def test_quote_binds_snapshot_not_ad_hoc_coupons(self):
        svc = open_service(":memory:")
        snap = svc.publish_snapshot(None, BEFORE)
        with self.assertRaises(BasketError) as ctx:
            svc.quote(BASKET, ADDED, "q", snap)
        self.assertEqual(ctx.exception.code, "SNAPSHOT_CONFLICT")

    def test_quote_does_not_register_sale_or_inventory(self):
        svc = open_service(":memory:")
        snap = svc.publish_snapshot(None, BEFORE)
        svc.quote(BASKET, [], "q", snap)
        self.assertEqual(svc.inventory(), {"RESTOCK": {}, "SCRAP": {}})
        with self.assertRaises(BasketError) as ctx:
            svc.get_sale("nope")
        self.assertEqual(ctx.exception.code, "UNKNOWN_SALE")

    def test_unknown_snapshot(self):
        svc = open_service(":memory:")
        with self.assertRaises(BasketError) as ctx:
            svc.quote(BASKET, [], "q", "missing")
        self.assertEqual(ctx.exception.code, "UNKNOWN_SNAPSHOT")

    def test_empty_basket_quote(self):
        svc = open_service(":memory:")
        snap = svc.publish_snapshot(None, [])
        q = svc.quote([], [], "empty", snap)
        self.assertEqual(q["total"], 0)
        self.assertEqual(q["allocation"], [])
        self.assertEqual(q["assignments"], {})

    def test_reopen_persists_quotes_and_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "svc.db")
            svc = open_service(path)
            snap = svc.publish_snapshot(None, BEFORE + ADDED)
            q = svc.quote(BASKET, [], "q", snap)
            del svc
            reopened = open_service(path)
            self.assertEqual(reopened.get_quote("q"), q)
            self.assertEqual(reopened.get_snapshot(snap)["coupons"], q["coupons"])
            self.assertEqual(reopened.quote(BASKET, [], "q", snap), q)


if __name__ == "__main__":
    unittest.main()
