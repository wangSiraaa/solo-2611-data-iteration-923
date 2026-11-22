"""End-to-end tests over real, reopenable SQLite sales/refund databases."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from basket_service.scenarios import (
    CATEGORY_V2,
    DISTINCT_MAPS,
    QUOTAS_AB,
    RETURN_AFTER,
    RETURN_BEFORE,
    build_distinct_vector_fixture,
    build_worked_example,
    open_worked_example,
    run_distinct_vector_report,
    run_worked_reports,
    worked_category_maps,
)


class WorkedExampleReportsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clean_db = str(Path(self.tmp) / "clean.db")
        self.returned_db = str(Path(self.tmp) / "returned.db")
        build_worked_example(self.clean_db, b_return_time=None)
        build_worked_example(self.returned_db, b_return_time=RETURN_BEFORE)

    def test_clean_report_is_p_against_r_and_s_index_12_over_8(self):
        payload = run_worked_reports(self.clean_db)
        report = payload["report"]
        self.assertFalse(report["is_empty"])
        self.assertEqual(report["balanced_units"], 2)
        self.assertEqual(report["early"]["packages"], ["P001"])
        self.assertEqual(report["late"]["packages"], ["P003", "P004"])
        self.assertEqual(report["early"]["paid_total"], 8)
        self.assertEqual(report["late"]["paid_total"], 12)
        self.assertEqual(report["paid_ratio_raw_integer_totals"], {"numerator": 12, "denominator": 8})
        self.assertEqual(report["paid_ratio_late_over_early"], {"numerator": 3, "denominator": 2})
        self.assertEqual(report["package_count_difference_late_minus_early"], 1)
        # Frozen paid, never current tags.
        self.assertEqual(report["early"]["category_counts"], {"A": 1, "B": 1})

    def test_early_b_return_before_cutoff_kills_p_and_falls_back_to_q(self):
        payload = run_worked_reports(self.returned_db)
        report = payload["report"]
        self.assertEqual(report["balanced_units"], 1)
        self.assertEqual(report["early"]["packages"], ["P002"])
        self.assertEqual(report["late"]["packages"], ["P003"])
        self.assertEqual(report["paid_ratio_raw_integer_totals"], {"numerator": 5, "denominator": 6})
        invalid = payload["audit_early"]["invalid"]
        self.assertEqual([row["package_id"] for row in invalid], ["P001"])
        self.assertEqual(invalid[0]["returned_before_cutoff"][0]["unit_id"], "b")
        # P's surviving A is not re-offered as a standalone cheap package.
        early_ids = {p["package_id"] for p in payload["audit_early"]["packages"]}
        self.assertEqual(early_ids, {"P001", "P002"})

    def test_return_after_cutoff_keeps_first_report_byte_identical(self):
        after_db = str(Path(self.tmp) / "after.db")
        build_worked_example(after_db, b_return_time=RETURN_AFTER)
        from basket_service.db import canonical_json

        self.assertEqual(
            canonical_json(run_worked_reports(after_db)["report"]),
            canonical_json(run_worked_reports(self.clean_db)["report"]),
        )

    def test_reclassification_makes_p_unbalanceable(self):
        payload = run_worked_reports(self.clean_db, category_version=CATEGORY_V2["late"])
        report = payload["report"]
        # B-other has quota 0, so late P004 (the B-only package) is excluded and
        # P cannot be balanced; only the A-only fallback remains.
        self.assertEqual(report["balanced_units"], 1)
        self.assertEqual(report["early"]["packages"], ["P002"])
        self.assertEqual(report["late"]["packages"], ["P003"])
        self.assertEqual(report["late"]["category_version"], CATEGORY_V2["late"])

    def test_reopen_database_reproduces_packages_and_report(self):
        first = run_worked_reports(self.clean_db)["report"]
        reopened = open_worked_example(self.clean_db)
        second = self._report(reopened)
        self.assertEqual(first, second)

    @staticmethod
    def _report(service):
        from basket_service.sampling import build_report

        return build_report(
            service,
            worked_category_maps(),
            QUOTAS_AB,
            versions=("cat-v1", "cat-v1"),
        ).to_dict()


class DistinctVectorFixtureTests(unittest.TestCase):
    def test_all_vectors_pairwise_distinct_and_only_combination_balances(self):
        path = str(Path(tempfile.mkdtemp()) / "distinct.db")
        build_distinct_vector_fixture(path)
        payload = run_distinct_vector_report(path)
        vectors = [tuple(sorted(v["vector"].items())) for v in payload["vectors"]]
        self.assertEqual(len(vectors), len(set(vectors)))
        report = payload["report"]
        # No single package has a matching counterpart: each side must combine.
        self.assertGreater(len(report["early"]["packages"]), 1)
        self.assertGreater(len(report["late"]["packages"]), 1)
        self.assertEqual(
            report["early"]["category_counts"],
            report["late"]["category_counts"],
        )
        self.assertEqual(report["balanced_units"], 3)
        self.assertEqual(report["category_counts"], {"catX": 2, "catY": 1})


if __name__ == "__main__":
    unittest.main()
