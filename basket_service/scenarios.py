"""Reopenable end-to-end scenarios built on the real sales/refund ledger.

Every number in the worked example is created exactly the way production data
is: a published immutable snapshot produces a complete quote, the ledger binds a
real sale to that quote (freezing each unit's paid amount), and a real return
request records the refund and its timestamp.  The databases are ordinary
SQLite files, so the observation service can reopen them after the builder
process exits.

Deterministic timestamps replace the wall-clock ones right after each write,
which makes "return before / after the data cutoff" reproducible without
mocking the clock.

Worked example (amounts are integer cents):

* early period: sale ``S-P`` is a set coupon covering A+B with paid 3 and 5;
  sale ``S-Q`` is an unpromoted single A paid 6;
* late period: sale ``S-R`` is a single A paid 5, sale ``S-S`` a single B
  paid 7;
* research categories equal the SKUs, targets A=1, B=1.

The two return variants differ only in when the B unit of ``S-P`` came back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import open_service
from .observations import ObservationPackage, ObservationService, PeriodSpec
from .sampling import build_report
from .db import canonical_json

ROOT = Path(__file__).resolve().parent.parent

EARLY = "early"
LATE = "late"
CUTOFF_EARLY = "2026-06-30T00:00:00+00:00"
CUTOFF_LATE = "2026-12-31T00:00:00+00:00"
# Deterministic event clock.
SALE_TIME = "2026-06-01T00:00:00+00:00"
RETURN_BEFORE = "2026-06-15T00:00:00+00:00"  # on or before the early cutoff
RETURN_AFTER = "2026-07-15T00:00:00+00:00"  # after the early cutoff

# Worked-example category versions: the mapping version is independent of the
# merchant SKU and coupon versions, and each period carries its own one.
CATEGORY_V1 = {EARLY: "cat-v1", LATE: "cat-v1"}
CATEGORY_V2 = {EARLY: "cat-v1", LATE: "cat-v2-reclass"}
QUOTAS_AB = {EARLY: {"A": 1, "B": 1}, LATE: {"A": 1, "B": 1}}
# Under the reclassification the late B-only package lands in B-other, which the
# research group deliberately gives no quota, so it cannot substitute for B.
QUOTAS_RECLASS = {
    EARLY: {"A": 1, "B": 1},
    LATE: {"A": 1, "B-other": 0},
}


def _set_time(db_path: str, table: str, id_column: str, row_id: str, timestamp: str) -> None:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"UPDATE {table} SET created_at = ? WHERE {id_column} = ?",
            (timestamp, row_id),
        )
        assert conn.total_changes == 1
        conn.commit()
    finally:
        conn.close()


def build_worked_example(db_path: str, *, b_return_time: Optional[str]) -> str:
    """Create the four real transactions; optionally return the B unit of S-P.

    ``b_return_time`` of None leaves every unit retained.  The file is rebuilt
    from scratch so the scenario is genuinely reopenable.
    """

    for suffix in ("", "-wal", "-shm"):
        candidate = Path(db_path + suffix)
        if candidate.exists():
            candidate.unlink()

    svc = open_service(db_path)

    # Early set coupon: original A=4, B=6, D=2 -> proportional paid 3 and 5.
    products_ab = [{"sku": "A", "price": 4}, {"sku": "B", "price": 6}]
    snap_ab = svc.publish_snapshot(
        products_ab,
        [{"coupon_id": "AB", "D": 2, "requires": {"A": 1, "B": 1}}],
        snapshot_id="snap-ab",
    )
    basket_ab = [{"unit_id": "a", "sku": "A", "price": 4}, {"unit_id": "b", "sku": "B", "price": 6}]
    quote_p = svc.quote(basket_ab, [], "quote-p", snap_ab)
    assert [ (u["unit_id"], u["paid"]) for u in quote_p["allocation"] ] == [("a", 3), ("b", 5)]
    svc.register_sale("S-P", quote=quote_p)
    _set_time(db_path, "sales", "sale_id", "S-P", SALE_TIME)

    # Early unpromoted single A paid 6 (no coupon on the snapshot).
    snap_plain_a = svc.publish_snapshot([{"sku": "A", "price": 6}], [], snapshot_id="snap-plain-a")
    quote_q = svc.quote([{"unit_id": "a", "sku": "A", "price": 6}], [], "quote-q", snap_plain_a)
    assert quote_q["allocation"][0]["paid"] == 6
    svc.register_sale("S-Q", quote=quote_q)
    _set_time(db_path, "sales", "sale_id", "S-Q", SALE_TIME)

    # Late single A paid 5.
    snap_r = svc.publish_snapshot([{"sku": "A", "price": 5}], [], snapshot_id="snap-r")
    quote_r = svc.quote([{"unit_id": "a", "sku": "A", "price": 5}], [], "quote-r", snap_r)
    svc.register_sale("S-R", quote=quote_r)
    _set_time(db_path, "sales", "sale_id", "S-R", SALE_TIME)

    # Late single B paid 7.
    snap_s = svc.publish_snapshot([{"sku": "B", "price": 7}], [], snapshot_id="snap-s")
    quote_s = svc.quote([{"unit_id": "b", "sku": "B", "price": 7}], [], "quote-s", snap_s)
    svc.register_sale("S-S", quote=quote_s)
    _set_time(db_path, "sales", "sale_id", "S-S", SALE_TIME)

    if b_return_time is not None:
        result = svc.return_units("ret-p-b", "S-P", ["b"], {"b": "RESTOCK"})
        assert result["items"][0]["refund"] == 5  # refund follows the frozen paid
        _set_time(db_path, "return_requests", "request_id", "ret-p-b", b_return_time)
    return db_path


def worked_periods() -> Tuple[PeriodSpec, PeriodSpec]:
    return (
        PeriodSpec(EARLY, ("S-P", "S-Q"), CUTOFF_EARLY),
        PeriodSpec(LATE, ("S-R", "S-S"), CUTOFF_LATE),
    )


def worked_category_maps(version: str = CATEGORY_V1[LATE]) -> Dict[str, Dict[str, str]]:
    base = {EARLY: {"A": "A", "B": "B"}, LATE: {"A": "A", "B": "B"}}
    if version == CATEGORY_V2[LATE]:
        # The late B-only observation is re-classed into a category that has no
        # early counterpart and a zero quota: it cannot substitute for anything.
        base[LATE] = {"A": "A", "B": "B-other"}
    return base


def open_worked_example(db_path: str) -> ObservationService:
    periods = worked_periods()
    return ObservationService(open_service(db_path).db, periods)


def find_package(service: ObservationService, sale_id: str, coupon_id: Optional[str]) -> ObservationPackage:
    for package in service._by_id.values():
        if package.sale_id == sale_id and package.coupon_id == coupon_id:
            return package
    raise KeyError(f"no package for sale={sale_id} coupon={coupon_id}")


def run_worked_reports(db_path: str, *, category_version: str = CATEGORY_V1[LATE]) -> Dict[str, Any]:
    service = open_worked_example(db_path)
    maps = worked_category_maps(category_version)
    quotas = QUOTAS_RECLASS if category_version == CATEGORY_V2[LATE] else QUOTAS_AB
    report = build_report(service, maps, quotas, versions=(CATEGORY_V1[EARLY], category_version))
    return {
        "audit_early": service.audit_period(EARLY),
        "audit_late": service.audit_period(LATE),
        "rebuild_audit": service.rebuild_audit(),
        "report": report.to_dict(),
    }


# ---------------------------------------------------------------------------
# Distinct-vector fixture: no pair of packages (across both periods) shares a
# category-count vector, yet a balanced sample exists only by combining units.
# ---------------------------------------------------------------------------
def build_distinct_vector_fixture(db_path: str) -> str:
    """Five real sales whose package vectors are pairwise different.

    The optimizer only retains the best coupon set per sale, so each desired
    package is realised as its own transaction with exactly the basket that
    admits it.  Across both periods the five category-count vectors are::

        early: (catX:1), (catX:2,catY:1), (catX:1,catY:1)
        late:  (catY:1), (catX:2)

    No two coincide, and no individual package balances against another: the
    optimum combines two packages on each side into (catX:2, catY:1).  A larger
    (catX:3, catY:2) balance is deliberately unreachable: neither period has a
    second standalone Y unit available, which also proves the selector does not
    invent "single-item" packages that were never transacted.
    """

    for suffix in ("", "-wal", "-shm"):
        candidate = Path(db_path + suffix)
        if candidate.exists():
            candidate.unlink()
    svc = open_service(db_path)
    products = [{"sku": "X", "price": 10}, {"sku": "Y", "price": 10}]

    def snapshot(name, coupons):
        return svc.publish_snapshot(products, coupons, snapshot_id=name)

    snap_xxy = snapshot("snap-xxy", [{"coupon_id": "XXY", "D": 6, "requires": {"X": 2, "Y": 1}}])
    snap_xy = snapshot("snap-xy", [{"coupon_id": "XY", "D": 4, "requires": {"X": 1, "Y": 1}}])
    snap_xx = snapshot("snap-xx", [{"coupon_id": "XX", "D": 3, "requires": {"X": 2}}])
    snap_x1 = snapshot("snap-x1", [{"coupon_id": "X1", "D": 2, "requires": {"X": 1}}])
    snap_plain = snapshot("snap-distinct-plain", [])

    def sell(sale_id: str, request_id: str, basket, snapshot_id):
        quote = svc.quote(basket, [], request_id, snapshot_id)
        svc.register_sale(sale_id, quote=quote)
        _set_time(db_path, "sales", "sale_id", sale_id, SALE_TIME)

    # Early period: three distinct vectors.
    sell("S-E1", "quote-e1", [{"unit_id": "x", "sku": "X", "price": 10}], snap_x1)
    sell(
        "S-E2",
        "quote-e2",
        [
            {"unit_id": "x1", "sku": "X", "price": 10},
            {"unit_id": "x2", "sku": "X", "price": 10},
            {"unit_id": "y1", "sku": "Y", "price": 10},
        ],
        snap_xxy,
    )
    sell(
        "S-E3",
        "quote-e3",
        [{"unit_id": "x", "sku": "X", "price": 10}, {"unit_id": "y", "sku": "Y", "price": 10}],
        snap_xy,
    )
    # Late period: two more distinct vectors.
    sell("S-L1", "quote-l1", [{"unit_id": "y", "sku": "Y", "price": 10}], snap_plain)
    sell(
        "S-L2",
        "quote-l2",
        [{"unit_id": "x1", "sku": "X", "price": 10}, {"unit_id": "x2", "sku": "X", "price": 10}],
        snap_xx,
    )
    return db_path


DISTINCT_PERIODS = (
    PeriodSpec(EARLY, ("S-E1", "S-E2", "S-E3"), CUTOFF_EARLY),
    PeriodSpec(LATE, ("S-L1", "S-L2"), CUTOFF_LATE),
)
DISTINCT_MAPS = {
    EARLY: {"X": "catX", "Y": "catY"},
    LATE: {"X": "catX", "Y": "catY"},
}
DISTINCT_QUOTAS = {
    EARLY: {"catX": 3, "catY": 2},
    LATE: {"catX": 3, "catY": 2},
}


def run_distinct_vector_report(db_path: str) -> Dict[str, Any]:
    service = ObservationService(open_service(db_path).db, DISTINCT_PERIODS)
    report = build_report(service, DISTINCT_MAPS, DISTINCT_QUOTAS, versions=("fixture-v1", "fixture-v1"))
    vectors = []
    for period in (EARLY, LATE):
        for package in service.packages(period):
            vectors.append(
                {
                    "package_id": package.package_id,
                    "period": period,
                    "vector": dict(package.category_vector(DISTINCT_MAPS[period])),
                }
            )
    return {"vectors": vectors, "report": report.to_dict()}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def run_all(base_dir: Optional[str] = None) -> Dict[str, Any]:
    """Build every scenario database and emit the four required reports."""

    base = Path(base_dir) if base_dir else ROOT / "data"
    results_dir = ROOT / "results"
    no_return_db = str(base / "observations_worked.db")
    returned_db = str(base / "observations_returned.db")
    distinct_db = str(base / "observations_distinct.db")

    build_worked_example(no_return_db, b_return_time=None)
    build_worked_example(returned_db, b_return_time=RETURN_BEFORE)
    build_distinct_vector_fixture(distinct_db)

    # Report 1: no return at all (equivalently a return after the cutoff).
    report_clean = run_worked_reports(no_return_db)
    # Report 2: the B unit is back before the early cutoff -> P dies entirely.
    report_returned = run_worked_reports(returned_db)
    # Report 3: same frozen transactions, same cutoffs, but a new late category
    # version reclasses B into a non-substitutable category.
    report_reclass = run_worked_reports(no_return_db, category_version=CATEGORY_V2[LATE])
    # Report 4: the all-distinct-vectors balancing fixture.
    report_distinct = run_distinct_vector_report(distinct_db)

    # An after-cutoff return must leave report 1 intact: rebuild a database with
    # a late return and confirm the report is byte-identical to the clean one.
    after_db = str(base / "observations_after_cutoff.db")
    build_worked_example(after_db, b_return_time=RETURN_AFTER)
    report_after = run_worked_reports(after_db)
    assert canonical_json(report_after["report"]) == canonical_json(report_clean["report"])

    payload = {
        "databases": {
            "worked_no_return": no_return_db,
            "worked_b_returned_before_cutoff": returned_db,
            "worked_b_returned_after_cutoff": after_db,
            "distinct_vectors": distinct_db,
        },
        "report_1_no_return_index_12_over_8": report_clean,
        "report_2_b_returned_before_cutoff_index_5_over_6": report_returned,
        "report_3_b_reclassed_unbalanceable": report_reclass,
        "report_4_distinct_vectors_combine_to_balance": report_distinct,
        "after_cutoff_return_matches_report_1": True,
    }
    write_json(results_dir / "observation_reports.json", payload)
    return payload
