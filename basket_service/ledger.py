"""After-sales ledger: sales, per-unit returns and independent stock adjustments.

Design points enforced here:

* A sale is bound to exactly one *complete* published quote.  The ledger stores
  the quote's per-unit original paid amounts and physical identities; refunds
  are read from those stored rows and never re-derived from current prices or
  new promotions.
* A physical unit can be returned at most once.  Returns are serialised with
  ``BEGIN IMMEDIATE`` and the eligibility flag is re-checked inside the
  transaction, so two connections racing on the same unit yield one success
  and one rejection (no in-memory-only locking).
* A return request is all-or-nothing: if any requested unit is already
  returned / ineligible, *none* of the units in that request are processed.
* Every refund and its inventory receipt/scrap move are inserted in the same
  transaction, so a crash cannot leave a refunded-but-unrecorded unit.
* Independent stock adjustments are idempotent by event id and never touch any
  sale's refundable amounts.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .db import Database, canonical_json, deep_copy, json_loads, utc_now
from .errors import (
    INVALID_GRADE,
    INVALID_INPUT,
    INVENTORY_CONFLICT,
    OVERLAPPING_RETURN,
    QUOTE_MISMATCH,
    SALE_CONFLICT,
    UNIT_ALREADY_RETURNED,
    UNIT_INELIGIBLE,
    UNKNOWN_SALE,
    BasketError,
)
from .validation import is_int, require_id, validate_quote_payload

GRADES = ("RESTOCK", "SCRAP")
BUCKETS = ("RESTOCK", "SCRAP")


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _quote_core(quote: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "basket": quote["basket"],
        "coupons": quote["coupons"],
        "assignments": quote["assignments"],
        "allocation": quote["allocation"],
        "total": quote["total"],
    }


def _validate_unit_ids(unit_ids: Any) -> List[str]:
    if unit_ids is None or isinstance(unit_ids, (str, bytes, Mapping)):
        raise BasketError(INVALID_INPUT, "unit_ids must be a list")
    if not isinstance(unit_ids, Sequence):
        raise BasketError(INVALID_INPUT, "unit_ids must be a list")
    result: List[str] = []
    for index, unit_id in enumerate(unit_ids):
        result.append(require_id(unit_id, INVALID_INPUT, f"unit_ids[{index}]"))
    result = sorted(result)
    if len(set(result)) != len(result):
        raise BasketError(INVALID_INPUT, "duplicate unit_id in request")
    return result


def _validate_grade(grade: Any, unit_ids: Sequence[str]) -> Dict[str, str]:
    if not isinstance(grade, Mapping):
        raise BasketError(INVALID_GRADE, "grade must be a unit_id -> RESTOCK|SCRAP mapping")
    canonical: Dict[str, str] = {}
    for unit_id in unit_ids:
        if unit_id not in grade:
            raise BasketError(INVALID_GRADE, "missing grade for a returned unit", {"unit_id": unit_id})
        value = grade[unit_id]
        if value not in GRADES:
            raise BasketError(INVALID_GRADE, "grade must be RESTOCK or SCRAP", {"unit_id": unit_id, "grade": value})
        canonical[unit_id] = value
    for unit_id in grade:
        if unit_id not in canonical:
            raise BasketError(INVALID_GRADE, "grade supplied for a unit not in the request", {"unit_id": unit_id})
    return canonical


class AfterSalesLedger:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ sale
    def register_sale(
        self,
        sale_id: str,
        *,
        quote: Optional[Mapping[str, Any]] = None,
        quote_id: Optional[str] = None,
        request_id: Optional[str] = None,
        snapshot_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        sale_id = require_id(sale_id, INVALID_INPUT, "sale_id")
        if quote is None:
            identifier = request_id or quote_id
            if identifier is None:
                raise BasketError(INVALID_INPUT, "register_sale needs a complete quote or a quote request id")
            with self.db.connection() as conn:
                row = conn.execute("SELECT quote_json FROM quotes WHERE request_id = ?", (identifier,)).fetchone()
            if row is None:
                raise BasketError("UNKNOWN_QUOTE", "quote request is not known to the ledger's quote store", {"request_id": identifier})
            payload = json_loads(row["quote_json"])
            validated = validate_quote_payload(payload)
            quote_request_id = payload["request_id"]
        else:
            validated = validate_quote_payload(quote)
            quote_request_id = quote["request_id"]
            identifier = request_id or quote_id
            if identifier is not None and identifier != quote_request_id:
                raise BasketError(
                    QUOTE_MISMATCH,
                    "sale is bound to a different quote identifier than the payload",
                    {"expected": quote_request_id, "got": identifier},
                )
            # If this ledger also stores the quote, require byte-canonical equality
            # so a tampered payload cannot masquerade as the stored quote.
            with self.db.connection() as conn:
                row = conn.execute("SELECT quote_json FROM quotes WHERE request_id = ?", (quote_request_id,)).fetchone()
            if row is not None:
                stored = json_loads(row["quote_json"])
                if _hash(_quote_core(stored)) != _hash(_quote_core(validated)):
                    raise BasketError(
                        QUOTE_MISMATCH,
                        "quote payload differs from the quote stored under the same request id",
                        {"request_id": quote_request_id},
                    )
        if snapshot_id is not None and snapshot_id != validated["snapshot_id"]:
            raise BasketError(
                QUOTE_MISMATCH,
                "snapshot_id does not match the bound quote",
                {"expected": validated["snapshot_id"], "got": snapshot_id},
            )

        quote_sig = _hash(_quote_core(validated))
        total = validated["total"]
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM sales WHERE sale_id = ?", (sale_id,)).fetchone()
            if existing is not None:
                if existing["quote_sig"] != quote_sig:
                    raise BasketError(
                        SALE_CONFLICT,
                        "sale_id already bound to a different complete quote",
                        {"sale_id": sale_id},
                    )
                return self._sale_result(conn, sale_id, repeated=True)
            conn.execute(
                "INSERT INTO sales (sale_id, quote_id, snapshot_id, quote_sig, quote_json, total, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    sale_id,
                    quote_request_id,
                    validated["snapshot_id"],
                    quote_sig,
                    canonical_json(validated),
                    total,
                    utc_now(),
                ),
            )
            for entry in validated["allocation"]:
                conn.execute(
                    "INSERT INTO sale_units (sale_id, unit_id, sku, price, coupon_id, discount, paid, returned) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                    (
                        sale_id,
                        entry["unit_id"],
                        entry["sku"],
                        entry["price"],
                        entry["coupon_id"],
                        entry["discount"],
                        entry["paid"],
                    ),
                )
            return self._sale_result(conn, sale_id, repeated=False)

    def get_sale(self, sale_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT sale_id FROM sales WHERE sale_id = ?", (sale_id,)).fetchone()
            if row is None:
                raise BasketError(UNKNOWN_SALE, "unknown sale", {"sale_id": sale_id})
            return self._sale_result(conn, sale_id, repeated=False)

    def _sale_result(self, conn, sale_id: str, *, repeated: bool) -> Dict[str, Any]:
        sale = conn.execute("SELECT * FROM sales WHERE sale_id = ?", (sale_id,)).fetchone()
        units = conn.execute(
            "SELECT unit_id, sku, price, coupon_id, discount, paid, returned, return_request_id, grade FROM sale_units WHERE sale_id = ? ORDER BY unit_id",
            (sale_id,),
        ).fetchall()
        unit_rows = [dict(row) for row in units]
        return {
            "ok": True,
            "sale_id": sale_id,
            "quote_id": sale["quote_id"],
            "snapshot_id": sale["snapshot_id"],
            "quote_sig": sale["quote_sig"],
            "total": sale["total"],
            "units": unit_rows,
            "refunded_total": sum(u["paid"] for u in unit_rows if u["returned"]),
            "remaining_refundable": [u["unit_id"] for u in unit_rows if not u["returned"]],
            "repeated": repeated,
        }

    # ---------------------------------------------------------------- return
    def return_units(
        self,
        request_id: str,
        sale_id: str,
        unit_ids: Any,
        grade: Any,
    ) -> Dict[str, Any]:
        request_id = require_id(request_id, INVALID_INPUT, "request_id")
        sale_id = require_id(sale_id, INVALID_INPUT, "sale_id")
        canonical_units = _validate_unit_ids(unit_ids)
        canonical_grade = _validate_grade(grade, canonical_units)
        canonical_request = canonical_json({"sale_id": sale_id, "unit_ids": canonical_units, "grade": canonical_grade})

        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM return_requests WHERE request_id = ?", (request_id,)).fetchone()
            if existing is not None:
                if existing["canonical_request"] != canonical_request:
                    raise BasketError(
                        "REQUEST_CONFLICT",
                        "return request_id reused with a different body",
                        {"request_id": request_id},
                    )
                return json_loads(existing["result_json"])
            if not canonical_units:
                raise BasketError(INVALID_INPUT, "a return request must contain at least one unit")

            sale = conn.execute("SELECT * FROM sales WHERE sale_id = ?", (sale_id,)).fetchone()
            if sale is None:
                raise BasketError(UNKNOWN_SALE, "unknown sale", {"sale_id": sale_id})
            rows = {
                row["unit_id"]: dict(row)
                for row in conn.execute("SELECT * FROM sale_units WHERE sale_id = ?", (sale_id,)).fetchall()
            }

            missing = [u for u in canonical_units if u not in rows]
            if missing:
                raise BasketError(
                    UNIT_INELIGIBLE,
                    "unit is not part of the bound sale",
                    {"sale_id": sale_id, "unit_ids": missing},
                )
            already = [u for u in canonical_units if rows[u]["returned"]]
            if already:
                raise BasketError(
                    UNIT_ALREADY_RETURNED,
                    "unit has already been returned; the whole request is rejected",
                    {"sale_id": sale_id, "unit_ids": already},
                )

            per_unit: List[Dict[str, Any]] = []
            refund_total = 0
            for unit_id in canonical_units:
                row = rows[unit_id]
                refund = row["paid"]  # stored original paid; never a new quote
                refund_total += refund
                per_unit.append(
                    {
                        "unit_id": unit_id,
                        "sku": row["sku"],
                        "grade": canonical_grade[unit_id],
                        "original_paid": row["paid"],
                        "refund": refund,
                    }
                )

            for item in per_unit:
                cursor = conn.execute(
                    "UPDATE sale_units SET returned = 1, return_request_id = ?, grade = ? WHERE sale_id = ? AND unit_id = ? AND returned = 0",
                    (request_id, item["grade"], sale_id, item["unit_id"]),
                )
                # The conditional UPDATE above is the unit-level guard; verify it
                # touched exactly one row before committing any inventory move.
                if cursor.rowcount != 1:  # pragma: no cover - defensive
                    raise BasketError(OVERLAPPING_RETURN, "unit changed concurrently", {"unit_id": item["unit_id"]})
                self._apply_inventory(
                    conn,
                    source_type="RETURN",
                    source_id=request_id,
                    item_id=item["unit_id"],
                    sku=item["sku"],
                    bucket=item["grade"],
                    delta=1,
                )

            refreshed = conn.execute(
                "SELECT unit_id, paid, returned FROM sale_units WHERE sale_id = ? ORDER BY unit_id", (sale_id,)
            ).fetchall()
            remaining = [row["unit_id"] for row in refreshed if not row["returned"]]
            cumulative = sum(row["paid"] for row in refreshed if row["returned"])
            result = {
                "ok": True,
                "request_id": request_id,
                "sale_id": sale_id,
                "items": per_unit,
                "refund_total": refund_total,
                "cumulative_refund": cumulative,
                "sale_total": sale["total"],
                "remaining_refundable": remaining,
                "inventory": self._inventory_snapshot(conn, {item["sku"] for item in per_unit}),
                "repeated": False,
            }
            conn.execute(
                "INSERT INTO return_requests (request_id, sale_id, canonical_request, result_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (request_id, sale_id, canonical_request, canonical_json(result), utc_now()),
            )
            for item in per_unit:
                conn.execute(
                    "INSERT INTO return_items (request_id, unit_id, sku, grade, original_paid, refund) VALUES (?, ?, ?, ?, ?, ?)",
                    (request_id, item["unit_id"], item["sku"], item["grade"], item["original_paid"], item["refund"]),
                )
            return result

    def get_return(self, request_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT result_json FROM return_requests WHERE request_id = ?", (request_id,)).fetchone()
        if row is None:
            raise BasketError("UNKNOWN_RETURN", "unknown return request", {"request_id": request_id})
        result = json_loads(row["result_json"])
        result["repeated"] = True
        return result

    # ------------------------------------------------------- inventory moves
    def adjust_inventory(self, event_id: str, sku: str, quantity: Any, bucket: str) -> Dict[str, Any]:
        event_id = require_id(event_id, INVALID_INPUT, "event_id")
        sku = require_id(sku, INVALID_INPUT, "sku")
        if not is_int(quantity) or quantity < 1:
            raise BasketError(INVALID_INPUT, "quantity must be a positive integer", {"quantity": quantity})
        if bucket not in BUCKETS:
            raise BasketError(INVALID_INPUT, "bucket must be RESTOCK or SCRAP", {"bucket": bucket})
        content = {"sku": sku, "quantity": quantity, "bucket": bucket}
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM adjustments WHERE event_id = ?", (event_id,)).fetchone()
            if existing is not None:
                if existing["content_json"] != canonical_json(content):
                    raise BasketError(
                        INVENTORY_CONFLICT,
                        "adjustment event_id reused with different content",
                        {"event_id": event_id},
                    )
                result = json_loads(existing["result_json"])
                result["repeated"] = True
                return result
            self._apply_inventory(
                conn,
                source_type="ADJUSTMENT",
                source_id=event_id,
                item_id="",
                sku=sku,
                bucket=bucket,
                delta=quantity,
            )
            result = {
                "ok": True,
                "event_id": event_id,
                **content,
                "inventory": self._inventory_snapshot(conn, {sku}),
                "repeated": False,
            }
            conn.execute(
                "INSERT INTO adjustments (event_id, content_json, result_json, created_at) VALUES (?, ?, ?, ?)",
                (event_id, canonical_json(content), canonical_json(result), utc_now()),
            )
            return result

    def inventory(self, skus: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, int]]:
        with self.db.connection() as conn:
            return self._inventory_snapshot(conn, None if skus is None else set(skus))

    def _inventory_snapshot(self, conn, skus: Optional[set[str]]) -> Dict[str, Dict[str, int]]:
        rows = conn.execute("SELECT sku, bucket, qty FROM inventory_totals ORDER BY sku, bucket").fetchall()
        result: Dict[str, Dict[str, int]] = {bucket: {} for bucket in BUCKETS}
        for row in rows:
            if skus is not None and row["sku"] not in skus:
                continue
            result[row["bucket"]][row["sku"]] = row["qty"]
        return result

    def _apply_inventory(self, conn, *, source_type: str, source_id: str, item_id: str, sku: str, bucket: str, delta: int) -> None:
        if bucket not in BUCKETS:  # pragma: no cover - callers validate
            raise BasketError(INVALID_INPUT, "invalid inventory bucket", {"bucket": bucket})
        conn.execute(
            "INSERT INTO inventory_events (source_type, source_id, item_id, sku, bucket, delta, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_type, source_id, item_id, sku, bucket, delta, utc_now()),
        )
        conn.execute(
            """
            INSERT INTO inventory_totals (sku, bucket, qty) VALUES (?, ?, ?)
            ON CONFLICT(sku, bucket) DO UPDATE SET qty = qty + excluded.qty
            """,
            (sku, bucket, delta),
        )

    # ---------------------------------------------------------------- audit
    def audit_inventory(self) -> Dict[str, Any]:
        """Recompute totals from raw inventory events (independent audit)."""

        with self.db.connection() as conn:
            rows = conn.execute("SELECT sku, bucket, SUM(delta) AS qty FROM inventory_events GROUP BY sku, bucket").fetchall()
            totals = {
                row["sku"]: {"RESTOCK": 0, "SCRAP": 0}
                for row in conn.execute("SELECT DISTINCT sku FROM inventory_events").fetchall()
            }
        for row in rows:
            totals.setdefault(row["sku"], {"RESTOCK": 0, "SCRAP": 0})[row["bucket"]] = row["qty"]
        return totals

    def audit_sales(self) -> List[Dict[str, Any]]:
        """Recompute refunds from original per-unit paid and return records."""

        audit: List[Dict[str, Any]] = []
        with self.db.connection() as conn:
            for sale in conn.execute("SELECT * FROM sales ORDER BY sale_id").fetchall():
                units = [dict(r) for r in conn.execute(
                    "SELECT * FROM sale_units WHERE sale_id = ? ORDER BY unit_id", (sale["sale_id"],)
                ).fetchall()]
                refundable = sum(u["paid"] for u in units)
                returned_ids = [u["unit_id"] for u in units if u["returned"]]
                refunded = sum(u["paid"] for u in units if u["returned"])
                audit.append(
                    {
                        "sale_id": sale["sale_id"],
                        "quote_id": sale["quote_id"],
                        "snapshot_id": sale["snapshot_id"],
                        "total": sale["total"],
                        "sum_paid": sum(u["paid"] for u in units),
                        "refunded_from_units": refunded,
                        "refundable_before_returns": refundable,
                        "returned_units": returned_ids,
                        "remaining_refundable": [u["unit_id"] for u in units if not u["returned"]],
                    }
                )
        return audit
