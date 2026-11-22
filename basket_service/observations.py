"""Observation packages rebuilt from the real, reopenable sales/refund ledger.

An *observation package* is the set of physical units covered by one coupon
that was actually consumed in a real transaction.  A coupon only ever covers a
frozen, mutually exclusive group of units (see ``quote_service``/``optimizer``);
every unit that did not take part in any coupon forms its own one-unit package
and stays at its full price.  The packages therefore partition each sale.

The reconstruction reads only persisted facts:

* ``sales`` / ``sale_units`` supply the member SKUs, the per-unit frozen paid
  amount and the id of the coupon actually used (``coupon_id``);
* ``return_requests`` supplies the real return time of each returned unit.

Nothing is re-priced and today's price tags are never consulted.  A package is
*complete* at a data cutoff only if none of its members was returned on or
before that cutoff.  If one member was returned, the whole promotion
observation is dead: the surviving units are never split out into "single-item"
observations, because they were never transacted on their own at those prices.

Package numbers are stable: they derive from the period sale order and the
coupon/unit ordering inside the sale, so reopening the database reproduces the
same numbers.  Each package also carries a content hash so the audit trail can
be checked independently.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .db import Database, canonical_json
from .errors import (
    CATEGORY_ERROR,
    LIMIT_EXCEEDED,
    UNKNOWN_PACKAGE,
    UNKNOWN_PERIOD,
    BasketError,
)
from .validation import is_int, require_id

# Contract limits from the brief.
MAX_PACKAGES_PER_PERIOD = 24
MAX_CATEGORIES = 8
MAX_CATEGORY_QUOTA = 12
MAX_PACKAGES_PER_PERIOD = 24
MAX_CATEGORIES = 8
MAX_CATEGORY_QUOTA = 12


@dataclass(frozen=True)
class UnitFact:
    """One physical member of an observation package."""

    unit_id: str
    sku: str
    paid: int  # frozen per-unit amount actually paid at transaction time
    coupon_id: Optional[str]  # None means the unit did not join a promotion
    return_time: Optional[str]  # persisted return-request timestamp, or None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "sku": self.sku,
            "paid": self.paid,
            "coupon_id": self.coupon_id,
            "return_time": self.return_time,
        }


@dataclass(frozen=True)
class PeriodSpec:
    """A named period: which sales belong to it and its data cutoff.

    The cutoff is compared lexicographically against the ISO-8601 return
    timestamps stored by the ledger, so the same string comparison works across
    process restarts.  A return whose timestamp is *equal* to the cutoff counts
    as "before the cutoff" (``return_time <= cutoff``) and kills the package.
    """

    name: str
    sale_ids: Tuple[str, ...]
    cutoff: str

    def __post_init__(self) -> None:
        require_id(self.name, UNKNOWN_PERIOD, "period name")
        if not isinstance(self.cutoff, str) or not self.cutoff:
            raise BasketError(UNKNOWN_PERIOD, "cutoff must be a non-empty timestamp string")
        if not self.sale_ids:
            raise BasketError(UNKNOWN_PERIOD, "a period must contain at least one sale", {"period": self.name})
        if len(set(self.sale_ids)) != len(self.sale_ids):
            raise BasketError(UNKNOWN_PERIOD, "duplicate sale_id in period", {"period": self.name})
        # tuple() of a generator/list for frozen-dataclass construction.
        object.__setattr__(self, "sale_ids", tuple(self.sale_ids))


@dataclass(frozen=True)
class ObservationPackage:
    """A frozen, auditable promotion (or standalone) observation."""

    package_id: str  # stable human number, e.g. "P007"
    package_no: int
    period: str
    sale_id: str
    coupon_id: Optional[str]  # None for a non-promoted one-unit package
    units: Tuple[UnitFact, ...]
    paid_total: int
    content_hash: str

    # ------------------------------------------------------------- helpers
    @property
    def skus(self) -> Tuple[str, ...]:
        return tuple(u.sku for u in self.units)

    @property
    def member_count(self) -> int:
        return len(self.units)

    @property
    def promoted_count(self) -> int:
        """Member units that actually went through a coupon."""

        return len(self.units) if self.coupon_id is not None else 0

    @property
    def return_times(self) -> Tuple[str, ...]:
        return tuple(u.return_time for u in self.units if u.return_time is not None)

    @property
    def earliest_return(self) -> Optional[str]:
        returns = self.return_times
        return min(returns) if returns else None

    def is_complete_at(self, cutoff: str) -> bool:
        """True iff no member was returned on or before ``cutoff``."""

        return all(return_time is None or return_time > cutoff for return_time in self.return_times)

    def category_vector(self, category_map: Mapping[str, str]) -> Tuple[Tuple[str, int], ...]:
        counts: Dict[str, int] = {}
        for unit in self.units:
            sku = unit.sku
            if sku not in category_map:
                raise BasketError(CATEGORY_ERROR, "sku has no approved research category", {"sku": sku})
            category = category_map[sku]
            counts[category] = counts.get(category, 0) + 1
        return tuple(sorted(counts.items()))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "package_id": self.package_id,
            "package_no": self.package_no,
            "period": self.period,
            "sale_id": self.sale_id,
            "coupon_id": self.coupon_id,
            "promoted": self.coupon_id is not None,
            "units": [u.to_dict() for u in self.units],
            "member_count": self.member_count,
            "promoted_count": self.promoted_count,
            "paid_total": self.paid_total,
            "content_hash": self.content_hash,
        }


def _package_hash(period: str, sale_id: str, coupon_id: Optional[str], units: Sequence[UnitFact]) -> str:
    payload = {
        "period": period,
        "sale_id": sale_id,
        "coupon_id": coupon_id,
        "units": [u.to_dict() for u in units],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class ObservationService:
    """Rebuild observation packages from a persisted basket ledger.

    The service is read-only with respect to the sales/refund ledger and is
    safe to construct repeatedly: every call re-reads the same SQLite rows and
    deterministically reproduces the same package numbers.
    """

    def __init__(self, db: Database, periods: Sequence[PeriodSpec]):
        self.db = db
        self.periods: Tuple[PeriodSpec, ...] = tuple(periods)
        if len(self.periods) != 2:
            raise BasketError(UNKNOWN_PERIOD, "the two-period comparison needs exactly two periods")
        if len({p.name for p in self.periods}) != 2:
            raise BasketError(UNKNOWN_PERIOD, "period names must be distinct")
        self._by_period: Dict[str, List[ObservationPackage]] = {}
        self._by_id: Dict[str, ObservationPackage] = {}
        self._rebuild()

    # ------------------------------------------------------------- rebuild
    def _read_sale(self, conn, sale_id: str) -> List[Dict[str, Any]]:
        sale = conn.execute("SELECT sale_id FROM sales WHERE sale_id = ?", (sale_id,)).fetchone()
        if sale is None:
            raise BasketError(UNKNOWN_PERIOD, "period references an unknown sale", {"sale_id": sale_id})
        rows = conn.execute(
            """
            SELECT su.sale_id, su.unit_id, su.sku, su.paid, su.coupon_id,
                   rr.created_at AS return_time
              FROM sale_units su
              LEFT JOIN return_requests rr ON rr.request_id = su.return_request_id
             WHERE su.sale_id = ?
             ORDER BY su.unit_id
            """,
            (sale_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _packages_for_sale(self, period: str, sale_id: str, rows: Sequence[Mapping[str, Any]], counter: List[int]) -> List[ObservationPackage]:
        # Promoted groups first, in coupon order; standalone units afterwards in
        # unit order.  Every unit appears exactly once.
        coupon_order: List[str] = sorted({r["coupon_id"] for r in rows if r["coupon_id"] is not None})
        groups: List[Tuple[Optional[str], List[Mapping[str, Any]]]] = []
        assigned: set[str] = set()
        for coupon_id in coupon_order:
            members = [r for r in rows if r["coupon_id"] == coupon_id]
            if not members:  # pragma: no cover - defensive
                continue
            groups.append((coupon_id, members))
            assigned.update(r["unit_id"] for r in members)
        for row in rows:
            if row["unit_id"] not in assigned:
                groups.append((None, [row]))

        packages: List[ObservationPackage] = []
        for coupon_id, members in groups:
            counter[0] += 1
            package_no = counter[0]
            units = tuple(
                UnitFact(
                    unit_id=r["unit_id"],
                    sku=r["sku"],
                    paid=int(r["paid"]),
                    coupon_id=r["coupon_id"],
                    return_time=r["return_time"],
                )
                for r in sorted(members, key=lambda r: r["unit_id"])
            )
            paid_total = sum(u.paid for u in units)
            package = ObservationPackage(
                package_id=f"P{package_no:03d}",
                package_no=package_no,
                period=period,
                sale_id=sale_id,
                coupon_id=coupon_id,
                units=units,
                paid_total=paid_total,
                content_hash=_package_hash(period, sale_id, coupon_id, units),
            )
            packages.append(package)
        return packages

    def _rebuild(self) -> None:
        counter = [0]
        all_sale_ids = [sale_id for period in self.periods for sale_id in period.sale_ids]
        if len(set(all_sale_ids)) != len(all_sale_ids):
            raise BasketError(UNKNOWN_PERIOD, "a sale cannot belong to both periods")
        with self.db.connection() as conn:
            for period in self.periods:
                packages: List[ObservationPackage] = []
                for sale_id in period.sale_ids:
                    rows = self._read_sale(conn, sale_id)
                    packages.extend(self._packages_for_sale(period.name, sale_id, rows, counter))
                if len(packages) > MAX_PACKAGES_PER_PERIOD:
                    raise BasketError(
                        LIMIT_EXCEEDED,
                        "period contains more than 24 observation packages",
                        {"period": period.name, "packages": len(packages)},
                    )
                self._by_period[period.name] = packages
                for package in packages:
                    self._by_id[package.package_id] = package

    # ------------------------------------------------------------- access
    @property
    def period_names(self) -> Tuple[str, str]:
        return (self.periods[0].name, self.periods[1].name)

    def packages(self, period: str) -> List[ObservationPackage]:
        self._require_period(period)
        return list(self._by_period[period])

    def get_package(self, package_id: str) -> ObservationPackage:
        package = self._by_id.get(package_id)
        if package is None:
            raise BasketError(UNKNOWN_PACKAGE, "unknown observation package", {"package_id": package_id})
        return package

    def _require_period(self, period: str) -> PeriodSpec:
        for spec in self.periods:
            if spec.name == period:
                return spec
        raise BasketError(UNKNOWN_PERIOD, "unknown period", {"period": period})

    def cutoff_for(self, period: str) -> str:
        return self._require_period(period).cutoff

    def complete_packages(self, period: str) -> List[ObservationPackage]:
        cutoff = self.cutoff_for(period)
        return [p for p in self.packages(period) if p.is_complete_at(cutoff)]

    # ------------------------------------------------------------- audit
    def audit_package(self, package_id: str, *, period: Optional[str] = None) -> Dict[str, Any]:
        """Explain, from raw ledger facts, why a package is complete or dead.

        Merchants do not need to run the price-index machinery to use this: the
        cutoff, the member return times and the offending unit are all surfaced.
        """

        package = self.get_package(package_id)
        effective_period = period or package.period
        cutoff = self.cutoff_for(effective_period)
        returned_before = [
            {
                "unit_id": u.unit_id,
                "sku": u.sku,
                "return_time": u.return_time,
            }
            for u in package.units
            if u.return_time is not None and u.return_time <= cutoff
        ]
        returned_after = [
            {"unit_id": u.unit_id, "return_time": u.return_time}
            for u in package.units
            if u.return_time is not None and u.return_time > cutoff
        ]
        return {
            "package_id": package.package_id,
            "period": effective_period,
            "cutoff": cutoff,
            "status": "COMPLETE" if not returned_before else "INVALID_RETURNED_MEMBER",
            "sale_id": package.sale_id,
            "coupon_id": package.coupon_id,
            "members": [u.to_dict() for u in package.units],
            "returned_before_cutoff": returned_before,
            "returned_after_cutoff": returned_after,
            "rule": "a package is complete only if every member return_time is null or later than the cutoff; "
            "a returned member invalidates the whole package and the survivors are never split out",
            "content_hash": package.content_hash,
        }

    def audit_period(self, period: str) -> Dict[str, Any]:
        spec = self._require_period(period)
        packages = self.packages(period)
        rows = [self.audit_package(p.package_id, period=period) for p in packages]
        return {
            "period": period,
            "cutoff": spec.cutoff,
            "package_count": len(packages),
            "complete": [r["package_id"] for r in rows if r["status"] == "COMPLETE"],
            "invalid": [
                {
                    "package_id": r["package_id"],
                    "returned_before_cutoff": r["returned_before_cutoff"],
                }
                for r in rows
                if r["status"] != "COMPLETE"
            ],
            "packages": rows,
        }

    def rebuild_audit(self) -> Dict[str, Any]:
        """Re-read every raw ledger row and reconcile it against the packages."""

        with self.db.connection() as conn:
            raw_units = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT su.sale_id, su.unit_id, su.sku, su.paid, su.coupon_id,
                           rr.created_at AS return_time
                      FROM sale_units su
                      LEFT JOIN return_requests rr ON rr.request_id = su.return_request_id
                     ORDER BY su.sale_id, su.unit_id
                    """
                ).fetchall()
            ]
        covered = [(p.sale_id, u.unit_id) for p in self._by_id.values() for u in p.units]
        raw = [(r["sale_id"], r["unit_id"]) for r in raw_units]
        return {
            "raw_unit_rows": len(raw),
            "packaged_unit_rows": len(covered),
            "every_unit_packaged_exactly_once": sorted(covered) == sorted(raw),
            "package_count": len(self._by_id),
            "periods": {name: [p.package_id for p in packages] for name, packages in self._by_period.items()},
        }


def validate_category_map(
    category_map: Mapping[str, Any],
    quotas: Mapping[str, Any],
    *,
    known_skus: Optional[Sequence[str]] = None,
    version: str = "",
) -> Dict[str, str]:
    """Validate a research-group approved SKU -> category version."""

    if not isinstance(category_map, Mapping) or not category_map:
        raise BasketError(CATEGORY_ERROR, "category_map must be a non-empty sku -> category mapping")
    canonical: Dict[str, str] = {}
    for sku, category in category_map.items():
        if not isinstance(sku, str) or not sku:
            raise BasketError(CATEGORY_ERROR, "category_map keys must be non-empty sku strings")
        if not isinstance(category, str) or not category:
            raise BasketError(CATEGORY_ERROR, "category names must be non-empty strings", {"sku": sku})
        canonical[sku] = category
    if known_skus is not None:
        missing = sorted(set(known_skus) - set(canonical))
        if missing:
            raise BasketError(CATEGORY_ERROR, "some transacted skus lack an approved category", {"skus": missing})
    categories = sorted(set(canonical.values()))
    if len(categories) > MAX_CATEGORIES:
        raise BasketError(LIMIT_EXCEEDED, "at most 8 research categories are supported", {"categories": categories})
    if not isinstance(quotas, Mapping) or not quotas:
        raise BasketError(CATEGORY_ERROR, "quotas must be a non-empty category -> target mapping")
    for category, target in quotas.items():
        if category not in categories:
            raise BasketError(CATEGORY_ERROR, "quota references a category absent from the mapping", {"category": category})
        if not is_int(target) or target < 0 or target > MAX_CATEGORY_QUOTA:
            raise BasketError(
                LIMIT_EXCEEDED,
                "each category quota must be an integer in [0, 12]",
                {"category": category, "target": target},
            )
    for category in categories:
        if category not in quotas:
            raise BasketError(CATEGORY_ERROR, "every mapped category needs a pre-registered quota", {"category": category})
    return {"version": version, "mapping": canonical, "quotas": dict(quotas), "categories": categories}
