"""Tests for balanced two-period selection, objectives and the ratio report."""

from __future__ import annotations

import itertools
import tempfile
import unittest
from fractions import Fraction
from typing import Dict, List, Mapping, Sequence, Tuple

from basket_service import open_service
from basket_service.errors import BasketError
from basket_service.observations import ObservationService, PeriodSpec, UnitFact
from basket_service.sampling import build_report, enumerate_choices

CUTOFF = "2026-06-30T00:00:00+00:00"


def _package(package_id: str, period: str, sku_paid: Sequence[Tuple[str, int]], *, promoted: bool):
    units = tuple(
        UnitFact(unit_id=f"{package_id}-{i}", sku=sku, paid=paid,
                 coupon_id="C" if promoted else None, return_time=None)
        for i, (sku, paid) in enumerate(sku_paid)
    )
    from basket_service.observations import ObservationPackage

    return ObservationPackage(
        package_id=package_id,
        package_no=int(package_id[1:]),
        period=period,
        sale_id=f"sale-{package_id}",
        coupon_id="C" if promoted else None,
        units=units,
        paid_total=sum(u.paid for u in units),
        content_hash=package_id,
    )


def _service_with(early, late) -> ObservationService:
    svc = open_service(":memory:")
    periods = (
        PeriodSpec("early", tuple(sorted({p.sale_id for p in early})), CUTOFF),
        PeriodSpec("late", tuple(sorted({p.sale_id for p in late})), CUTOFF),
    )
    obs = ObservationService.__new__(ObservationService)
    obs.db = svc.db
    obs.periods = periods
    obs._by_period = {"early": list(early), "late": list(late)}
    obs._by_id = {p.package_id: p for p in early + late}
    return obs


def _brute_force(obs: ObservationService, maps, quotas):
    """Independent exhaustive reference for the three lexicographic objectives.

    Returns ``(balanced_units, promoted_total, early_ids, late_ids)``.
    """

    def options(period):
        packages = obs.complete_packages(period)
        mapping = maps[period]
        universe = tuple(sorted(set().union(*(set(m.values()) for m in maps.values()))))
        result = []
        for r in range(len(packages) + 1):
            for subset in itertools.combinations(packages, r):
                counts: Dict[str, int] = {}
                promoted = 0
                for package in subset:
                    for category, count in package.category_vector(mapping):
                        counts[category] = counts.get(category, 0) + count
                    promoted += package.promoted_count
                if any(counts.get(c, 0) > quotas[period].get(c, 0) for c in set(counts) | set(quotas[period])):
                    continue
                # Full-length vector over the shared universe, matching production.
                vector = tuple((c, counts.get(c, 0)) for c in universe)
                result.append((vector, promoted, tuple(p.package_id for p in subset)))
        return result

    best = None
    for ev, ep, eids in options("early"):
        for lv, lp, lids in options("late"):
            if ev != lv:
                continue
            units = sum(c for _, c in ev)
            promoted = ep + lp
            joint = tuple(sorted(eids + lids))
            if best is None:
                best = (units, promoted, eids, lids)
                continue
            if units > best[0] or (units == best[0] and promoted > best[1]) or (
                units == best[0] and promoted == best[1] and list(joint) < list(tuple(sorted(best[2] + best[3])))
            ):
                best = (units, promoted, eids, lids)
    return best


class SelectionObjectiveTests(unittest.TestCase):
    def setUp(self):
        # Early: one promoted AB package and two singleton A alternatives.
        self.early = [
            _package("P001", "early", [("A", 3), ("B", 5)], promoted=True),
            _package("P002", "early", [("A", 6)], promoted=False),
            _package("P003", "early", [("A", 4)], promoted=False),
        ]
        self.late = [
            _package("P004", "late", [("A", 5)], promoted=False),
            _package("P005", "late", [("B", 7)], promoted=False),
        ]
        self.maps = {"early": {"A": "A", "B": "B"}, "late": {"A": "A", "B": "B"}}
        self.quotas = {"early": {"A": 1, "B": 1}, "late": {"A": 1, "B": 1}}

    def test_max_units_then_promoted_then_id_list(self):
        obs = _service_with(self.early, self.late)
        report = build_report(obs, self.maps, self.quotas, versions=("v1", "v1"))
        self.assertEqual(report.balanced_units, 2)
        self.assertEqual(report.early.package_ids, ("P001",))
        self.assertEqual(report.late.package_ids, ("P004", "P005"))
        self.assertEqual(report.promoted_units_total, 2)
        self.assertEqual(report.raw_ratio, (12, 8))
        self.assertEqual(report.paid_ratio, Fraction(12, 8))

    def test_tie_break_picks_smallest_package_ids_regardless_of_price(self):
        # Two interchangeable singleton-A packages in each period with the same
        # vector and promotion flag; the cheaper/dearer price must not matter.
        early = [_package("P001", "early", [("A", 9)], promoted=False),
                 _package("P002", "early", [("A", 1)], promoted=False)]
        late = [_package("P003", "late", [("A", 8)], promoted=False),
                _package("P004", "late", [("A", 2)], promoted=False)]
        obs = _service_with(early, late)
        maps = {"early": {"A": "A"}, "late": {"A": "A"}}
        quotas = {"early": {"A": 1}, "late": {"A": 1}}
        report = build_report(obs, maps, quotas)
        self.assertEqual(report.early.package_ids, ("P001",))
        self.assertEqual(report.late.package_ids, ("P003",))

    def test_promotion_objective_beats_identical_unit_count(self):
        # Same vectors on both sides, but a promoted and an unpromoted early
        # package both realise (A:1); the promoted one must win at level 2.
        early = [_package("P001", "early", [("A", 2)], promoted=False),
                 _package("P002", "early", [("A", 2)], promoted=True)]
        late = [_package("P003", "late", [("A", 5)], promoted=False)]
        obs = _service_with(early, late)
        maps = {"early": {"A": "A"}, "late": {"A": "A"}}
        report = build_report(obs, maps, {"early": {"A": 1}, "late": {"A": 1}})
        self.assertEqual(report.early.package_ids, ("P002",))
        self.assertEqual(report.promoted_units_total, 1)

    def test_quota_caps_balanced_count(self):
        obs = _service_with(self.early, self.late)
        quotas = {"early": {"A": 1, "B": 0}, "late": {"A": 1, "B": 0}}
        report = build_report(obs, self.maps, quotas)
        self.assertEqual(report.balanced_units, 1)
        self.assertEqual(dict(report.category_counts), {"A": 1, "B": 0})
        self.assertEqual(report.to_dict()["category_counts"], {"A": 1})

    def test_empty_report_when_nothing_balances(self):
        early = [_package("P001", "early", [("A", 3)], promoted=True)]
        late = [_package("P002", "late", [("B", 7)], promoted=False)]
        obs = _service_with(early, late)
        maps = {"early": {"A": "A"}, "late": {"B": "B"}}
        report = build_report(obs, maps, {"early": {"A": 1}, "late": {"B": 1}})
        self.assertTrue(report.is_empty)
        self.assertEqual(report.balanced_units, 0)
        self.assertTrue(report.ratio_undefined)
        self.assertEqual(report.to_dict()["paid_ratio_text"], "UNDEFINED (early paid total is zero)")

    def test_zero_early_total_is_undefined_not_zero_division(self):
        early = [_package("P001", "early", [("A", 0)], promoted=True)]
        late = [_package("P002", "late", [("A", 5)], promoted=False)]
        obs = _service_with(early, late)
        maps = {"early": {"A": "A"}, "late": {"A": "A"}}
        report = build_report(obs, maps, {"early": {"A": 1}, "late": {"A": 1}})
        self.assertFalse(report.is_empty)
        self.assertTrue(report.ratio_undefined)
        self.assertIsNone(report.paid_ratio)

    def test_matches_independent_brute_force_on_random_instances(self):
        import random

        rng = random.Random(20261122)
        categories = ("c1", "c2", "c3")
        skus = {"c1": ("A", "B"), "c2": ("C",), "c3": ("D", "E")}
        for trial in range(40):
            early, late = [], []
            for period, sink, prefix in (("early", early, "E"), ("late", late, "L")):
                count = rng.randint(1, 6)
                for index in range(count):
                    members = []
                    for _ in range(rng.randint(1, 3)):
                        cat = rng.choice(categories)
                        members.append((rng.choice(skus[cat]), rng.randint(0, 9)))
                    sink.append(_package(f"{prefix}{index:03d}", period, members, promoted=rng.random() < 0.5))
            obs = _service_with(early, late)
            sku_to_cat = {sku: cat for cat, cat_skus in skus.items() for sku in cat_skus}
            maps = {"early": dict(sku_to_cat), "late": dict(sku_to_cat)}
            quotas = {period: {cat: rng.randint(0, 3) for cat in categories} for period in ("early", "late")}
            report = build_report(obs, maps, quotas)
            expected = _brute_force(obs, maps, quotas)
            self.assertIsNotNone(expected, f"trial {trial} lost the empty fallback")
            exp_units, exp_promoted, exp_early, exp_late = expected
            self.assertEqual(report.balanced_units, exp_units, f"units trial {trial}")
            self.assertEqual(report.promoted_units_total, exp_promoted, f"promoted trial {trial}")
            self.assertEqual(report.early.package_ids, tuple(sorted(exp_early)), f"early ids trial {trial}")
            self.assertEqual(report.late.package_ids, tuple(sorted(exp_late)), f"late ids trial {trial}")


class LimitValidationTests(unittest.TestCase):
    def _obs(self):
        early = [_package("P001", "early", [("A", 1)], promoted=False)]
        late = [_package("P002", "late", [("A", 2)], promoted=False)]
        return _service_with(early, late)

    def test_quota_above_twelve_rejected(self):
        obs = self._obs()
        maps = {"early": {"A": "c"}, "late": {"A": "c"}}
        with self.assertRaises(BasketError) as ctx:
            build_report(obs, maps, {"early": {"c": 13}, "late": {"c": 1}})
        self.assertEqual(ctx.exception.code, "LIMIT_EXCEEDED")

    def test_missing_quota_for_mapped_category_rejected(self):
        obs = self._obs()
        maps = {"early": {"A": "c"}, "late": {"A": "c"}}
        with self.assertRaises(BasketError) as ctx:
            build_report(obs, maps, {"early": {}, "late": {"c": 1}})
        self.assertEqual(ctx.exception.code, "CATEGORY_ERROR")

    def test_more_than_eight_categories_rejected(self):
        from basket_service.observations import validate_category_map
        mapping = {f"S{i}": f"c{i}" for i in range(9)}
        quotas = {f"c{i}": 1 for i in range(9)}
        with self.assertRaises(BasketError) as ctx:
            validate_category_map(mapping, quotas)
        self.assertEqual(ctx.exception.code, "LIMIT_EXCEEDED")

    def test_zero_quota_excludes_category_but_keeps_others(self):
        early = [
            _package("P001", "early", [("A", 1)], promoted=False),
            _package("P002", "early", [("B", 1)], promoted=False),
        ]
        late = [_package("P003", "late", [("A", 2)], promoted=False)]
        obs = _service_with(early, late)
        maps = {"early": {"A": "cA", "B": "cB"}, "late": {"A": "cA", "B": "cB"}}
        report = build_report(
            obs, maps,
            {"early": {"cA": 1, "cB": 0}, "late": {"cA": 1, "cB": 0}},
        )
        self.assertEqual(report.to_dict()["category_counts"], {"cA": 1})


class EnumerationTests(unittest.TestCase):
    def test_choice_dp_keeps_best_subset_per_vector(self):
        packages = [
            _package("P001", "early", [("A", 1)], promoted=False),
            _package("P002", "early", [("A", 1)], promoted=True),
        ]
        states = enumerate_choices(packages, {"A": "A"}, ("A",), {"A": 1})
        candidate = states[(1,)]
        self.assertEqual(candidate.ids, ("P002",))  # promoted wins for the same counts
        self.assertEqual(candidate.promoted, 1)

    def test_choice_dp_never_reuses_a_package_within_one_round(self):
        # Two identical singleton vectors: without the per-round snapshot the
        # second package could be folded in twice and reach (A:2) by itself.
        packages = [
            _package("P001", "early", [("A", 1)], promoted=True),
            _package("P002", "early", [("A", 1)], promoted=False),
        ]
        states = enumerate_choices(packages, {"A": "A"}, ("A",), {"A": 2})
        doubled = states[(2,)]
        self.assertEqual(doubled.ids, ("P001", "P002"))
        self.assertEqual(doubled.promoted, 1)
        for candidate in states.values():
            self.assertEqual(len(candidate.ids), len(set(candidate.ids)))


if __name__ == "__main__":
    unittest.main()
