"""Reference cross-check suite.

Every expectation below is derived *without* the production quote or return
functions: optimal prices come from ``reference.exhaustive_best`` (full
enumeration), per-unit allocations from ``reference.expected_allocation``
(``fractions.Fraction``), and refunds are summed from the stored original
per-unit paid amounts plus the raw return/adjustment records.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from basket_service import open_service
from basket_service.errors import BasketError
from basket_service.reference import exhaustive_best, expected_allocation, expected_return
from basket_service.validation import validate_basket, validate_coupons

PRODUCTS = [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}]
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


def brute_quote(units, coupons):
    u = validate_basket(units)
    c = validate_coupons(coupons, {x["sku"] for x in u})
    total, used, unit_lists, mapping = exhaustive_best(u, c)
    discounts = {x["coupon_id"]: x["D"] for x in c}
    allocation = expected_allocation(u, mapping, discounts)
    return total, mapping, allocation


class ReferenceCrossCheck(unittest.TestCase):
    def test_case_material_quotes(self):
        for coupons in (BEFORE, BEFORE + ADDED):
            svc = open_service(":memory:")
            snap = svc.publish_snapshot(PRODUCTS, coupons)
            quote = svc.quote(BASKET, [], "q", snap)
            total, mapping, allocation = brute_quote(BASKET, coupons)
            self.assertEqual(quote["total"], total)
            self.assertEqual(quote["assignments"], mapping)
            self.assertEqual(quote["allocation"], allocation)

    def test_two_sales_and_refunds_follow_bound_quote(self):
        svc = open_service(":memory:")
        old_snap = svc.publish_snapshot(PRODUCTS, BEFORE, snapshot_id="old")
        new_snap = svc.publish_snapshot(PRODUCTS, BEFORE + ADDED, snapshot_id="new")
        old_quote = svc.quote(BASKET, [], "q-old", old_snap)
        new_quote = svc.quote(BASKET, [], "q-new", new_snap)
        old_total, _, old_allocation = brute_quote(BASKET, BEFORE)
        new_total, _, new_allocation = brute_quote(BASKET, BEFORE + ADDED)
        self.assertEqual((old_quote["total"], new_quote["total"]), (old_total, new_total))
        old_sale = svc.register_sale("S-old", quote=old_quote)
        new_sale = svc.register_sale("S-new", quote=new_quote)
        old_paid = {row["unit_id"]: row["paid"] for row in old_allocation}
        new_paid = {row["unit_id"]: row["paid"] for row in new_allocation}
        self.assertEqual({u["unit_id"]: u["paid"] for u in old_sale["units"]}, old_paid)
        self.assertEqual({u["unit_id"]: u["paid"] for u in new_sale["units"]}, new_paid)
        # Return b from both sales with different grades.
        r_old = svc.return_units("r-old", "S-old", ["b"], {"b": "RESTOCK"})
        r_new = svc.return_units("r-new", "S-new", ["b"], {"b": "SCRAP"})
        self.assertEqual(r_old["refund_total"], *expected_return(old_paid, ["b"])[:1])
        self.assertEqual(r_new["refund_total"], *expected_return(new_paid, ["b"])[:1])
        self.assertNotEqual(r_old["refund_total"], r_new["refund_total"])

    def test_ten_unit_exhaustive_and_ledger_audit(self):
        basket = [
            {"unit_id": "u0", "sku": "A", "price": 100},
            {"unit_id": "u1", "sku": "A", "price": 30},
            {"unit_id": "u2", "sku": "B", "price": 70},
            {"unit_id": "u3", "sku": "B", "price": 45},
            {"unit_id": "u4", "sku": "C", "price": 90},
            {"unit_id": "u5", "sku": "C", "price": 20},
            {"unit_id": "u6", "sku": "D", "price": 60},
            {"unit_id": "u7", "sku": "D", "price": 10},
            {"unit_id": "u8", "sku": "E", "price": 55},
            {"unit_id": "u9", "sku": "F", "price": 5},
        ]
        coupons = [
            {"coupon_id": "c0", "D": 40, "requires": {"A": 1}},
            {"coupon_id": "c1", "D": 50, "requires": {"B": 1, "C": 1}},
            {"coupon_id": "c2", "D": 35, "requires": {"D": 2}},
            {"coupon_id": "c3", "D": 60, "requires": {"A": 2}},
            {"coupon_id": "c4", "D": 70, "requires": {"C": 1, "D": 1, "E": 1}},
        ]
        svc = open_service(":memory:")
        snap = svc.publish_snapshot(None, coupons)
        quote = svc.quote(basket, [], "q", snap)
        total, mapping, allocation = brute_quote(basket, coupons)
        self.assertEqual(quote["total"], total)
        self.assertEqual(quote["assignments"], mapping)
        self.assertEqual(quote["allocation"], allocation)
        # Full ledger audit against the independent paid table, not service code.
        sale = svc.register_sale("S", quote=quote)
        paid = {row["unit_id"]: row["paid"] for row in allocation}
        returned = ["u1", "u5", "u9"]
        grades = {"u1": "RESTOCK", "u5": "SCRAP", "u9": "RESTOCK"}
        result = svc.return_units("r", "S", returned, grades)
        expected_amount = sum(paid[u] for u in returned)
        self.assertEqual(result["refund_total"], expected_amount)
        self.assertEqual(result["cumulative_refund"], expected_amount)
        inv = svc.inventory()
        self.assertEqual(inv["RESTOCK"], {"A": 1, "F": 1})
        self.assertEqual(inv["SCRAP"], {"C": 1})
        remaining = sorted(set(paid) - set(returned))
        self.assertEqual(svc.get_sale("S")["remaining_refundable"], remaining)
        audit = svc.audit_sales()[0]
        self.assertEqual(audit["refunded_from_units"], expected_amount)
        self.assertEqual(audit["remaining_refundable"], remaining)

    def test_promotion_change_then_old_sale_return(self):
        svc = open_service(":memory:")
        old_snap = svc.publish_snapshot(PRODUCTS, BEFORE, snapshot_id="promo-1")
        q1 = svc.quote(BASKET, [], "q1", old_snap)
        sale = svc.register_sale("S1", quote=q1)
        old_paid = {u["unit_id"]: u["paid"] for u in sale["units"]}
        # New promotion version; must not affect the old sale.
        svc.publish_snapshot(PRODUCTS, BEFORE + ADDED, snapshot_id="promo-2")
        svc.quote(BASKET, [], "q2", "promo-2")
        r = svc.return_units("r1", "S1", ["a", "c"], {"a": "RESTOCK", "c": "SCRAP"})
        self.assertEqual(r["refund_total"], old_paid["a"] + old_paid["c"])
        self.assertEqual(r["cumulative_refund"], old_paid["a"] + old_paid["c"])
        self.assertEqual(r["remaining_refundable"], ["b"])

    def test_inventory_and_refund_are_one_transaction(self):
        # Force a mid-return failure by pre-returning the second unit in the
        # same request; no refund or stock move may survive.
        svc = open_service(":memory:")
        snap = svc.publish_snapshot(PRODUCTS, BEFORE)
        q = svc.quote(BASKET, [], "q", snap)
        svc.register_sale("S", quote=q)
        svc.return_units("pre", "S", ["c"], {"c": "RESTOCK"})
        with self.assertRaises(BasketError):
            svc.return_units("both", "S", ["a", "c"], {"a": "RESTOCK", "c": "RESTOCK"})
        sale = svc.get_sale("S")
        self.assertEqual(sale["remaining_refundable"], ["a", "b"])
        inv = svc.inventory()
        self.assertEqual(inv["RESTOCK"], {"C": 1})
        self.assertNotIn("A", inv["RESTOCK"])
        self.assertNotIn("A", inv["SCRAP"])

    def test_adjustment_cannot_resurrect_returns(self):
        svc = open_service(":memory:")
        snap = svc.publish_snapshot(PRODUCTS, BEFORE)
        q = svc.quote(BASKET, [], "q", snap)
        svc.register_sale("S", quote=q)
        svc.return_units("r", "S", ["b"], {"b": "RESTOCK"})
        svc.adjust_inventory("stock-up", "B", 10, "RESTOCK")
        with self.assertRaises(BasketError) as ctx:
            svc.return_units("r2", "S", ["b"], {"b": "RESTOCK"})
        self.assertEqual(ctx.exception.code, "UNIT_ALREADY_RETURNED")
        self.assertEqual(svc.inventory()["RESTOCK"].get("B", 0), 11)

    def test_round_trip_json_plan(self):
        plan = {
            "steps": [
                {"name": "snap", "op": "publish_snapshot", "products": PRODUCTS, "coupons": BEFORE + ADDED, "snapshot_id": "s1"},
                {"name": "q", "op": "quote", "basket": BASKET, "coupons": [], "request_id": "q1", "snapshot_id": "$snap"},
                {"name": "sale", "op": "register_sale", "sale_id": "S1", "quote": "$q"},
                {"name": "ret", "op": "return", "request_id": "r1", "sale_id": "S1", "unit_ids": ["b"], "grade": {"b": "RESTOCK"}},
                {"name": "dup", "op": "return", "request_id": "r1", "sale_id": "S1", "unit_ids": ["b"], "grade": {"b": "RESTOCK"}},
                {"op": "return", "request_id": "r2", "sale_id": "S1", "unit_ids": ["a", "b"], "grade": {"a": "SCRAP", "b": "SCRAP"}, "expect_error": "UNIT_ALREADY_RETURNED"},
                {"name": "adj", "op": "adjust_inventory", "event_id": "x1", "sku": "A", "quantity": 2, "bucket": "RESTOCK"},
                {"name": "inv", "op": "inventory"},
                {"name": "audit", "op": "audit_inventory"},
            ]
        }
        svc = open_service(":memory:")
        result = svc.run(plan)
        self.assertTrue(result["ok"])
        self.assertEqual(result["steps"][4]["result"], result["steps"][3]["result"])
        self.assertEqual(result["steps"][7]["result"], {"RESTOCK": {"A": 2, "B": 1}, "SCRAP": {}})
        self.assertEqual(result["steps"][8]["result"], {"A": {"RESTOCK": 2, "SCRAP": 0}, "B": {"RESTOCK": 1, "SCRAP": 0}})


if __name__ == "__main__":
    unittest.main()
