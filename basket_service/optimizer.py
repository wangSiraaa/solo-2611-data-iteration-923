"""Exact minimum-price optimizer with deterministic two-level tie breaking.

Formally, for a fixed basket and coupon catalogue we choose a set of coupons
and a disjoint selection of physical units per coupon such that every coupon's
SKU counts are met and the selected units' original prices cover its discount.
The basket total is ``sum(price) - sum(D)`` so the optimum maximises the total
discount; unit choice only affects legality and tie breaking.

The optimizer is exact and deterministic:

1. compute the maximum achievable discount with a bitmask dynamic program;
2. pick the lexicographically smallest sorted list of used coupon ids among
   feasible assignments reaching that discount;
3. greedily fix, coupon id by coupon id, the lexicographically smallest sorted
   unit-id list that still admits a completion.

Step 3 is a standard prefix-greedy argument: the tie-break compares the tuple
of per-coupon unit lists component-wise, and feasibility of the remaining
coupons only depends on which units are already consumed, so choosing the
smallest viable component never hurts optimality.
"""

from __future__ import annotations

import itertools
from functools import lru_cache
from typing import Any, Dict, List, Mapping, Sequence, Tuple

# A coupon mask is the set of basket indices (basket is sorted by unit_id).
Mask = int


class _Solver:
    def __init__(self, units: Sequence[Mapping[str, Any]], coupons: Sequence[Mapping[str, Any]]):
        self.units = units
        self.coupons = coupons
        self.prices = [u["price"] for u in units]
        self.by_sku: Dict[str, List[int]] = {}
        for index, unit in enumerate(units):
            self.by_sku.setdefault(unit["sku"], []).append(index)
        self.candidates: List[List[Mask]] = []
        for coupon in coupons:
            self.candidates.append(self._masks(coupon))

    def _masks(self, coupon: Mapping[str, Any]) -> List[Mask]:
        requires = coupon["requires"]
        skus = sorted(requires)
        option_lists = [list(itertools.combinations(self.by_sku[sku], requires[sku])) for sku in skus]
        result: List[Mask] = []
        for combo in itertools.product(*option_lists):
            indices = [i for group in combo for i in group]
            if len(set(indices)) != len(indices):
                continue
            if sum(self.prices[i] for i in indices) < coupon["D"]:
                continue
            mask = 0
            for i in indices:
                mask |= 1 << i
            result.append(mask)
        # Lexicographically smallest unit-id list first; basket is unit_id sorted
        # so ascending index order is ascending unit_id order.
        result.sort(key=lambda m: tuple(i for i in range(len(self.units)) if m >> i & 1))
        return result

    def max_discount(self) -> int:
        states: Dict[Mask, int] = {0: 0}
        for candidates, coupon in zip(self.candidates, self.coupons):
            nxt = dict(states)  # skipping the coupon
            for used, value in states.items():
                for mask in candidates:
                    if mask & used:
                        continue
                    merged = used | mask
                    candidate_value = value + coupon["D"]
                    if candidate_value > nxt.get(merged, -1):
                        nxt[merged] = candidate_value
            states = nxt
        return max(states.values(), default=0)

    def feasible(self, coupon_indices: Sequence[int], initial_used: Mask = 0) -> bool:
        # Most constrained coupon first; memo key includes position and used mask.
        order = sorted(coupon_indices, key=lambda i: len(self.candidates[i]))
        candidates = [self.candidates[i] for i in order]

        @lru_cache(maxsize=None)
        def search(position: int, used: Mask) -> bool:
            if position == len(candidates):
                return True
            for mask in candidates[position]:
                if mask & used:
                    continue
                if search(position + 1, used | mask):
                    return True
            return False

        return search(0, initial_used)

    def best_subset(self, target_discount: int) -> List[int]:
        """Lexicographically smallest used coupon-id list reaching the target."""

        combinations: List[Tuple[int, ...]] = []
        for size in range(len(self.coupons) + 1):
            combinations.extend(itertools.combinations(range(len(self.coupons)), size))
        combinations.sort(key=lambda idxs: tuple(self.coupons[i]["coupon_id"] for i in idxs))
        for idxs in combinations:
            if sum(self.coupons[i]["D"] for i in idxs) != target_discount:
                continue
            if self.feasible(idxs):
                return list(idxs)
        raise AssertionError("target discount is not achievable")  # pragma: no cover

    def best_units(self, coupon_indices: Sequence[int]) -> Dict[int, Mask]:
        used: Mask = 0
        chosen: Dict[int, Mask] = {}
        remaining = list(coupon_indices)
        for coupon_index in sorted(coupon_indices, key=lambda i: self.coupons[i]["coupon_id"]):
            remaining.remove(coupon_index)
            for mask in self.candidates[coupon_index]:
                if mask & used:
                    continue
                if self.feasible(remaining, used | mask):
                    chosen[coupon_index] = mask
                    used |= mask
                    break
            else:  # pragma: no cover - best_subset guaranteed feasibility
                raise AssertionError("no completion for coupon")
        return chosen


def solve(units: Sequence[Mapping[str, Any]], coupons: Sequence[Mapping[str, Any]]) -> Tuple[Dict[str, int], int]:
    """Return ``(coupon_id -> unit mask, total price)`` for the optimal quote."""

    solver = _Solver(units, coupons)
    target = solver.max_discount()
    subset = solver.best_subset(target)
    chosen = solver.best_units(subset)
    result = {coupons[i]["coupon_id"]: mask for i, mask in chosen.items()}
    total = sum(u["price"] for u in units) - target
    return dict(sorted(result.items())), total
