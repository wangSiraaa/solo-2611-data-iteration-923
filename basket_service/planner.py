"""JSON plan runner for the quote service and ledger.

A plan is ``{"steps": [ ... ]}``.  Each step is an object with an ``op`` and
operation-specific fields.  Steps may be named; later steps can reference an
earlier result with a ``"$name"`` placeholder (``"$name"`` alone returns the
whole result, ``"$name.field"`` walks dictionaries, ``"$name.0"`` indexes
lists).  Every executed step is recorded in the returned plan result so the
delivered JSON is a complete, replayable transcript.

Supported ops::

    publish_snapshot   products, coupons, snapshot_id?
    get_snapshot       snapshot_id
    quote              basket, coupons, request_id, snapshot_id?
    get_quote          request_id
    register_sale      sale_id, quote_request_id | quote, snapshot_id?
    get_sale           sale_id
    return             request_id, sale_id, unit_ids, grade
    get_return         request_id
    adjust_inventory   event_id, sku, quantity, bucket
    inventory          skus?
    audit_sales
    audit_inventory

Any step may set ``"expect_error": <code>`` (string or list) to assert that the
operation is rejected with that error code; the transcript then records a
passing step instead of aborting.  Without it, the first error aborts the plan
and is reported in the transcript.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence

from .errors import BasketError


class PlanError(Exception):
    def __init__(self, message: str, *, step_index: int, step: Any, transcript: List[Dict[str, Any]]):
        super().__init__(message)
        self.step_index = step_index
        self.step = step
        self.transcript = transcript


def _walk(value: Any, path: Sequence[str]) -> Any:
    current = value
    for part in path:
        if isinstance(current, Mapping):
            if part not in current:
                raise KeyError(part)
            current = current[part]
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            current = current[int(part)]
        else:
            raise KeyError(part)
    return current


def _resolve(value: Any, env: Mapping[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        body = value[1:]
        if body == "":
            raise PlanError("empty placeholder", step_index=-1, step=value, transcript=[])
        parts = body.split(".")
        name, rest = parts[0], parts[1:]
        if name not in env:
            raise PlanError(f"unknown step name {name!r}", step_index=-1, step=value, transcript=[])
        return _walk(env[name], rest) if rest else env[name]
    if isinstance(value, list):
        return [_resolve(item, env) for item in value]
    if isinstance(value, Mapping):
        return {key: _resolve(item, env) for key, item in value.items()}
    return value


def _require(step: Mapping[str, Any], key: str, op: str) -> Any:
    if key not in step:
        raise PlanError(f"{op} step missing {key!r}", step_index=-1, step=step, transcript=[])
    return step[key]


def _error_codes(expected: Any) -> set[str]:
    if isinstance(expected, str):
        return {expected}
    if isinstance(expected, Sequence):
        return {str(code) for code in expected}
    return {str(expected)}


def _execute(service, step: Mapping[str, Any], env: Mapping[str, Any]) -> Any:
    op = step.get("op")
    if op == "publish_snapshot":
        return service.publish_snapshot(
            step.get("products"),
            step.get("coupons"),
            snapshot_id=step.get("snapshot_id"),
        )
    if op == "get_snapshot":
        return service.get_snapshot(_require(step, "snapshot_id", op))
    if op == "quote":
        return service.quote(
            _require(step, "basket", op),
            _require(step, "coupons", op),
            _require(step, "request_id", op),
            step.get("snapshot_id"),
        )
    if op == "get_quote":
        return service.get_quote(_require(step, "request_id", op))
    if op == "register_sale":
        sale_id = _require(step, "sale_id", op)
        if "quote" in step:
            kwargs: Dict[str, Any] = {"quote": step["quote"]}
        elif "quote_request_id" in step:
            kwargs = {"request_id": step["quote_request_id"]}
        elif "quote_id" in step:
            kwargs = {"quote_id": step["quote_id"]}
        else:
            raise PlanError("register_sale needs quote or quote_request_id", step_index=-1, step=step, transcript=[])
        if "snapshot_id" in step:
            kwargs["snapshot_id"] = step["snapshot_id"]
        return service.register_sale(sale_id, **kwargs)
    if op == "get_sale":
        return service.get_sale(_require(step, "sale_id", op))
    if op == "return":
        return service.return_units(
            _require(step, "request_id", op),
            _require(step, "sale_id", op),
            _require(step, "unit_ids", op),
            _require(step, "grade", op),
        )
    if op == "get_return":
        return service.get_return(_require(step, "request_id", op))
    if op == "adjust_inventory":
        return service.adjust_inventory(
            _require(step, "event_id", op),
            _require(step, "sku", op),
            _require(step, "quantity", op),
            _require(step, "bucket", op),
        )
    if op == "inventory":
        return service.inventory(step.get("skus"))
    if op == "audit_sales":
        return service.audit_sales()
    if op == "audit_inventory":
        return service.audit_inventory()
    raise PlanError(f"unknown op {op!r}", step_index=-1, step=step, transcript=[])


def run_plan(service, plan: Any) -> Dict[str, Any]:
    if not isinstance(plan, Mapping) or not isinstance(plan.get("steps"), Sequence):
        raise PlanError("plan must be an object with a steps list", step_index=-1, step=plan, transcript=[])
    env: Dict[str, Any] = {}
    transcript: List[Dict[str, Any]] = []
    for index, raw_step in enumerate(plan["steps"]):
        if not isinstance(raw_step, Mapping):
            raise PlanError("each step must be an object", step_index=index, step=raw_step, transcript=transcript)
        name = raw_step.get("name")
        expect = raw_step.get("expect_error")
        try:
            step = _resolve(raw_step, env)
        except (PlanError, KeyError) as exc:
            raise PlanError(f"placeholder resolution failed: {exc}", step_index=index, step=raw_step, transcript=transcript)
        record: Dict[str, Any] = {"index": index, "op": raw_step.get("op")}
        if name is not None:
            record["name"] = name
        try:
            result = _execute(service, step, env)
            if expect is not None:
                raise PlanError(
                    f"expected error {expect!r} but step succeeded",
                    step_index=index,
                    step=raw_step,
                    transcript=transcript,
                )
            record.update({"ok": True, "result": result})
            if name is not None:
                env[name] = result
        except BasketError as exc:
            record.update({"ok": False, "error": exc.to_dict()["error"]})
            if expect is None:
                transcript.append(record)
                raise PlanError(
                    f"step failed with {exc.code}: {exc.message}",
                    step_index=index,
                    step=raw_step,
                    transcript=transcript,
                )
            if exc.code not in _error_codes(expect):
                transcript.append(record)
                raise PlanError(
                    f"expected {expect!r} but got {exc.code}",
                    step_index=index,
                    step=raw_step,
                    transcript=transcript,
                )
            record["expected_error_observed"] = exc.code
            if name is not None:
                env[name] = exc.to_dict()["error"]
        transcript.append(record)
    return {"ok": True, "steps": transcript}
