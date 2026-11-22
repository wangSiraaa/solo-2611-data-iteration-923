"""Two-period balanced sample selection and the price comparison report.

The research group pre-registers, per period, a category quota (the maximum
number of units sampled in each approved category).  A report chooses two sets
of *complete* observation packages, one per period, such that:

* the per-category unit counts of the two chosen sets are identical;
* every per-category count stays within the period's quota;
* each package is used at most once.

Selection optimises, strictly in this order:

1. maximise the balanced total unit count;
2. among those, maximise the total number of units that actually went through a
   promotion across every selected package of both periods;
3. among those, take the lexicographically smallest combined stable package-id
   list (early ids, then late ids), so neither cheap nor expensive samples can
   be hand-picked.

At the stated limits (at most 24 packages a period, at most 8 categories, quota
at most 12) a subset DP over category-count tuples is exact.  Each reachable
count tuple lies inside the quota box, so there are at most
``prod(quota_c + 1) <= 13**8`` positions, and in practice far fewer because only
24 packages exist; cross-tuple dominance pruning is deliberately *not* used,
since the two periods must finish on identical tuples and a tuple dominated in
one period may be the only match available in the other.  The DP is an offline
step; ordinary inputs finish in milliseconds, while a deliberately adversarial
input that makes nearly every subset reach a distinct in-box tuple takes a few
seconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import CATEGORY_ERROR, BasketError
from .observations import ObservationService, ObservationPackage, validate_category_map

Vector = Tuple[Tuple[str, int], ...]
# Internal DP keys are plain integer count tuples aligned with ``categories``.
# Avoiding (name, count) pairs in the hot loop removes tens of millions of
# tuple/dict allocations at the 24-package, 8-category limit.
Counts = Tuple[int, ...]


def _full_vector(counts: Mapping[str, int], categories: Sequence[str]) -> Vector:
    """Fixed-length vector over the shared category universe (zeros included).

    Keeping the zero entries is what makes the same counts compare equal across
    periods even when one period never transacts a category the other uses.
    """

    return tuple((c, int(counts.get(c, 0))) for c in categories)


def _to_vector(counts: Counts, categories: Sequence[str]) -> Vector:
    return tuple((c, value) for c, value in zip(categories, counts))


def _add_counts(left: Counts, right: Counts) -> Counts:
    return tuple(a + b for a, b in zip(left, right))


def _within_bounds(counts: Counts, bounds: Counts) -> bool:
    return all(value <= bound for value, bound in zip(counts, bounds))


def _counts_sum(counts: Counts) -> int:
    return sum(counts)


def _drop_zeros(vector: Vector) -> Vector:
    return tuple((category, count) for category, count in vector if count)


@dataclass(frozen=True)
class Candidate:
    """A reachable category-count tuple with its best realising subset."""

    counts: Counts
    promoted: int
    ids: Tuple[str, ...]
    paid_total: int
    package_count: int


def _better(candidate: Candidate, promoted: int, ids: Tuple[str, ...]) -> bool:
    """Objective 2 then objective 3 (the count tuple is already fixed)."""

    if promoted != candidate.promoted:
        return promoted > candidate.promoted
    return list(ids) < list(candidate.ids)


def enumerate_choices(
    packages: Sequence[ObservationPackage],
    category_map: Mapping[str, str],
    categories: Sequence[str],
    quotas: Mapping[str, int],
) -> Dict[Counts, Candidate]:
    """Exact subset DP: best subset per reachable category-count tuple.

    Every reachable count tuple is retained: cross-vector dominance pruning
    would be unsafe because the two periods must finish on *identical* counts,
    so a tuple that looks dominated inside one period may be the only
    counterpart a package on the other side can reach.  Keys are plain integer
    tuples aligned with ``categories``, keeping addition and quota checks as
    cheap tuple arithmetic.  Packages are processed in ascending package-id
    order, so each subset's id tuple is built by plain append and stays sorted
    for the objective-3 tie-break.
    """

    zero: Counts = tuple(0 for _ in categories)
    bounds = tuple(quotas[c] for c in categories)
    package_terms: List[Tuple[Counts, int, int, str]] = []
    for package in sorted(packages, key=lambda p: p.package_id):
        counts = {category: 0 for category in categories}
        for category, count in package.category_vector(category_map):
            counts[category] = count
        package_terms.append(
            (
                tuple(counts[c] for c in categories),
                package.promoted_count,
                package.paid_total,
                package.package_id,
            )
        )

    states: Dict[Counts, Candidate] = {
        zero: Candidate(counts=zero, promoted=0, ids=(), paid_total=0, package_count=0)
    }
    for package_counts, promoted_add, paid_add, package_id in package_terms:
        # Snapshot the states reachable *without* this package.  Iterating this
        # frozen snapshot (and never states inserted below) is the 0/1 knapsack
        # guard: otherwise a package could be folded in twice within its own
        # round.  Package ids are ascending, so appending keeps ids sorted.
        snapshot = list(states.items())
        for current, candidate in snapshot:
            merged = _add_counts(current, package_counts)
            if not _within_bounds(merged, bounds):
                continue
            promoted = candidate.promoted + promoted_add
            ids = candidate.ids + (package_id,)
            existing = states.get(merged)
            if existing is None or _better(existing, promoted, ids):
                states[merged] = Candidate(
                    counts=merged,
                    promoted=promoted,
                    ids=ids,
                    paid_total=candidate.paid_total + paid_add,
                    package_count=candidate.package_count + 1,
                )
    return states


def _joint_key(candidate: Candidate, other: Candidate) -> Tuple[int, Tuple[str, ...]]:
    return candidate.promoted + other.promoted, tuple(sorted(candidate.ids + other.ids))


@dataclass(frozen=True)
class PeriodSelection:
    name: str
    cutoff: str
    package_ids: Tuple[str, ...]
    category_counts: Vector
    unit_count: int
    promoted_units: int
    paid_total: int


@dataclass(frozen=True)
class SampleReport:
    is_empty: bool
    balanced_units: int
    promoted_units_total: int
    category_counts: Vector
    early: PeriodSelection
    late: PeriodSelection
    package_count_difference: int
    paid_ratio: Optional[Fraction]
    ratio_undefined: bool
    raw_ratio: Optional[Tuple[int, int]]
    category_version_early: str
    category_version_late: str
    quotas: Dict[str, Dict[str, int]]

    def to_dict(self) -> Dict[str, Any]:
        if self.paid_ratio is None or self.raw_ratio is None:
            ratio: Any = None
            raw: Any = None
            ratio_text = "UNDEFINED (early paid total is zero)"
        else:
            ratio = {"numerator": self.paid_ratio.numerator, "denominator": self.paid_ratio.denominator}
            raw = {"numerator": self.raw_ratio[0], "denominator": self.raw_ratio[1]}
            ratio_text = (
                f"{self.raw_ratio[0]}/{self.raw_ratio[1]}"
                f" (= {self.paid_ratio.numerator}/{self.paid_ratio.denominator})"
                if (self.paid_ratio.numerator, self.paid_ratio.denominator) != self.raw_ratio
                else f"{self.raw_ratio[0]}/{self.raw_ratio[1]}"
            )
        return {
            "is_empty": self.is_empty,
            "balanced_units": self.balanced_units,
            "promoted_units_total": self.promoted_units_total,
            "category_counts": dict(_drop_zeros(self.category_counts)),
            "early": {
                "period": self.early.name,
                "cutoff": self.early.cutoff,
                "packages": list(self.early.package_ids),
                "package_count": len(self.early.package_ids),
                "category_counts": dict(_drop_zeros(self.early.category_counts)),
                "unit_count": self.early.unit_count,
                "promoted_units": self.early.promoted_units,
                "paid_total": self.early.paid_total,
                "category_version": self.category_version_early,
            },
            "late": {
                "period": self.late.name,
                "cutoff": self.late.cutoff,
                "packages": list(self.late.package_ids),
                "package_count": len(self.late.package_ids),
                "category_counts": dict(_drop_zeros(self.late.category_counts)),
                "unit_count": self.late.unit_count,
                "promoted_units": self.late.promoted_units,
                "paid_total": self.late.paid_total,
                "category_version": self.category_version_late,
            },
            "package_count_difference_late_minus_early": self.package_count_difference,
            "paid_ratio_late_over_early": ratio,
            "paid_ratio_raw_integer_totals": raw,
            "paid_ratio_text": ratio_text,
            "quotas": self.quotas,
        }


def _empty_report(
    service: ObservationService,
    versions: Tuple[str, str],
    quotas: Dict[str, Dict[str, int]],
    universe: Sequence[str],
) -> SampleReport:
    early_spec, late_spec = service.periods
    zero: Vector = _full_vector({}, universe)
    early = PeriodSelection(early_spec.name, early_spec.cutoff, (), zero, 0, 0, 0)
    late = PeriodSelection(late_spec.name, late_spec.cutoff, (), zero, 0, 0, 0)
    return SampleReport(
        is_empty=True,
        balanced_units=0,
        promoted_units_total=0,
        category_counts=zero,
        early=early,
        late=late,
        package_count_difference=0,
        paid_ratio=None,
        ratio_undefined=True,
        raw_ratio=None,
        category_version_early=versions[0],
        category_version_late=versions[1],
        quotas=quotas,
    )


def build_report(
    service: ObservationService,
    category_maps: Mapping[str, Mapping[str, str]],
    quotas: Mapping[str, Mapping[str, int]],
    *,
    versions: Optional[Tuple[str, str]] = ("", ""),
) -> SampleReport:
    """Select the balanced two-period sample and assemble the frozen report."""

    early_name, late_name = service.period_names
    if set(category_maps) != {early_name, late_name}:
        raise BasketError(
            CATEGORY_ERROR,
            "category_maps must provide one mapping per period",
            {"expected": [early_name, late_name], "got": sorted(category_maps)},
        )
    if set(quotas) != {early_name, late_name}:
        raise BasketError(CATEGORY_ERROR, "quotas must provide one mapping per period")

    period_inputs: Dict[str, Tuple[List[ObservationPackage], Dict[str, str], Tuple[str, ...], Dict[str, int]]] = {}
    for name in (early_name, late_name):
        complete = service.complete_packages(name)
        mapping = dict(category_maps[name])
        known = sorted({u.sku for p in complete for u in p.units})
        # Every SKU on a *complete* package must be mappable; returned packages
        # cannot enter the sample but are still auditable from observations.
        missing = [sku for sku in known if sku not in mapping]
        if missing:
            raise BasketError(CATEGORY_ERROR, "complete package sku lacks an approved category", {"period": name, "skus": missing})
        # Enforce the pre-registered limits (<=8 categories, quota in [0,12]);
        # every mapped category must carry an explicit quota and vice versa.
        period_version = (versions or ("", ""))[0 if name == early_name else 1]
        validated = validate_category_map(mapping, quotas[name], known_skus=known, version=period_version)
        period_quotas = dict(validated["quotas"])
        categories = tuple(validated["categories"])
        period_inputs[name] = (complete, mapping, categories, period_quotas)

    early_packages, early_map, early_categories, early_quotas = period_inputs[early_name]
    late_packages, late_map, late_categories, late_quotas = period_inputs[late_name]

    # The comparison universe is the union of categories that the research group
    # actually approved for either period.  A category absent from a period's
    # mapping is not inserted with a zero quota there: its packages cannot exist
    # anyway, and inventing a zero cell would reject legal combinations.  An
    # explicit zero quota still blocks a category that *is* mapped in a period.
    universe = tuple(sorted(set(early_categories) | set(late_categories)))
    for category in universe:
        early_quotas.setdefault(category, 0)
        late_quotas.setdefault(category, 0)

    early_states = enumerate_choices(early_packages, early_map, universe, early_quotas)
    late_states = enumerate_choices(late_packages, late_map, universe, late_quotas)

    common = set(early_states) & set(late_states)
    # Objective 1: largest balanced unit count.  The zero tuple is always
    # reachable on both sides, so a zero-balanced report always exists.
    best_counts = max(common, key=_counts_sum)
    best_count = _counts_sum(best_counts)
    if best_count == 0:
        return _empty_report(
            service,
            versions or ("", ""),
            {early_name: dict(early_quotas), late_name: dict(late_quotas)},
            universe,
        )
    tied = [counts for counts in common if _counts_sum(counts) == best_count]

    # Objective 2 then 3 across the two periods.
    best: Optional[Tuple[Counts, Candidate, Candidate]] = None
    for counts in tied:
        early_candidate = early_states[counts]
        late_candidate = late_states[counts]
        promoted, joint_ids = _joint_key(early_candidate, late_candidate)
        if best is None:
            best = (counts, early_candidate, late_candidate)
            continue
        best_promoted, best_joint_ids = _joint_key(best[1], best[2])
        if promoted > best_promoted or (promoted == best_promoted and list(joint_ids) < list(best_joint_ids)):
            best = (counts, early_candidate, late_candidate)

    assert best is not None
    counts, early_candidate, late_candidate = best
    vector = _to_vector(counts, universe)
    early_spec, late_spec = service.periods
    early_selection = PeriodSelection(
        name=early_name,
        cutoff=early_spec.cutoff,
        package_ids=early_candidate.ids,
        category_counts=vector,
        unit_count=best_count,
        promoted_units=early_candidate.promoted,
        paid_total=early_candidate.paid_total,
    )
    late_selection = PeriodSelection(
        name=late_name,
        cutoff=late_spec.cutoff,
        package_ids=late_candidate.ids,
        category_counts=vector,
        unit_count=best_count,
        promoted_units=late_candidate.promoted,
        paid_total=late_candidate.paid_total,
    )

    # Freeze-total ratio as an exact rational; a zero early total is reported
    # explicitly as undefined rather than raising ZeroDivisionError.  The raw
    # integer-total pair is kept alongside the reduced fraction.
    raw_ratio: Optional[Tuple[int, int]]
    if early_selection.paid_total == 0:
        paid_ratio: Optional[Fraction] = None
        ratio_undefined = True
        raw_ratio = None
    else:
        paid_ratio = Fraction(late_selection.paid_total, early_selection.paid_total)
        ratio_undefined = False
        raw_ratio = (late_selection.paid_total, early_selection.paid_total)

    return SampleReport(
        is_empty=False,
        balanced_units=best_count,
        promoted_units_total=early_candidate.promoted + late_candidate.promoted,
        category_counts=vector,
        early=early_selection,
        late=late_selection,
        package_count_difference=late_candidate.package_count - early_candidate.package_count,
        paid_ratio=paid_ratio,
        ratio_undefined=ratio_undefined,
        raw_ratio=raw_ratio,
        category_version_early=(versions or ("", ""))[0],
        category_version_late=(versions or ("", ""))[1],
        quotas={early_name: dict(early_quotas), late_name: dict(late_quotas)},
    )
