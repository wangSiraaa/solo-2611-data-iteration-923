"""Differential and behavioural tests for the quote service.

Run with ``python3 -m unittest discover -s tests``.
"""

from __future__ import annotations

import random
import unittest

from basket_service import open_service
from basket_service.errors import BasketError
from basket_service.optimizer import solve
from basket_service.reference import exhaustive_best, expected_allocation
from basket_service.validation import allocate_report, validate_basket, validate_coupons


def normalise(units, coupons):
    u = validate_basket(units)
    c = validate_coupons(coupons, {x["sku"] for x in u})
    return u, c


class OptimizerTests(unittest.TestCase):
    def assert_matches_brute(self, units, coupons):
        u, c = normalise(units, coupons)
        masks, total = solve(u, c)
        bt, used_ids, unit_lists, mapping = exhaustive_best(u, c)
        self.assertEqual(total, bt, (units, coupons))
        mine = {cid: sorted(u[i]["unit_id"] for i in range(len(u)) if m >> i & 1) for cid, m in masks.items()}
        self.assertEqual(mine, mapping, (units, coupons))
        discounts = {x["coupon_id"]: x["D"] for x in c}
        self.assertEqual(allocate_report(u, masks, discounts), expected_allocation(u, mapping, discounts))

    def test_greedy_counterexample_where_biggest_first_is_worse(self):
        # Taking the single largest coupon (COMBINED, 190) consumes every unit
        # and blocks the two smaller coupons that together discount 240.
        basket = [
            {"unit_id": "a", "sku": "A", "price": 100},
            {"unit_id": "b", "sku": "B", "price": 100},
            {"unit_id": "c", "sku": "C", "price": 100},
            {"unit_id": "d", "sku": "D", "price": 100},
        ]
        coupons = [
            {"coupon_id": "COMBINED", "D": 190, "requires": {"A": 1, "B": 1, "C": 1, "D": 1}},
            {"coupon_id": "AB", "D": 120, "requires": {"A": 1, "B": 1}},
            {"coupon_id": "CD", "D": 120, "requires": {"C": 1, "D": 1}},
        ]
        u, c = normalise(basket, coupons)
        masks, total = solve(u, c)
        # Greedy "largest D first": COMBINED alone -> 400-190 = 210.
        # Optimal: AB + CD -> 400-240 = 160.
        self.assertEqual(total, 160)
        self.assertEqual(sorted(masks), ["AB", "CD"])
        self.assert_matches_brute(basket, coupons)

    def test_same_sku_different_prices(self):
        basket = [
            {"unit_id": "cheap", "sku": "A", "price": 30},
            {"unit_id": "dear", "sku": "A", "price": 300},
        ]
        coupons = [
            {"coupon_id": "one", "D": 250, "requires": {"A": 1}},
            {"coupon_id": "two", "D": 40, "requires": {"A": 1}},
        ]
        u, c = normalise(basket, coupons)
        masks, total = solve(u, c)
        # Both coupons would total 290, but the two SKU-A units cannot share a
        # coupon, so the optimum is just "one" on the dear unit (a 250 unit
        # price is needed to cover D=250).
        self.assertEqual(list(masks), ["one"])
        self.assertEqual(masks["one"], 1 << 1)  # 'dear' index 1
        self.assertEqual(total, 330 - 250)
        self.assert_matches_brute(basket, coupons)

    def test_multi_coupon_contention(self):
        basket = [
            {"unit_id": "a", "sku": "A", "price": 100},
            {"unit_id": "b", "sku": "B", "price": 100},
        ]
        coupons = [
            {"coupon_id": "AB1", "D": 120, "requires": {"A": 1, "B": 1}},
            {"coupon_id": "AB2", "D": 110, "requires": {"A": 1, "B": 1}},
        ]
        u, c = normalise(basket, coupons)
        masks, total = solve(u, c)
        self.assertEqual(sorted(masks), ["AB1"])
        self.assertEqual(total, 80)
        self.assert_matches_brute(basket, coupons)

    def test_two_level_tie_break_coupon_ids(self):
        basket = [
            {"unit_id": "a", "sku": "A", "price": 100},
            {"unit_id": "b", "sku": "B", "price": 100},
        ]
        coupons = [
            {"coupon_id": "ZZ", "D": 50, "requires": {"A": 1, "B": 1}},
            {"coupon_id": "AA", "D": 50, "requires": {"A": 1, "B": 1}},
        ]
        u, c = normalise(basket, coupons)
        masks, total = solve(u, c)
        self.assertEqual(list(masks), ["AA"])
        self.assert_matches_brute(basket, coupons)

    def test_two_level_tie_break_unit_ids(self):
        basket = [
            {"unit_id": "a", "sku": "A", "price": 100},
            {"unit_id": "b", "sku": "A", "price": 100},
            {"unit_id": "c", "sku": "A", "price": 100},
        ]
        # Two single-sku coupons, both D=50, one A each.  Equal totals with
        # many pairings; required answer: c1 -> smallest <unused> saw.
        coupons = [
            {"coupon_id": "c1", "D": 50, "requires": {"A": 1}},
            {"coupon_id": "c2", "D": 50, "requires": {"A": 1}},
        ]
        u, c = normalise(basket, coupons)
        masks, total = solve(u, c)
        # Signature compares (coupon_ids, (units for c1, units for c2)) so the
        # lexicographically smallest is c1->[a], c2->[b].
        self.assertEqual([u[i]["unit_id"] for i in range(3) if masks["c1"] >> i & 1], ["a"])
        self.assertEqual([u[i]["unit_id"] for i in range(3) if masks["c2"] >> i & 1], ["b"])
        self.assert_matches_brute(basket, coupons)

    def test_tie_break_compares_whole_coupon_id_list(self):
        # Same total discount (150) via one big coupon or two smaller ones.
        # Coupon-id list ["big"] is lexicographically smaller than ["x", "y"].
        basket = [
            {"unit_id": "a", "sku": "A", "price": 100},
            {"unit_id": "b", "sku": "A", "price": 100},
        ]
        coupons = [
            {"coupon_id": "big", "D": 150, "requires": {"A": 2}},
            {"coupon_id": "x", "D": 50, "requires": {"A": 1}},
            {"coupon_id": "y", "D": 100, "requires": {"A": 1}},
        ]
        u, c = normalise(basket, coupons)
        masks, total = solve(u, c)
        self.assertEqual(list(masks), ["big"])
        self.assertEqual(total, 50)
        self.assert_matches_brute(basket, coupons)

    def test_tie_break_same_coupon_set_evaluates_unit_list_order(self):
        # Coupon set is forced (only "p" can cover its 2-of-A requirement in a
        # cheap pairing), but two unit choices tie; smallest sorted unit list
        # wins component-wise.
        basket = [
            {"unit_id": "a", "sku": "A", "price": 100},
            {"unit_id": "b", "sku": "A", "price": 100},
            {"unit_id": "c", "sku": "A", "price": 100},
        ]
        coupons = [{"coupon_id": "p", "D": 150, "requires": {"A": 2}}]
        u, c = normalise(basket, coupons)
        masks, total = solve(u, c)
        chosen = sorted(u[i]["unit_id"] for i in range(len(u)) if masks["p"] >> i & 1)
        self.assertEqual(chosen, ["a", "b"])
        self.assert_matches_brute(basket, coupons)

    def test_tie_break_prefers_fewer_then_smaller_coupon_ids(self):
        basket = [{"unit_id": "a", "sku": "A", "price": 100}]
        coupons = [
            {"coupon_id": "A", "D": 30, "requires": {"A": 1}},
            {"coupon_id": "B", "D": 30, "requires": {"A": 1}},
        ]
        # Only one A unit, so at most one coupon can apply -> ["A"].
        u, c = normalise(basket, coupons)
        masks, total = solve(u, c)
        self.assertEqual(list(masks), ["A"])
        self.assertEqual(total, 70)

    def test_proportional_remainder_tie_break(self):
        # D=100 over prices 100/100/100 -> exact thirds. Floors 33 each, one cent
        # remains and goes to the smallest unit_id.
        basket = [
            {"unit_id": "z", "sku": "A", "price": 100},
            {"unit_id": "a", "sku": "A", "price": 100},
            {"unit_id": "m", "sku": "A", "price": 100},
        ]
        coupons = [{"coupon_id": "c", "D": 100, "requires": {"A": 3}}]
        u, c = normalise(basket, coupons)
        masks, total = solve(u, c)
        alloc = {row["unit_id"]: row["discount"] for row in allocate_report(u, masks, {"c": 100})}
        self.assertEqual(alloc, {"a": 34, "m": 33, "z": 33})
        self.assert_matches_brute(basket, coupons)

    def test_proportional_remainder_by_price(self):
        basket = [
            {"unit_id": "a", "sku": "A", "price": 1},
            {"unit_id": "b", "sku": "A", "price": 2},
            {"unit_id": "c", "sku": "A", "price": 3},
        ]
        coupons = [{"coupon_id": "c", "D": 4, "requires": {"A": 3}}]
        u, c = normalise(basket, coupons)
        masks, _ = solve(u, c)
        alloc = {row["unit_id"]: row["discount"] for row in allocate_report(u, masks, {"c": 4})}
        # Exact: 4*1/6=0.666, 4*2/6=1.333, 4*3/6=2.0 -> floors 0,1,2 +
        # one cent to largest remainder (a .666) -> 1,1,2.
        self.assertEqual(alloc, {"a": 1, "b": 1, "c": 2})
        self.assert_matches_brute(basket, coupons)

    def test_random_differential(self):
        rng = random.Random(20241117)
        for trial in range(250):
            n_units = rng.randint(0, 8)
            skus = [chr(65 + i) for i in range(rng.randint(1, 4))]
            basket = [
                {"unit_id": f"u{i}", "sku": rng.choice(skus), "price": rng.randint(1, 60)}
                for i in range(n_units)
            ]
            present = sorted({u["sku"] for u in basket})
            coupons = []
            for j in range(rng.randint(0, 5)):
                if not present:
                    break
                req = {}
                for sku in present:
                    have = sum(1 for u in basket if u["sku"] == sku)
                    if rng.random() < 0.5 and have:
                        req[sku] = rng.randint(1, have)
                if not req:
                    sku = rng.choice(present)
                    req[sku] = 1
                coupons.append({"coupon_id": f"c{j:02d}", "D": rng.randint(1, 80), "requires": req})
            with self.subTest(trial=trial):
                self.assert_matches_brute(basket, coupons)

    def test_empty_basket(self):
        u, c = normalise([], [])
        masks, total = solve(u, c)
        self.assertEqual((masks, total), ({}, 0))

    def test_invalid_inputs_rejected(self):
        svc = open_service(":memory:")
        basket = [{"unit_id": "a", "sku": "A", "price": 100}]
        snap = svc.publish_snapshot([{"sku": "A", "price": 100}], [])
        for bad in [
            [{"unit_id": "a", "sku": "A", "price": 0}],
            [{"unit_id": "a", "sku": "A", "price": -1}],
            [{"unit_id": "a", "sku": "A", "price": True}],
            [{"unit_id": "a", "sku": "A", "price": 1.5}],
            [{"unit_id": "a", "sku": "A", "price": 10}, {"unit_id": "a", "sku": "B", "price": 10}],
            [{"unit_id": "", "sku": "A", "price": 10}],
        ]:
            with self.assertRaises(BasketError):
                svc.quote(bad, [], "r", snap)
        for bad_coupons in [
            [{"coupon_id": "x", "D": 0, "requires": {"A": 1}}],
            [{"coupon_id": "x", "D": 10, "requires": {"B": 1}}],
            [{"coupon_id": "x", "D": 10, "requires": {}}],
            [{"coupon_id": "x", "D": 10, "requires": {"A": 0}}],
            [{"coupon_id": "x", "D": True, "requires": {"A": 1}}],
            [{"coupon_id": "x", "D": 10, "requires": {"A": 1}}, {"coupon_id": "x", "D": 20, "requires": {"A": 1}}],
        ]:
            with self.assertRaises(BasketError):
                svc.quote(basket, bad_coupons, "r2", None)


if __name__ == "__main__":
    unittest.main()
