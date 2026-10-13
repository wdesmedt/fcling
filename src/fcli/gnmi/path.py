"""gNMI paths as values: parsed once, compared, hashed and turned into protobuf.

A path is a tuple of :class:`Elem`. The string form is the one SR Linux and
gnmic use - ``/interface[name=ethernet-1/1]/subinterface[index=0]`` - where a
key value may itself hold slashes, and ``]`` or ``\\`` inside one is escaped
with a backslash.

YANG module prefixes (``srl_nokia-interfaces:interface``) are dropped on the
way in: SR Linux puts them on some elements and not others, and a path that is
spelled two ways cannot be looked up.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from .proto import gnmi_pb2

#: A key value that matches every entry of a list.
WILDCARD = "*"


@dataclass(frozen=True, slots=True)
class Elem:
    """One element of a path: a name and, on a list, the keys it selects."""

    name: str
    keys: tuple[tuple[str, str], ...] = ()

    @property
    def key_dict(self) -> dict[str, str]:
        return dict(self.keys)

    def __str__(self) -> str:
        return self.name + "".join(f"[{k}={_escape(v)}]" for k, v in self.keys)


Path = tuple[Elem, ...]


class PathError(ValueError):
    """A string that is not a gNMI path."""


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("]", "\\]")


def strip_prefix(name: str) -> str:
    """``srl_nokia-interfaces:interface`` -> ``interface``."""
    return name.rpartition(":")[2]


def parse(text: str | Path) -> Path:
    """The :data:`Path` *text* spells; a path passes through as it is."""
    if isinstance(text, tuple):
        return text
    elems: list[Elem] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "/":
            i += 1
            continue
        start = i
        while i < n and text[i] not in "/[":
            i += 1
        name = strip_prefix(text[start:i])
        if not name:
            raise PathError(f"empty element in {text!r}")
        keys: list[tuple[str, str]] = []
        while i < n and text[i] == "[":
            i += 1
            eq = text.find("=", i)
            if eq < 0:
                raise PathError(f"key without a value in {text!r}")
            key = strip_prefix(text[i:eq])
            i = eq + 1
            value: list[str] = []
            while i < n and text[i] != "]":
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                value.append(text[i])
                i += 1
            if i >= n:
                raise PathError(f"unterminated key in {text!r}")
            i += 1  # the ']'
            keys.append((key, "".join(value)))
        elems.append(Elem(name, tuple(keys)))
    return tuple(elems)


def format(path: Path) -> str:  # noqa: A001 - mirrors parse()
    return "/" + "/".join(str(e) for e in path)


def schema(path: Path) -> str:
    """The path without its keys: where in the model it points, not at what."""
    return "/".join(e.name for e in path)


def to_proto(path: str | Path, origin: str = "") -> gnmi_pb2.Path:
    return gnmi_pb2.Path(
        origin=origin,
        elem=[gnmi_pb2.PathElem(name=e.name, key=dict(e.keys)) for e in parse(path)],
    )


def from_proto(path: gnmi_pb2.Path) -> Path:
    if path.elem:
        return tuple(
            Elem(strip_prefix(e.name), tuple(sorted((strip_prefix(k), v) for k, v in e.key.items())))
            for e in path.elem
        )
    # Pre-0.4 targets spell the path as plain strings.
    return parse("/".join(path.element)) if path.element else ()


def matches(pattern: Path, path: Path) -> bool:
    """Whether *path* is *pattern* or under it, with ``*`` matching any key value.

    A pattern element without keys matches the element whatever its keys, so
    ``/interface/statistics`` covers ``/interface[name=mgmt0]/statistics``.
    """
    if len(path) < len(pattern):
        return False
    for want, got in zip(pattern, path, strict=False):
        if want.name != got.name:
            return False
        have = got.key_dict
        for key, value in want.keys:
            if value != WILDCARD and have.get(key, value) != value:
                return False
    return True


def _key_overlaps(a: str, b: str) -> bool:
    if a == b or WILDCARD in (a, b):
        return True
    return fnmatch.fnmatchcase(b, a) or fnmatch.fnmatchcase(a, b)


def overlaps(a: Path, b: Path) -> bool:
    """Whether *a* and *b* can name some of the same data: one is at, under or
    above the other, with keys that can select the same entries.

    ``/interface[name=irb*]/subinterface`` overlaps an update at
    ``/interface[name=irb0]/subinterface[index=1]/oper-state``, and one at
    ``/`` holding everything.
    """
    for x, y in zip(a, b, strict=False):
        if x.name != y.name:
            return False
        ykeys = y.key_dict
        for key, value in x.keys:
            if key in ykeys and not _key_overlaps(value, ykeys[key]):
                return False
    return True
