"""Tests for observation package reconstruction and cutoff validity."""

from __future__ import annotations

import unittest

from basket_service import open_service
from basket_service.errors import BasketError
from basket_service.observations import ObservationService, PeriodSpec
from basket_service.scenarios import (
    CUTOFF_EARLY,
    CUTOFF_LATE,
    RETURN_AFTER,
    RETURN_BEFORE,
    build_worked_example,
    find_package,
    worked_periods,
)


def _service(db_path: str) -> ObservationService:
    return ObservationService(open_service(db_path).db, worked_periods())


class ReconstructionTests(unittest.TestCase):
    def test_packages_partition_every_sale_unit_with_frozen_paid(self):
        import tempfile

        path = tempfile.mktemp(suffix=".db")
        build_worked_example(path, b_return_time=None)
        svc = _service(path)
        audit = svc.rebuild_audit()
        self.assertTrue(audit["every_unit_packaged_exactly_once"])
        self.assertEqual(audit["package_count"], 4)

        p = find_package(svc, "S-P", "AB")
        self.assertEqual([(u.unit_id, u.sku, u.paid, u.coupon_id) for u in p.units],
                         [("a", "A", 3, "AB"), ("b", "B", 5, "AB")])
        self.assertEqual(p.paid_total, 8)
        self.assertEqual(p.promoted_count, 2)

        q = find_package(svc, "S-Q", None)
        self.assertEqual(q.member_count, 1)
        self.assertEqual(q.units[0].paid, 6)
        self.assertEqual(q.promoted_count, 0)

    def test_package_numbers_are_stable_across_reopen(self):
        import tempfile

        path = tempfile.mktemp(suffix=".db")
        build_worked_example(path, b_return_time=None)
        first = [p.package_id for p in _service(path).packages("early")]
        second = [p.package_id for p in _service(path).packages("early")]
        self.assertEqual(first, second)
        self.assertEqual(first, ["P001", "P002"])

    def test_returned_member_kills_whole_package_before_cutoff(self):
        import tempfile

        path = tempfile.mktemp(suffix=".db")
        build_worked_example(path, b_return_time=RETURN_BEFORE)
        svc = _service(path)
        p = find_package(svc, "S-P", "AB")
        self.assertFalse(p.is_complete_at(CUTOFF_EARLY))
        # The surviving A is still a member of the dead package and was never
        # split into a standalone observation.
        self.assertEqual({u.unit_id for u in p.units}, {"a", "b"})
        complete = {p.package_id for p in svc.complete_packages("early")}
        self.assertEqual(complete, {"P002"})

        audit = svc.audit_package(p.package_id)
        self.assertEqual(audit["status"], "INVALID_RETURNED_MEMBER")
        self.assertEqual(audit["returned_before_cutoff"][0]["unit_id"], "b")

    def test_return_after_cutoff_leaves_package_complete(self):
        import tempfile

        path = tempfile.mktemp(suffix=".db")
        build_worked_example(path, b_return_time=RETURN_AFTER)
        svc = _service(path)
        p = find_package(svc, "S-P", "AB")
        self.assertTrue(p.is_complete_at(CUTOFF_EARLY))
        audit = svc.audit_package(p.package_id)
        self.assertEqual(audit["status"], "COMPLETE")
        self.assertEqual(len(audit["returned_after_cutoff"]), 1)
        self.assertEqual(audit["returned_before_cutoff"], [])

    def test_cutoff_equality_counts_as_before(self):
        # Direct boundary check on a rebuilt package: return_time == cutoff.
        import tempfile

        path = tempfile.mktemp(suffix=".db")
        build_worked_example(path, b_return_time=CUTOFF_EARLY)
        svc = _service(path)
        p = find_package(svc, "S-P", "AB")
        self.assertFalse(p.is_complete_at(CUTOFF_EARLY))

    def test_same_basket_different_coupon_members_are_distinct_packages(self):
        # Two sales with the same units and same total, but different coupon
        # coverage, must reconstruct as different observations.
        import tempfile

        path = tempfile.mktemp(suffix=".db")
        svc = open_service(path)
        products = [{"sku": "A", "price": 5}, {"sku": "B", "price": 5}]
        snap1 = svc.publish_snapshot(products, [{"coupon_id": "AB", "D": 4, "requires": {"A": 1, "B": 1}}], snapshot_id="s1")
        snap2 = svc.publish_snapshot(
            products,
            [
                {"coupon_id": "A1", "D": 2, "requires": {"A": 1}},
                {"coupon_id": "B1", "D": 2, "requires": {"B": 1}},
            ],
            snapshot_id="s2",
        )
        basket = [{"unit_id": "a", "sku": "A", "price": 5}, {"unit_id": "b", "sku": "B", "price": 5}]
        svc.register_sale("S1", quote=svc.quote(basket, [], "q1", snap1))
        svc.register_sale("S2", quote=svc.quote(basket, [], "q2", snap2))
        # A sale cannot belong to both periods.
        with self.assertRaises(BasketError):
            ObservationService(svc.db, (
                PeriodSpec("early", ("S1", "S2"), CUTOFF_EARLY),
                PeriodSpec("late", ("S1",), CUTOFF_LATE),
            ))
        obs = ObservationService(svc.db, (
            PeriodSpec("early", ("S1",), CUTOFF_EARLY),
            PeriodSpec("late", ("S2",), CUTOFF_LATE),
        ))
        early_packages = obs.packages("early")
        late_packages = obs.packages("late")
        self.assertEqual(len(early_packages), 1)  # one AB coupon package
        self.assertEqual(len(late_packages), 2)  # two singleton coupon packages
        self.assertNotEqual(early_packages[0].content_hash, late_packages[0].content_hash)

    def test_unknown_package_and_period_audit_errors(self):
        import tempfile

        path = tempfile.mktemp(suffix=".db")
        build_worked_example(path, b_return_time=None)
        svc = _service(path)
        with self.assertRaises(BasketError) as ctx:
            svc.get_package("P999")
        self.assertEqual(ctx.exception.code, "UNKNOWN_PACKAGE")
        with self.assertRaises(BasketError):
            svc.packages("never")

    def test_period_is_rejected_above_24_packages(self):
        import tempfile

        from basket_service.observations import MAX_PACKAGES_PER_PERIOD

        path = tempfile.mktemp(suffix=".db")
        svc = open_service(path)
        # One sale with 26 unpromoted distinct units -> 26 standalone packages.
        basket = [{"unit_id": f"u{i:02d}", "sku": f"SKU{i}", "price": 10} for i in range(MAX_PACKAGES_PER_PERIOD + 2)]
        snap = svc.publish_snapshot(
            [{"sku": f"SKU{i}", "price": 10} for i in range(MAX_PACKAGES_PER_PERIOD + 2)],
            [],
            snapshot_id="big-plain",
        )
        svc.register_sale("S-big", quote=svc.quote(basket, [], "q-big", snap))
        small = [{"unit_id": "v0", "sku": "Z0", "price": 10}]
        snap_small = svc.publish_snapshot([{"sku": "Z0", "price": 10}], [], snapshot_id="small-plain")
        svc.register_sale("S-small", quote=svc.quote(small, [], "q-small", snap_small))
        with self.assertRaises(BasketError) as ctx:
            ObservationService(
                svc.db,
                (
                    PeriodSpec("early", ("S-big",), CUTOFF_EARLY),
                    PeriodSpec("late", ("S-small",), CUTOFF_LATE),
                ),
            )
        self.assertEqual(ctx.exception.code, "LIMIT_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
