"""Demo driver: builds a real persistent database and a JSON result record.

Run ``python3 -m basket_service demo`` to (re)generate::

    data/demo.db            real SQLite database
    results/demo_plan.json  the JSON plan that was executed for the main run
    results/demo_results.json  every quote, sale, return and independent check

All *expectations* in this file are produced by ``basket_service.reference``
(brute-force enumeration and Fraction arithmetic) and by reading the raw
ledger records, never by calling the production optimizer/refund functions to
compute the expected answer.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

from . import open_service
from .errors import BasketError
from .reference import exhaustive_best, expected_allocation, expected_return
from .validation import validate_basket, validate_coupons

ROOT = Path(__file__).resolve().parent.parent


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _brute(basket, coupons):
    units = validate_basket(basket)
    canonical = validate_coupons(coupons, {u["sku"] for u in units})
    total, used, unit_lists, mapping = exhaustive_best(units, canonical)
    allocation = expected_allocation(units, mapping, {c["coupon_id"]: c["D"] for c in canonical})
    return total, mapping, allocation


def _case_material() -> Dict[str, Any]:
    case = json.loads((ROOT / "case.json").read_text(encoding="utf-8"))
    basket = case["basket"]
    before = case["before_coupons"]
    added = case["added_coupons"]
    return {"basket": basket, "before": before, "added": added, "return_request": case["return_request"]}


def run_demo(db_path: str, out_path: str, plan_path: str) -> Dict[str, Any]:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(db_path + suffix)
        if candidate.exists():
            candidate.unlink()

    svc = open_service(db_path)
    results: Dict[str, Any] = {"database": str(db_path), "scenarios": {}, "checks": []}

    # ------------------------------------------------------------------
    # 1. Case material: two snapshots, quote under both, bind two sales.
    # ------------------------------------------------------------------
    material = _case_material()
    basket = material["basket"]
    snap_before = svc.publish_snapshot(
        [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}],
        material["before"],
        snapshot_id="case-before",
    )
    snap_added = svc.publish_snapshot(
        [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}],
        material["before"] + material["added"],
        snapshot_id="case-added",
    )
    quote_before = svc.quote(basket, [], "case-q-before", snap_before)
    quote_added = svc.quote(basket, [], "case-q-added", snap_added)
    brute_before = _brute(basket, material["before"])
    brute_added = _brute(basket, material["before"] + material["added"])
    results["scenarios"]["case_material"] = {
        "snapshots": {"before": snap_before, "added": snap_added},
        "quote_before": quote_before,
        "quote_added": quote_added,
        "independent_before": {"total": brute_before[0], "assignments": brute_before[1], "allocation": brute_before[2]},
        "independent_added": {"total": brute_added[0], "assignments": brute_added[1], "allocation": brute_added[2]},
    }
    results["checks"].append({
        "check": "case.before.total_matches_bruteforce",
        "ok": quote_before["total"] == brute_before[0],
        "expected": brute_before[0],
        "actual": quote_before["total"],
    })
    results["checks"].append({
        "check": "case.added.total_matches_bruteforce",
        "ok": quote_added["total"] == brute_added[0],
        "expected": brute_added[0],
        "actual": quote_added["total"],
    })
    results["checks"].append({
        "check": "old_quote_unchanged_after_new_snapshot",
        "ok": svc.get_quote("case-q-before") == quote_before,
        "expected": True,
        "actual": svc.get_quote("case-q-before") == quote_before,
    })

    # Two independent legal sales, different promotions -> different paid.
    sale_before = svc.register_sale("S1", quote=quote_before)
    sale_added = svc.register_sale("S2", quote=quote_added)
    results["scenarios"]["two_sales"] = {"S1": sale_before, "S2": sale_added}

    # Returns follow their own bound quote, not the newest promotion.
    ret_before_b = svc.return_units(material["return_request"]["request_id"], "S1", ["b"], {"b": "RESTOCK"})
    ret_added_b = svc.return_units("ret-added-b", "S2", ["b"], {"b": "SCRAP"})
    paid_before = {u["unit_id"]: u["paid"] for u in sale_before["units"]}
    paid_added = {u["unit_id"]: u["paid"] for u in sale_added["units"]}
    results["scenarios"]["bound_quote_refunds"] = {
        "case-return-request": ret_before_b,
        "ret-added-b": ret_added_b,
    }
    results["checks"].append({
        "check": "old_sale_refund_uses_old_quote",
        "ok": ret_before_b["refund_total"] == expected_return(paid_before, ["b"])[0],
        "expected": expected_return(paid_before, ["b"])[0],
        "actual": ret_before_b["refund_total"],
    })
    results["checks"].append({
        "check": "new_sale_refund_uses_new_quote",
        "ok": ret_added_b["refund_total"] == expected_return(paid_added, ["b"])[0],
        "expected": expected_return(paid_added, ["b"])[0],
        "actual": ret_added_b["refund_total"],
    })

    # Same sale_id with the other quote must be rejected; the old identity is
    # still a legitimate, different sale rather than a retry.
    try:
        svc.register_sale("S1", quote=quote_added)
        rebind_ok = False
        rebind_error = None
    except BasketError as exc:
        rebind_ok = True
        rebind_error = exc.code
    results["scenarios"]["sale_conflict"] = {"rejected": rebind_ok, "error": rebind_error}

    # Promotion switch: publish a third, stronger snapshot and quote again; the
    # old sale still refunds from its own binding.
    third_coupons = material["before"] + material["added"] + [
        {"coupon_id": "ABC", "D": 200, "requires": {"A": 1, "B": 1, "C": 1}}
    ]
    snap_third = svc.publish_snapshot(
        [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}],
        third_coupons,
        snapshot_id="case-third",
    )
    quote_third = svc.quote(basket, [], "case-q-third", snap_third)
    ret_before_a = svc.return_units("ret-before-a", "S1", ["a"], {"a": "SCRAP"})
    results["scenarios"]["promotion_switch"] = {
        "snapshot": snap_third,
        "quote": quote_third,
        "old_sale_refund_a_after_switch": ret_before_a,
    }
    results["checks"].append({
        "check": "post_switch_old_sale_refund_follows_old_paid",
        "ok": ret_before_a["refund_total"] == paid_before["a"],
        "expected": paid_before["a"],
        "actual": ret_before_a["refund_total"],
    })

    # ------------------------------------------------------------------
    # 2. Optimizer trap and tie-break examples.
    # ------------------------------------------------------------------
    greedy_basket = [
        {"unit_id": "a", "sku": "A", "price": 100},
        {"unit_id": "b", "sku": "B", "price": 100},
        {"unit_id": "c", "sku": "C", "price": 100},
        {"unit_id": "d", "sku": "D", "price": 100},
    ]
    greedy_coupons = [
        {"coupon_id": "COMBINED", "D": 190, "requires": {"A": 1, "B": 1, "C": 1, "D": 1}},
        {"coupon_id": "AB", "D": 120, "requires": {"A": 1, "B": 1}},
        {"coupon_id": "CD", "D": 120, "requires": {"C": 1, "D": 1}},
    ]
    gsnap = svc.publish_snapshot(None, greedy_coupons)
    greedy_quote = svc.quote(greedy_basket, [], "greedy-quote", gsnap)
    greedy_brute = _brute(greedy_basket, greedy_coupons)
    results["scenarios"]["greedy_trap"] = {
        "basket": greedy_basket,
        "coupons": greedy_coupons,
        "largest_D_first_total": 400 - 190,
        "optimal_quote": greedy_quote,
        "independent": {"total": greedy_brute[0], "assignments": greedy_brute[1], "allocation": greedy_brute[2]},
    }
    results["checks"].append({
        "check": "greedy_largest_d_is_not_optimal",
        "ok": greedy_quote["total"] < 400 - 190 and greedy_quote["total"] == greedy_brute[0],
        "expected": greedy_brute[0],
        "actual": greedy_quote["total"],
        "greedy_total": 400 - 190,
    })

    contention_basket = [
        {"unit_id": "a", "sku": "A", "price": 100},
        {"unit_id": "b", "sku": "B", "price": 100},
    ]
    contention_coupons = [
        {"coupon_id": "AB1", "D": 120, "requires": {"A": 1, "B": 1}},
        {"coupon_id": "AB2", "D": 110, "requires": {"A": 1, "B": 1}},
    ]
    csnap = svc.publish_snapshot(None, contention_coupons)
    contention_quote = svc.quote(contention_basket, [], "contention-quote", csnap)
    results["scenarios"]["contention"] = {
        "quote": contention_quote,
        "independent": _brute(contention_basket, contention_coupons)[:2],
    }

    price_basket = [
        {"unit_id": "cheap", "sku": "A", "price": 30},
        {"unit_id": "dear", "sku": "A", "price": 300},
        {"unit_id": "mid", "sku": "A", "price": 120},
    ]
    price_coupons = [
        {"coupon_id": "big", "D": 250, "requires": {"A": 1}},
        {"coupon_id": "small1", "D": 60, "requires": {"A": 1}},
        {"coupon_id": "small2", "D": 50, "requires": {"A": 1}},
    ]
    psnap = svc.publish_snapshot(None, price_coupons)
    price_quote = svc.quote(price_basket, [], "price-quote", psnap)
    results["scenarios"]["same_sku_different_prices"] = {
        "quote": price_quote,
        "independent": _brute(price_basket, price_coupons)[:2],
    }

    tie_basket = [
        {"unit_id": "a", "sku": "A", "price": 100},
        {"unit_id": "b", "sku": "A", "price": 100},
        {"unit_id": "c", "sku": "A", "price": 100},
    ]
    tie_coupons = [
        {"coupon_id": "z", "D": 50, "requires": {"A": 1}},
        {"coupon_id": "y", "D": 50, "requires": {"A": 1}},
        {"coupon_id": "x", "D": 50, "requires": {"A": 1}},
    ]
    tsnap = svc.publish_snapshot(None, tie_coupons)
    tie_quote = svc.quote(tie_basket, [], "tie-quote", tsnap)
    results["scenarios"]["two_level_tie_break"] = {
        "quote": tie_quote,
        "independent": _brute(tie_basket, tie_coupons)[:2],
    }
    results["checks"].append({
        "check": "tie_break_matches_bruteforce",
        "ok": tie_quote["assignments"] == _brute(tie_basket, tie_coupons)[1],
        "expected": _brute(tie_basket, tie_coupons)[1],
        "actual": tie_quote["assignments"],
    })

    remainder_basket = [
        {"unit_id": "z", "sku": "A", "price": 100},
        {"unit_id": "a", "sku": "A", "price": 100},
        {"unit_id": "m", "sku": "A", "price": 100},
        {"unit_id": "low", "sku": "A", "price": 1},
    ]
    remainder_coupons = [{"coupon_id": "third", "D": 100, "requires": {"A": 3}}]
    rsnap = svc.publish_snapshot(None, remainder_coupons)
    remainder_quote = svc.quote(remainder_basket, [], "remainder-quote", rsnap)
    results["scenarios"]["proportional_remainder"] = {
        "quote": remainder_quote,
        "independent": _brute(remainder_basket, remainder_coupons),
    }
    results["checks"].append({
        "check": "proportional_allocation_matches_fraction_reference",
        "ok": remainder_quote["allocation"] == _brute(remainder_basket, remainder_coupons)[2],
        "expected": _brute(remainder_basket, remainder_coupons)[2],
        "actual": remainder_quote["allocation"],
    })

    # ------------------------------------------------------------------
    # 3. Ten-unit basket: independent full enumeration + ledger audit.
    # ------------------------------------------------------------------
    ten_basket = [
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
    ten_coupons = [
        {"coupon_id": "c0", "D": 40, "requires": {"A": 1}},
        {"coupon_id": "c1", "D": 50, "requires": {"B": 1, "C": 1}},
        {"coupon_id": "c2", "D": 35, "requires": {"D": 2}},
        {"coupon_id": "c3", "D": 60, "requires": {"A": 2}},
        {"coupon_id": "c4", "D": 70, "requires": {"C": 1, "D": 1, "E": 1}},
        {"coupon_id": "c5", "D": 25, "requires": {"F": 1}},
        {"coupon_id": "c6", "D": 45, "requires": {"B": 2}},
        {"coupon_id": "c7", "D": 30, "requires": {"E": 1, "F": 1}},
    ]
    ten_snap = svc.publish_snapshot(None, ten_coupons)
    ten_quote = svc.quote(ten_basket, [], "ten-quote", ten_snap)
    ten_brute = _brute(ten_basket, ten_coupons)
    ten_sale = svc.register_sale("S-ten", quote=ten_quote)
    returned_units = ["u1", "u5", "u9", "u3"]
    grades = {"u1": "RESTOCK", "u5": "SCRAP", "u9": "RESTOCK", "u3": "RESTOCK"}
    ten_return = svc.return_units("ret-ten", "S-ten", returned_units, grades)
    paid = {row["unit_id"]: row["paid"] for row in ten_brute[2]}
    expected_refund, expected_per_unit = expected_return(paid, returned_units)
    results["scenarios"]["ten_unit"] = {
        "basket": ten_basket,
        "coupons": ten_coupons,
        "quote": ten_quote,
        "independent": {"total": ten_brute[0], "assignments": ten_brute[1], "allocation": ten_brute[2]},
        "sale": ten_sale,
        "return": ten_return,
        "independent_refund": {"total": expected_refund, "per_unit": expected_per_unit},
    }
    results["checks"].append({
        "check": "ten_unit_total_matches_full_enumeration",
        "ok": ten_quote["total"] == ten_brute[0] and ten_quote["assignments"] == ten_brute[1],
        "expected": ten_brute[0],
        "actual": ten_quote["total"],
    })
    results["checks"].append({
        "check": "ten_unit_return_matches_original_paid",
        "ok": ten_return["refund_total"] == expected_refund,
        "expected": expected_refund,
        "actual": ten_return["refund_total"],
    })

    # Independent ledger audit: reconstruct from sale_units + return_items, not
    # from the service's refund function.
    raw = svc.db
    with raw.connection() as conn:
        unit_rows = conn.execute(
            "SELECT unit_id, sku, paid, returned, grade FROM sale_units WHERE sale_id = ? ORDER BY unit_id", ("S-ten",)
        ).fetchall()
        return_rows = conn.execute(
            "SELECT ri.unit_id, ri.grade, ri.refund FROM return_items ri JOIN return_requests rr ON rr.request_id = ri.request_id WHERE rr.sale_id = ? ORDER BY ri.unit_id",
            ("S-ten",),
        ).fetchall()
        inv_rows = conn.execute("SELECT sku, bucket, SUM(delta) AS qty FROM inventory_events GROUP BY sku, bucket").fetchall()
    independent_ledger = {
        "sale_units": [dict(r) for r in unit_rows],
        "return_items": [dict(r) for r in return_rows],
        "inventory_from_raw_events": [dict(r) for r in inv_rows],
        "per_unit_paid_sum": sum(r["paid"] for r in unit_rows),
        "returned_sum": sum(r["refund"] for r in return_rows),
        "remaining": [r["unit_id"] for r in unit_rows if not r["returned"]],
    }
    results["scenarios"]["ten_unit"]["independent_ledger_audit"] = independent_ledger
    results["checks"].append({
        "check": "ten_unit_ledger_audit_per_unit_paid_sums_to_quote",
        "ok": independent_ledger["per_unit_paid_sum"] == ten_quote["total"],
        "expected": ten_quote["total"],
        "actual": independent_ledger["per_unit_paid_sum"],
    })
    results["checks"].append({
        "check": "ten_unit_ledger_audit_refund_sums_to_return",
        "ok": independent_ledger["returned_sum"] == ten_return["refund_total"],
        "expected": ten_return["refund_total"],
        "actual": independent_ledger["returned_sum"],
    })

    # ------------------------------------------------------------------
    # 4. Retry, overlap and independent stock movements.
    # ------------------------------------------------------------------
    lost_response_retry = svc.return_units("ret-ten", "S-ten", returned_units, grades)
    results["scenarios"]["lost_response_retry"] = lost_response_retry
    results["checks"].append({
        "check": "retry_replays_first_result",
        "ok": lost_response_retry == ten_return,
        "expected": ten_return,
        "actual": lost_response_retry,
    })

    try:
        svc.return_units("overlap", "S-ten", ["u0", "u1"], {"u0": "RESTOCK", "u1": "RESTOCK"})
        overlap = {"rejected": False}
    except BasketError as exc:
        overlap = {"rejected": True, "error": exc.code}
    results["scenarios"]["overlap_rejected"] = overlap
    results["checks"].append({
        "check": "overlap_rejects_whole_request_and_keeps_u0_refundable",
        "ok": overlap["rejected"] and "u0" in svc.get_sale("S-ten")["remaining_refundable"],
        "expected": True,
        "actual": overlap["rejected"],
    })

    adj = svc.adjust_inventory("adj-A-1", "A", 4, "RESTOCK")
    adj_scrap = svc.adjust_inventory("adj-B-1", "B", 2, "SCRAP")
    results["scenarios"]["adjustments"] = {"restock": adj, "scrap": adj_scrap}
    results["checks"].append({
        "check": "adjustment_does_not_change_refundable",
        "ok": "u0" in svc.get_sale("S-ten")["remaining_refundable"],
        "expected": True,
        "actual": "u0" in svc.get_sale("S-ten")["remaining_refundable"],
    })

    # ------------------------------------------------------------------
    # 5. Reopen the database and keep all state.
    # ------------------------------------------------------------------
    results["scenarios"]["before_reopen"] = {
        "inventory": svc.inventory(),
        "sale_ten": svc.get_sale("S-ten"),
        "audit_sales": svc.audit_sales(),
        "audit_inventory": svc.audit_inventory(),
    }
    del svc
    reopened = open_service(db_path)
    results["scenarios"]["after_reopen"] = {
        "get_quote_case": reopened.get_quote("case-q-before"),
        "get_sale_ten": reopened.get_sale("S-ten"),
        "get_return_ten": reopened.get_return("ret-ten"),
        "inventory": reopened.inventory(),
        "audit_sales": reopened.audit_sales(),
        "audit_inventory": reopened.audit_inventory(),
    }
    results["checks"].append({
        "check": "reopen_preserves_quote_sale_return_inventory",
        "ok": (
            reopened.get_quote("case-q-before") == quote_before
            and reopened.get_sale("S-ten")["remaining_refundable"] == ["u0", "u2", "u4", "u6", "u7", "u8"]
            and reopened.get_return("ret-ten")["refund_total"] == ten_return["refund_total"]
            and reopened.inventory() == results["scenarios"]["before_reopen"]["inventory"]
        ),
        "expected": "identical",
        "actual": "identical"
        if (
            reopened.get_quote("case-q-before") == quote_before
            and reopened.get_sale("S-ten")["remaining_refundable"] == ["u0", "u2", "u4", "u6", "u7", "u8"]
            and reopened.get_return("ret-ten")["refund_total"] == ten_return["refund_total"]
            and reopened.inventory() == results["scenarios"]["before_reopen"]["inventory"]
        )
        else "differs",
    })

    # ------------------------------------------------------------------
    # 6. Independent quote issuer -> ledger-only consumer.
    # ------------------------------------------------------------------
    ledger_path = str(Path(db_path).with_suffix(".ledger.db"))
    for suffix in ("", "-wal", "-shm"):
        stale = Path(ledger_path + suffix)
        if stale.exists():
            stale.unlink()
    ledger_only = open_service(ledger_path)
    consumer_sale = ledger_only.register_sale("S-independent", quote=quote_added)
    consumer_return = ledger_only.return_units("ret-independent", "S-independent", ["c"], {"c": "RESTOCK"})
    results["scenarios"]["independent_quote_consumer"] = {
        "sale": consumer_sale,
        "return": consumer_return,
        "quote_came_from": "case-q-added (validated payload; ledger never re-optimised)",
    }
    results["checks"].append({
        "check": "ledger_consumes_complete_external_quote",
        "ok": consumer_sale["total"] == quote_added["total"]
        and consumer_return["refund_total"] == expected_return(paid_added, ["c"])[0],
        "expected": expected_return(paid_added, ["c"])[0],
        "actual": consumer_return["refund_total"],
    })
    del ledger_only

    # ------------------------------------------------------------------
    # 7. Concurrent overlapping returns against the real file database.
    # ------------------------------------------------------------------
    race_svc = open_service(db_path)
    concurrent_quote = race_svc.quote(basket, [], "race-q", snap_before)
    race_svc.register_sale("S-race", quote=concurrent_quote)
    restock_b_before_race = race_svc.inventory()["RESTOCK"].get("B", 0)
    del race_svc
    barrier = threading.Barrier(2)
    outcomes: List[Dict[str, Any]] = []
    failures: List[str] = []
    lock = threading.Lock()

    def worker(request_id: str) -> None:
        client = open_service(db_path)
        try:
            barrier.wait()
            result = client.return_units(request_id, "S-race", ["b"], {"b": "RESTOCK"})
            with lock:
                outcomes.append({"request_id": request_id, "refund_total": result["refund_total"]})
        except BasketError as exc:
            with lock:
                failures.append(exc.code)

    threads = [threading.Thread(target=worker, args=(f"race-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    final = open_service(db_path)
    restock_b_after_race = final.inventory()["RESTOCK"].get("B", 0)
    race_state = {
        "successes": outcomes,
        "failures": failures,
        "inventory_B_restock": restock_b_after_race,
        "inventory_B_restock_delta": restock_b_after_race - restock_b_before_race,
        "remaining": final.get_sale("S-race")["remaining_refundable"],
    }
    results["scenarios"]["concurrent_returns"] = race_state
    results["checks"].append({
        "check": "concurrent_overlap_one_success_one_rejection_no_double_stock",
        "ok": len(outcomes) == 1
        and failures == ["UNIT_ALREADY_RETURNED"]
        and race_state["inventory_B_restock_delta"] == 1,
        "expected": {"successes": 1, "rejections": ["UNIT_ALREADY_RETURNED"], "restock_B_delta": 1},
        "actual": race_state,
    })
    del final

    results["summary"] = {
        "checks": len(results["checks"]),
        "passed": sum(1 for c in results["checks"] if c["ok"]),
        "failed": sum(1 for c in results["checks"] if not c["ok"]),
    }
    results["plan"] = {
        "note": "The structured operations executed above, replayable with `python -m basket_service run-plan`.",
        "steps": [
            {"op": "publish_snapshot", "products": [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}], "coupons": material["before"], "snapshot_id": "case-before"},
            {"op": "publish_snapshot", "products": [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}], "coupons": material["before"] + material["added"], "snapshot_id": "case-added"},
            {"name": "q_before", "op": "quote", "basket": basket, "coupons": [], "request_id": "case-q-before", "snapshot_id": "case-before"},
            {"name": "q_added", "op": "quote", "basket": basket, "coupons": [], "request_id": "case-q-added", "snapshot_id": "case-added"},
            {"name": "s_before", "op": "register_sale", "sale_id": "S1", "quote": "$q_before"},
            {"name": "s_added", "op": "register_sale", "sale_id": "S2", "quote": "$q_added"},
            {"op": "return", "request_id": "r1", "sale_id": "S1", "unit_ids": ["b"], "grade": {"b": "RESTOCK"}},
            {"op": "return", "request_id": "ret-added-b", "sale_id": "S2", "unit_ids": ["b"], "grade": {"b": "SCRAP"}},
            {"op": "register_sale", "sale_id": "S1", "quote": "$q_added", "expect_error": "SALE_CONFLICT"},
        ],
    }

    _write_json(Path(out_path), results)
    _write_json(Path(plan_path), results["plan"])
    return results


def _run_plan_file(db_path: str, plan_file: str, out_file: str | None) -> Dict[str, Any]:
    svc = open_service(db_path)
    plan = json.loads(Path(plan_file).read_text(encoding="utf-8"))
    result = svc.run(plan)
    if out_file:
        _write_json(Path(out_file), result)
    return result


def main(argv: List[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m basket_service", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run the full demo and write database/results")
    demo.add_argument("--db", default=str(ROOT / "data" / "demo.db"))
    demo.add_argument("--out", default=str(ROOT / "results" / "demo_results.json"))
    demo.add_argument("--plan", default=str(ROOT / "results" / "demo_plan.json"))

    run_plan = sub.add_parser("run-plan", help="execute a JSON plan against a database")
    run_plan.add_argument("plan")
    run_plan.add_argument("--db", required=True)
    run_plan.add_argument("--out")

    selfcheck = sub.add_parser("self-check", help="run the unit test suite")
    selfcheck.add_argument("--db", default=str(ROOT / "data" / "selfcheck.db"))
    selfcheck.add_argument("--out", default=str(ROOT / "results" / "self_check.json"))

    observations = sub.add_parser(
        "observations",
        help="build the reopenable observation scenarios and the two-period reports",
    )
    observations.add_argument("--db-dir", default=str(ROOT / "data"))
    observations.add_argument("--out", default=str(ROOT / "results" / "observation_reports.json"))

    args = parser.parse_args(argv)
    if args.command == "demo":
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)
        results = run_demo(args.db, args.out, args.plan)
        print(json.dumps({"database": args.db, "results": args.out, "summary": results["summary"]}, indent=2))
        return 0 if results["summary"]["failed"] == 0 else 1
    if args.command == "run-plan":
        result = _run_plan_file(args.db, args.plan, args.out)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if args.command == "observations":
        from .scenarios import run_all

        payload = run_all(args.db_dir)
        _write_json(Path(args.out), payload)
        summary = {
            key: payload[key]["report"]["paid_ratio_text"] if "report" in payload[key] else None
            for key in payload
            if key.startswith("report_")
        }
        print(json.dumps({"out": args.out, "ratios": summary}, indent=2, ensure_ascii=False))
        return 0
    if args.command == "self-check":
        import unittest

        loader = unittest.TestLoader()
        suite = loader.discover(str(ROOT / "tests"))
        runner = unittest.TextTestRunner(verbosity=2)
        outcome = runner.run(suite)
        payload = {
            "ok": outcome.wasSuccessful(),
            "tests": outcome.testsRun,
            "failures": [str(t) for t, _ in outcome.failures],
            "errors": [str(t) for t, _ in outcome.errors],
        }
        _write_json(Path(args.out), payload)
        return 0 if outcome.wasSuccessful() else 1
    return 2
