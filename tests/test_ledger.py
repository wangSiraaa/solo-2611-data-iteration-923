"""Ledger tests: binding, per-unit refunds, idempotency, overlaps, inventory."""

from __future__ import annotations

import sqlite3
import threading
import tempfile
import unittest
from pathlib import Path

from basket_service import open_service
from basket_service.errors import BasketError
from basket_service.reference import expected_return

BASKET = [
    {"unit_id": "a", "sku": "A", "price": 100},
    {"unit_id": "b", "sku": "B", "price": 100},
    {"unit_id": "c", "sku": "C", "price": 100},
]
PROMO_AB = [{"coupon_id": "AB", "D": 150, "requires": {"A": 1, "B": 1}}]
PROMO_ADDED = [
    {"coupon_id": "A", "D": 80, "requires": {"A": 1}},
    {"coupon_id": "BC", "D": 100, "requires": {"B": 1, "C": 1}},
]
PRODUCTS = [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}]


def setup_two_sales(path=":memory:"):
    svc = open_service(path)
    old_snap = svc.publish_snapshot(PRODUCTS, PROMO_AB, snapshot_id="snap-old")
    new_snap = svc.publish_snapshot(PRODUCTS, PROMO_AB + PROMO_ADDED, snapshot_id="snap-new")
    old_quote = svc.quote(BASKET, [], "q-old", old_snap)
    new_quote = svc.quote(BASKET, [], "q-new", new_snap)
    return svc, old_quote, new_quote


class LedgerTests(unittest.TestCase):
    def test_two_sales_follow_their_own_quotes(self):
        svc, old_quote, new_quote = setup_two_sales()
        self.assertEqual(old_quote["total"], 150)
        self.assertEqual(new_quote["total"], 120)
        sale_old = svc.register_sale("S-old", quote=old_quote)
        sale_new = svc.register_sale("S-new", quote=new_quote)
        old_paid = {u["unit_id"]: u["paid"] for u in sale_old["units"]}
        new_paid = {u["unit_id"]: u["paid"] for u in sale_new["units"]}
        self.assertNotEqual(old_paid["b"], new_paid["b"])
        # Return b on the old sale -> refund old paid; new promotion is irrelevant.
        r = svc.return_units("r-old-b", "S-old", ["b"], {"b": "RESTOCK"})
        expected_total, expected_units = expected_return(old_paid, ["b"])
        self.assertEqual(r["refund_total"], expected_total)
        self.assertEqual({i["unit_id"]: i["refund"] for i in r["items"]}, expected_units)
        # Return b on the new sale -> new paid.
        r2 = svc.return_units("r-new-b", "S-new", ["b"], {"b": "SCRAP"})
        expected_total2, expected_units2 = expected_return(new_paid, ["b"])
        self.assertEqual(r2["refund_total"], expected_total2)
        self.assertEqual({i["unit_id"]: i["refund"] for i in r2["items"]}, expected_units2)

    def test_sale_rebinding_old_vs_new_quote(self):
        svc, old_quote, new_quote = setup_two_sales()
        svc.register_sale("S1", quote=old_quote)
        # Same body replays.
        repeated = svc.register_sale("S1", quote=old_quote)
        self.assertTrue(repeated["repeated"])
        # A different quote under the same sale_id is rejected.
        with self.assertRaises(BasketError) as ctx:
            svc.register_sale("S1", quote=new_quote)
        self.assertEqual(ctx.exception.code, "SALE_CONFLICT")
        # Binding explicitly to the old identity is legitimate, not a retry of
        # the new request.
        svc.register_sale("S2", quote=old_quote)
        r = svc.return_units("r2", "S2", ["b"], {"b": "RESTOCK"})
        old_paid = next(u["paid"] for u in svc.get_sale("S1")["units"] if u["unit_id"] == "b")
        self.assertEqual(r["refund_total"], old_paid)

    def test_sale_id_unknown_quote_and_mismatch(self):
        svc, old_quote, new_quote = setup_two_sales()
        with self.assertRaises(BasketError) as ctx:
            svc.register_sale("S0", request_id="q-missing")
        self.assertEqual(ctx.exception.code, "UNKNOWN_QUOTE")
        with self.assertRaises(BasketError) as ctx:
            svc.register_sale("S0", quote=old_quote, snapshot_id=new_quote["snapshot_id"])
        self.assertEqual(ctx.exception.code, "QUOTE_MISMATCH")
        with self.assertRaises(BasketError) as ctx:
            svc.register_sale("S0", quote=old_quote, request_id="q-new")
        self.assertEqual(ctx.exception.code, "QUOTE_MISMATCH")

    def test_tampered_quote_payload_rejected(self):
        svc, old_quote, _ = setup_two_sales()
        tampered = {**old_quote, "total": old_quote["total"] - 1}
        with self.assertRaises(BasketError) as ctx:
            svc.register_sale("S1", quote=tampered)
        self.assertEqual(ctx.exception.code, "INVALID_QUOTE")

    def test_return_without_quote_lookup(self):
        # The ledger may consume a quote object it has never stored, which is
        # how it talks to an independent quote issuer.
        svc, old_quote, _ = setup_two_sales()
        external = {k: v for k, v in old_quote.items()}
        del_external = dict(external)
        # Simulate an independent quote source: same request id but this ledger
        # never saw it. Use a fresh database to prove lookup is not required.
        fresh = open_service(":memory:")
        sale = fresh.register_sale("S1", quote=del_external)
        self.assertEqual(sale["total"], old_quote["total"])

    def test_return_idempotency_and_conflict(self):
        svc, old_quote, _ = setup_two_sales()
        svc.register_sale("S1", quote=old_quote)
        first = svc.return_units("r1", "S1", ["b"], {"b": "RESTOCK"})
        again = svc.return_units("r1", "S1", ["b"], {"b": "RESTOCK"})
        # Replays the first result even after another unit is returned later.
        svc.return_units("r2", "S1", ["a"], {"a": "SCRAP"})
        replay = svc.return_units("r1", "S1", ["b"], {"b": "RESTOCK"})
        self.assertEqual(first, again)
        self.assertEqual(first, replay)
        with self.assertRaises(BasketError) as ctx:
            svc.return_units("r1", "S1", ["c"], {"c": "RESTOCK"})
        self.assertEqual(ctx.exception.code, "REQUEST_CONFLICT")

    def test_unit_cannot_be_returned_twice(self):
        svc, old_quote, _ = setup_two_sales()
        svc.register_sale("S1", quote=old_quote)
        svc.return_units("r1", "S1", ["b"], {"b": "RESTOCK"})
        with self.assertRaises(BasketError) as ctx:
            svc.return_units("r2", "S1", ["b"], {"b": "SCRAP"})
        self.assertEqual(ctx.exception.code, "UNIT_ALREADY_RETURNED")

    def test_overlapping_request_is_all_or_nothing(self):
        svc, old_quote, _ = setup_two_sales()
        svc.register_sale("S1", quote=old_quote)
        svc.return_units("r1", "S1", ["b"], {"b": "RESTOCK"})
        with self.assertRaises(BasketError) as ctx:
            svc.return_units("r2", "S1", ["a", "b"], {"a": "SCRAP", "b": "SCRAP"})
        self.assertEqual(ctx.exception.code, "UNIT_ALREADY_RETURNED")
        # "a" must still be refundable with no inventory movement from r2.
        sale = svc.get_sale("S1")
        self.assertIn("a", sale["remaining_refundable"])
        inv = svc.inventory()
        self.assertEqual(inv["RESTOCK"].get("B", 0), 1)
        self.assertNotIn("A", inv["SCRAP"])
        r = svc.return_units("r3", "S1", ["a"], {"a": "SCRAP"})
        self.assertEqual(r["refund_total"], next(u["paid"] for u in sale["units"] if u["unit_id"] == "a"))
        self.assertEqual(svc.inventory()["SCRAP"].get("A", 0), 1)

    def test_partial_return_then_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ledger.db")
            svc, old_quote, _ = setup_two_sales(path)
            svc.register_sale("S1", quote=old_quote)
            svc.return_units("r1", "S1", ["b"], {"b": "RESTOCK"})
            del svc
            reopened = open_service(path)
            sale = reopened.get_sale("S1")
            self.assertEqual(sale["remaining_refundable"], ["a", "c"])
            replay = reopened.get_return("r1")
            self.assertTrue(replay["repeated"])
            r2 = reopened.return_units("r2", "S1", ["c"], {"c": "SCRAP"})
            expected = next(u["paid"] for u in sale["units"] if u["unit_id"] == "c")
            self.assertEqual(r2["refund_total"], expected)
            inv = reopened.inventory()
            self.assertEqual(inv, {"RESTOCK": {"B": 1}, "SCRAP": {"C": 1}})

    def test_grade_validation(self):
        svc, old_quote, _ = setup_two_sales()
        svc.register_sale("S1", quote=old_quote)
        for bad in [
            (["b"], {}),
            (["b"], {"b": "GOOD"}),
            (["b"], {"b": "RESTOCK", "a": "SCRAP"}),
            (["b", "b"], {"b": "RESTOCK"}),
            ([], {}),
        ]:
            with self.assertRaises(BasketError):
                svc.return_units("rx", "S1", bad[0], bad[1])
        with self.assertRaises(BasketError) as ctx:
            svc.return_units("rx", "S1", ["z"], {"z": "RESTOCK"})
        self.assertEqual(ctx.exception.code, "UNIT_INELIGIBLE")
        # No partial state from failed grade validation.
        self.assertEqual(svc.get_sale("S1")["remaining_refundable"], ["a", "b", "c"])

    def test_inventory_adjustment_independent_and_idempotent(self):
        svc, old_quote, _ = setup_two_sales()
        svc.register_sale("S1", quote=old_quote)
        first = svc.adjust_inventory("adj1", "A", 3, "RESTOCK")
        again = svc.adjust_inventory("adj1", "A", 3, "RESTOCK")
        self.assertEqual(first["inventory"], again["inventory"])
        self.assertTrue(again["repeated"])
        with self.assertRaises(BasketError) as ctx:
            svc.adjust_inventory("adj1", "A", 4, "RESTOCK")
        self.assertEqual(ctx.exception.code, "INVENTORY_CONFLICT")
        # Adjustment must not resurrect or alter refundable amounts.
        self.assertEqual(svc.get_sale("S1")["remaining_refundable"], ["a", "b", "c"])
        svc.adjust_inventory("adj2", "A", 2, "SCRAP")
        self.assertEqual(svc.inventory(), {"RESTOCK": {"A": 3}, "SCRAP": {"A": 2}})
        # Returning a unit still refunds the same original paid amount.
        r = svc.return_units("r1", "S1", ["a"], {"a": "SCRAP"})
        self.assertEqual(r["refund_total"], next(u["paid"] for u in svc.get_sale("S1")["units"] if u["unit_id"] == "a"))

    def test_concurrent_overlapping_returns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "race.db")
            svc, old_quote, _ = setup_two_sales(path)
            svc.register_sale("S1", quote=old_quote)
            results = []
            barrier = threading.Barrier(2)
            errors = []

            def worker(request_id):
                client = open_service(path)
                try:
                    barrier.wait()
                    results.append(client.return_units(request_id, "S1", ["b"], {"b": "RESTOCK"}))
                except BasketError as exc:
                    errors.append(exc.code)

            threads = [threading.Thread(target=worker, args=(f"r{i}",)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(results), 1)
            self.assertEqual(errors, ["UNIT_ALREADY_RETURNED"])
            final = open_service(path)
            inv = final.inventory()
            self.assertEqual(inv["RESTOCK"].get("B", 0), 1)

    def test_audit_recomputes_from_raw_events(self):
        svc, old_quote, _ = setup_two_sales()
        svc.register_sale("S1", quote=old_quote)
        svc.return_units("r1", "S1", ["b"], {"b": "RESTOCK"})
        svc.adjust_inventory("adj", "A", 5, "RESTOCK")
        audit = svc.audit_inventory()
        self.assertEqual(audit["B"]["RESTOCK"], 1)
        self.assertEqual(audit["A"]["RESTOCK"], 5)
        sales = svc.audit_sales()
        self.assertEqual(len(sales), 1)
        self.assertEqual(sales[0]["returned_units"], ["b"])
        self.assertEqual(sales[0]["remaining_refundable"], ["a", "c"])


if __name__ == "__main__":
    unittest.main()
