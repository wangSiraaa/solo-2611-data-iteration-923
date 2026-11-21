"""Strict validation and canonicalisation of baskets, coupons and quotes.

The quote contract is intentionally narrow:

* a basket is an ordered-independent list of physical units, each carrying a
  unique ``unit_id`` (string), a ``sku`` (string) and a positive integer price;
* a coupon has a unique ``coupon_id``, a positive integer ``D`` and a
  ``requires`` mapping ``sku -> positive integer count`` whose keys must be the
  SKUs explicitly listed on the basket;
* booleans are rejected wherever an integer is expected.

Everything is copied into fresh canonical objects so callers can never mutate
state that is later persisted.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .errors import INVALID_BASKET, INVALID_COUPON, INVALID_QUOTE, BasketError


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def require_int(value: Any, code: str, field: str, *, minimum: int = 1) -> int:
    if not is_int(value) or value < minimum:
        raise BasketError(code, f"{field} must be an integer >= {minimum}", {"field": field, "value": value})
    return value


def require_id(value: Any, code: str, field: str) -> str:
    if not isinstance(value, str) or value == "":
        raise BasketError(code, f"{field} must be a non-empty string", {"field": field, "value": value})
    return value


def validate_basket(basket: Any) -> List[Dict[str, Any]]:
    if basket is None or isinstance(basket, (str, bytes, Mapping)):
        raise BasketError(INVALID_BASKET, "basket must be a list of unit objects")
    if not isinstance(basket, Sequence):
        raise BasketError(INVALID_BASKET, "basket must be a list of unit objects")

    units: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(basket):
        if not isinstance(item, Mapping):
            raise BasketError(INVALID_BASKET, "basket entries must be objects", {"index": index})
        unit_id = require_id(item.get("unit_id"), INVALID_BASKET, f"basket[{index}].unit_id")
        if unit_id in seen:
            raise BasketError(INVALID_BASKET, "duplicate unit_id in basket", {"unit_id": unit_id})
        seen.add(unit_id)
        sku = require_id(item.get("sku"), INVALID_BASKET, f"basket[{index}].sku")
        price = require_int(item.get("price"), INVALID_BASKET, f"basket[{index}].price")
        units.append({"unit_id": unit_id, "sku": sku, "price": price})
    units.sort(key=lambda u: u["unit_id"])
    return units


def validate_coupons(coupons: Any, skus: set[str], *, allow_absent_skus: bool = False) -> List[Dict[str, Any]]:
    if coupons is None or isinstance(coupons, (str, bytes, Mapping)):
        raise BasketError(INVALID_COUPON, "coupons must be a list of coupon objects")
    if not isinstance(coupons, Sequence):
        raise BasketError(INVALID_COUPON, "coupons must be a list of coupon objects")

    result: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(coupons):
        if not isinstance(item, Mapping):
            raise BasketError(INVALID_COUPON, "coupon entries must be objects", {"index": index})
        coupon_id = require_id(item.get("coupon_id"), INVALID_COUPON, f"coupons[{index}].coupon_id")
        if coupon_id in seen:
            raise BasketError(INVALID_COUPON, "duplicate coupon_id", {"coupon_id": coupon_id})
        seen.add(coupon_id)
        discount = require_int(item.get("D"), INVALID_COUPON, f"coupons[{index}].D")
        requires = item.get("requires")
        if not isinstance(requires, Mapping) or len(requires) == 0:
            raise BasketError(INVALID_COUPON, "requires must be a non-empty sku->count mapping", {"coupon_id": coupon_id})
        canonical_requires: Dict[str, int] = {}
        for sku, count in requires.items():
            if not isinstance(sku, str) or sku == "":
                raise BasketError(INVALID_COUPON, "requires keys must be non-empty sku strings", {"coupon_id": coupon_id})
            if sku not in skus and not allow_absent_skus:
                raise BasketError(
                    INVALID_COUPON,
                    "coupon requires a sku that is not present in the basket",
                    {"coupon_id": coupon_id, "sku": sku},
                )
            canonical_requires[sku] = require_int(count, INVALID_COUPON, f"coupons[{index}].requires[{sku!r}]")
        result.append({"coupon_id": coupon_id, "D": discount, "requires": canonical_requires})
    result.sort(key=lambda c: c["coupon_id"])
    return result


def _round_ratio(total: int, weight: int, weight_sum: int) -> int:
    return (total * weight) // weight_sum


def allocate_report(units: Sequence[Mapping[str, Any]], used: Mapping[str, int], discounts: Mapping[str, int]) -> List[Dict[str, Any]]:
    """Allocate each coupon discount proportionally inside its participating units.

    ``units`` is the validated, unit_id-sorted basket.  ``used`` maps a coupon
    id to the bitmask of units it consumes; ``discounts`` maps the same coupon
    id to its full D.  The returned list is sorted by unit_id, carries the
    original price, the coupon identity, the allocated discount and the integer
    amount actually paid.  Floors are taken first, then remaining cents go to
    the largest fractional remainders, ties by unit_id ascending.
    """

    allocation: Dict[int, int] = {}
    owner: Dict[int, str] = {}
    for coupon_id, mask in used.items():
        idxs = [i for i in range(len(units)) if mask >> i & 1]
        weight_sum = sum(units[i]["price"] for i in idxs)
        exact_numerator = [(discounts[coupon_id] * units[i]["price"], i) for i in idxs]
        floors = {i: num // weight_sum for num, i in exact_numerator}
        remainders = {i: num % weight_sum for num, i in exact_numerator}
        remaining = discounts[coupon_id] - sum(floors.values())
        assert 0 <= remaining < len(idxs)
        for i in sorted(idxs, key=lambda i: (-remainders[i], units[i]["unit_id"]))[:remaining]:
            floors[i] += 1
        for i in idxs:
            allocation[i] = floors[i]
            owner[i] = coupon_id

    report: List[Dict[str, Any]] = []
    for i, unit in enumerate(units):
        discount = allocation.get(i, 0)
        if discount > unit["price"]:  # defensive: cannot happen for legal allocations
            raise BasketError(INVALID_QUOTE, "allocated discount exceeds unit price", {"unit_id": unit["unit_id"]})
        report.append(
            {
                "unit_id": unit["unit_id"],
                "sku": unit["sku"],
                "price": unit["price"],
                "coupon_id": owner.get(i),
                "discount": discount,
                "paid": unit["price"] - discount,
            }
        )
    return report


def validate_quote_payload(payload: Any) -> Dict[str, Any]:
    """Validate a quote payload supplied by an external, trusted quote issuer.

    The payload must be a *complete* best quote for the basket: it carries the
    basket, the coupon catalogue, the allocation list and the total.  We verify
    structural legality without re-running the optimizer.  This is what allows
    a ledger to consume an independent quote service while still refusing
    internally inconsistent contracts.
    """

    if not isinstance(payload, Mapping):
        raise BasketError(INVALID_QUOTE, "quote must be an object")
    try:
        request_id = require_id(payload.get("request_id"), INVALID_QUOTE, "request_id")
        snapshot_id = require_id(payload.get("snapshot_id"), INVALID_QUOTE, "snapshot_id")
        total = require_int(payload.get("total"), INVALID_QUOTE, "total", minimum=0)
        basket = validate_basket(payload.get("basket"))
        skus = {u["sku"] for u in basket}
        coupons = validate_coupons(payload.get("coupons"), skus)
        assignments_raw = payload.get("assignments")
        masks = validate_assignments(assignments_raw, basket, coupons)
        assignments = {
            cid: [basket[i]["unit_id"] for i in range(len(basket)) if mask >> i & 1]
            for cid, mask in masks.items()
        }
        allocation = payload.get("allocation")
        if not isinstance(allocation, Sequence) or isinstance(allocation, (str, bytes)):
            raise BasketError(INVALID_QUOTE, "allocation must be a list")
        report = allocate_report(basket, masks, {c["coupon_id"]: c["D"] for c in coupons})
        if list(allocation) != report:
            raise BasketError(INVALID_QUOTE, "allocation does not match the reassembled proportional allocation")
        if sum(entry["paid"] for entry in report) != total:
            raise BasketError(INVALID_QUOTE, "total does not equal the sum of per-unit paid amounts")
        return {
            "request_id": request_id,
            "snapshot_id": snapshot_id,
            "basket": basket,
            "coupons": coupons,
            "assignments": assignments,
            "allocation": report,
            "total": total,
        }
    except BasketError as exc:
        if exc.code == INVALID_QUOTE:
            raise
        raise BasketError(INVALID_QUOTE, exc.message, exc.details) from exc


def validate_assignments(assignments: Any, basket: Sequence[Mapping[str, Any]], coupons: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """Validate the assignment mapping ``coupon_id -> sorted unit_id list``.

    Returns the same mapping as ``coupon_id -> bitmask`` over the sorted basket.
    Raises INVALID_QUOTE (or the supplied code) for illegal assignments: a
    coupon that is not in the catalogue, the wrong number of units per SKU, a
    unit used twice, or a discount exceeding the selected units' prices.
    """

    code = INVALID_QUOTE
    if not isinstance(assignments, Mapping):
        raise BasketError(code, "assignments must be a coupon_id -> unit_id list mapping")
    price = {u["unit_id"]: u["price"] for u in basket}
    sku = {u["unit_id"]: u["sku"] for u in basket}
    index = {u["unit_id"]: i for i, u in enumerate(basket)}
    coupon_by_id = {c["coupon_id"]: c for c in coupons}
    used_units: set[str] = set()
    masks: Dict[str, int] = {}
    for coupon_id, unit_ids in assignments.items():
        if coupon_id not in coupon_by_id:
            raise BasketError(code, "assignment references an unknown coupon", {"coupon_id": coupon_id})
        if not isinstance(unit_ids, Sequence) or isinstance(unit_ids, (str, bytes)):
            raise BasketError(code, "assignment unit list must be a list", {"coupon_id": coupon_id})
        chosen = list(unit_ids)
        if len(set(chosen)) != len(chosen):
            raise BasketError(code, "assignment reuses a physical unit", {"coupon_id": coupon_id})
        for unit_id in chosen:
            if unit_id not in price:
                raise BasketError(code, "assignment references an unknown unit", {"coupon_id": coupon_id, "unit_id": unit_id})
            if unit_id in used_units:
                raise BasketError(code, "physical unit participates in more than one coupon", {"unit_id": unit_id})
        used_units.update(chosen)
        counts: Dict[str, int] = {}
        for unit_id in chosen:
            counts[sku[unit_id]] = counts.get(sku[unit_id], 0) + 1
        if counts != dict(coupon_by_id[coupon_id]["requires"]):
            raise BasketError(
                code,
                "selected units do not satisfy the coupon sku requirements",
                {"coupon_id": coupon_id, "selected": counts, "requires": dict(coupon_by_id[coupon_id]["requires"])},
            )
        if sum(price[u] for u in chosen) < coupon_by_id[coupon_id]["D"]:
            raise BasketError(code, "coupon discount exceeds the selected units' original prices", {"coupon_id": coupon_id})
        mask = 0
        for unit_id in chosen:
            mask |= 1 << index[unit_id]
        masks[coupon_id] = mask
    return dict(sorted(masks.items()))
