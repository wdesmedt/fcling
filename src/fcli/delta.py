"""What changed between two runs of a report on a node, record by record.

Records are matched on their :attr:`~fcli.report.Record.key`, and compared
without their :attr:`~fcli.report.Record.volatile` fields: a counter ticking
is an ``update`` - live news, not history - where a session going down is a
``change``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .report import Record


class Op(StrEnum):
    ADD = "add"
    REMOVE = "remove"
    CHANGE = "change"
    #: Only volatile fields moved: not history.
    UPDATE = "update"


@dataclass(frozen=True, slots=True)
class Change:
    ts: float
    node: str
    report: str
    key: str
    op: Op
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    #: The sequence number the store gave it; 0 until stored.
    seq: int = field(default=0, compare=False)

    @property
    def kept(self) -> bool:
        """Whether this is history, rather than live news only."""
        return self.op is not Op.UPDATE

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "node": self.node,
            "report": self.report,
            "key": self.key,
            "op": str(self.op),
            "before": self.before,
            "after": self.after,
        }


def record_key(record: Record) -> str:
    """The identity of *record* on its node, as text: what a change is filed under."""
    names = record.key or tuple(n for n in type(record).model_fields if n != "node")
    return json.dumps([getattr(record, n) for n in names], default=str, separators=(",", ":"))


def _drop(value: Any, path: list[str]) -> None:
    if isinstance(value, list):
        for item in value:
            _drop(item, path)
    elif isinstance(value, dict):
        if len(path) == 1:
            value.pop(path[0], None)
        elif path[0] in value:
            _drop(value[path[0]], path[1:])


def stable(record: Record) -> dict[str, Any]:
    """*record* as JSON, without its volatile fields."""
    data = record.model_dump(mode="json")
    for name in record.volatile:
        _drop(data, name.split("."))
    return data


def keyed(records: Iterable[Record]) -> dict[str, Record]:
    """Records by key. A key seen twice - two rows the device gives no way to
    tell apart - gets ``#2``, ``#3``... in the order they came."""
    out: dict[str, Record] = {}
    for record in records:
        key = base = record_key(record)
        n = 1
        while key in out:
            n += 1
            key = f"{base}#{n}"
        out[key] = record
    return out


def diff(
    node: str,
    report: str,
    before: Mapping[str, Record],
    after: Mapping[str, Record],
    ts: float,
) -> list[Change]:
    changes = []
    for key, old in before.items():
        if key not in after:
            changes.append(Change(ts, node, report, key, Op.REMOVE, before=old.model_dump(mode="json")))
    for key, new in after.items():
        old_record = before.get(key)
        if old_record is None:
            changes.append(Change(ts, node, report, key, Op.ADD, after=new.model_dump(mode="json")))
        elif old_record != new:
            op = Op.CHANGE if stable(old_record) != stable(new) else Op.UPDATE
            changes.append(
                Change(
                    ts,
                    node,
                    report,
                    key,
                    op,
                    before=old_record.model_dump(mode="json"),
                    after=new.model_dump(mode="json"),
                )
            )
    return changes
