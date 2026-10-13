"""A node's running configuration, as something to keep and compare.

The configuration is a gNMI Get of ``/`` with datatype ``config``: one JSON
tree. Before it is kept or shown:

* :func:`normalize` takes out what is not configuration - module prefixes on
  names and identity values, annotations - and replaces every secret with a
  digest of it (:func:`redact`). A password that changed still reads as
  changed; what it was or became is never kept.
* :func:`flatten` renders the tree as the ``set / ...`` lines SR Linux prints
  with ``info flat``, one per leaf: what an engineer reads a change in.
* :func:`side_by_side` compares two renderings as a split diff: rows of an old
  and a new line, each kept, removed, added or changed.

JSON carries a YANG list as an array of entries without saying which leaves
are its keys. They are worked out from the entries (:func:`list_keys`): the
key names SR Linux uses, most common first, extended until they tell the
entries apart. Both sides of a comparison are keyed together, so a list reads
the same on both.

Ported from the original fcli's ``nornir_srl/configs.py``.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: The leaves whose value is a secret: a password or its hash, a key, a
#: pre-shared or authentication key, an SNMP community. Matched on the leaf's
#: own name, so ``ssh-key`` (a public key) and ``keychain`` are not.
SECRET_LEAF = re.compile(
    r"^(?:password|hashed-password|secret|key|private-key|pre-shared-key|psk|"
    r"authentication-key|auth-key|auth-password|priv-password|privacy-password|"
    r"shared-secret|community|token|client-secret|md5-key)$"
    r"|-password$|-secret$|-psk$",
    re.IGNORECASE,
)

#: What a redacted value reads as: the digest tells a changed secret from an
#: unchanged one, and nothing else.
REDACTED = "<redacted {}>"

_ANNOTATION = re.compile(r"^_annotate")
#: A module prefix on a name: a YANG identifier cannot hold a colon.
_NAME_MODULE = re.compile(r"^[A-Za-z_][\w.-]*:(?=[A-Za-z_])")
#: A module prefix on an identity value. A value can be any string - ``site:west``,
#: a MAC - so only the modules SR Linux answers with are taken for one.
_VALUE_MODULE = re.compile(r"^(?:srl_nokia|openconfig|ietf|iana)-[\w.-]*:(?=[A-Za-z_])")
_PREDICATE = re.compile(r"\[[^\]]*\]")

#: The names SR Linux keys its lists by, most common first.
KEY_NAMES: tuple[str, ...] = (
    "name",
    "id",
    "index",
    "sequence-id",
    "interface-name",
    "peer-address",
    "group-name",
    "ip-prefix",
    "ipv4-prefix",
    "ipv6-prefix",
    "prefix",
    "address",
    "area-id",
    "level-number",
    "system-id",
    "router-id",
    "vlan-id",
    "esi",
    "vni",
    "target",
    "type",
    "afi-safi-name",
    "key-id",
    "entry-id",
    "label",
)

#: Lists known to be keyed by more than one leaf, by the list's own name.
KNOWN_KEYS: Mapping[str, tuple[str, ...]] = {"acl-filter": ("name", "type")}


def strip_module(name: str) -> str:
    return _NAME_MODULE.sub("", name)


def _digest(value: Any, salt: str) -> str:
    raw = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256((salt + raw).encode("utf-8")).hexdigest()[:8]


def leaf_name(key: Any) -> str:
    """The leaf a key or path names: ``srl_nokia-aaa:user[name=x]/password`` is ``password``."""
    parts = [p for p in _PREDICATE.sub("", str(key)).split("/") if p]
    return strip_module(parts[-1]) if parts else ""


def redact(tree: Any, salt: str = "", name: str = "") -> Any:
    """*tree* with every secret leaf's value replaced by a salted digest of it."""
    if isinstance(tree, dict):
        return {key: redact(value, salt, leaf_name(key) or name) for key, value in tree.items()}
    if isinstance(tree, list):
        # A leaf-list of communities is BGP's, not SNMP's.
        if (
            name
            and SECRET_LEAF.search(name)
            and name.lower() != "community"
            and all(not isinstance(v, dict | list) for v in tree)
        ):
            return [REDACTED.format(_digest(v, salt)) for v in tree]
        return [redact(value, salt, name if _unnamed(value) else "") for value in tree]
    if name and SECRET_LEAF.search(name) and tree is not None:
        return REDACTED.format(_digest(tree, salt))
    if isinstance(tree, str) and "\n" in tree:
        return redact_text(tree, salt)
    return tree


def _unnamed(value: Any) -> bool:
    return isinstance(value, dict) and bool(value) and all(not leaf_name(k) for k in value)


#: A secret leaf as CLI text prints it, value last on its line.
_SECRET_TEXT = re.compile(
    r"(?<![\w-])("
    r"(?:password|hashed-password|secret|key|private-key|pre-shared-key|psk|"
    r"authentication-key|auth-key|auth-password|priv-password|privacy-password|"
    r"shared-secret|community|token|client-secret|md5-key|"
    r"[\w-]+-password|[\w-]+-secret|[\w-]+-psk)"
    r"[ \t]+)"
    r"(\"(?:[^\"\\\n]|\\.)*\"|[^\s\"]+)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def redact_text(text: str, salt: str = "") -> str:
    """CLI text - a banner, a script - with every secret leaf's value replaced by a digest."""
    return _SECRET_TEXT.sub(
        lambda m: m.group(1) + REDACTED.format(_digest(m.group(2).strip('"'), salt)), text
    )


def _clean(tree: Any) -> Any:
    """*tree* without module prefixes or annotations; path-shaped keys nested."""
    if isinstance(tree, dict):
        cleaned: dict[str, Any] = {}
        for key, value in tree.items():
            if _ANNOTATION.match(str(key)):
                continue
            parts = [strip_module(p) for p in strip_module(str(key)).split("/") if p]
            target = cleaned
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            leaf = parts[-1] if parts else str(key)
            value = _clean(value)
            if isinstance(target.get(leaf), dict) and isinstance(value, dict):
                target[leaf].update(value)
            else:
                target[leaf] = value
        return cleaned
    if isinstance(tree, list):
        return [_clean(value) for value in tree]
    if isinstance(tree, str):
        return _VALUE_MODULE.sub("", tree)
    return tree


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    for key, value in extra.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def normalize(payloads: Iterable[tuple[str, Any]], salt: str = "") -> dict[str, Any]:
    """The configuration tree a Get of ``/`` answered, as ``(path, value)`` updates, clean and redacted.

    Secrets are redacted first, from the values exactly as the node sent
    them: cleaning before digesting could make two secrets read as one.
    """
    tree: dict[str, Any] = {}
    for path, value in payloads:
        value = redact(value, salt, leaf_name(path))
        if path in ("/", "") and isinstance(value, dict):
            tree = _merge(tree, _clean(value))
        else:
            tree = _merge(tree, _clean({path: value}))
    return tree


def canonical(tree: Any) -> str:
    return json.dumps(tree, sort_keys=True, separators=(",", ":"), default=str)


def digest(tree: Any) -> str:
    """What tells two configurations apart without comparing them."""
    return hashlib.sha256(canonical(tree).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# set-style lines
# --------------------------------------------------------------------------- #


def _scalar(value: Any) -> bool:
    return not isinstance(value, dict | list)


def _is_list(value: Any) -> bool:
    """A YANG list - entries - as opposed to a leaf-list of values."""
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


@dataclass
class _ListShape:
    occurrences: list[list[dict[str, Any]]] = field(default_factory=list)


def _collect(tree: Any, path: tuple[str, ...], shapes: dict[tuple[str, ...], _ListShape]) -> None:
    if isinstance(tree, dict):
        for key, value in tree.items():
            if _is_list(value):
                signature = (*path, key)
                shapes.setdefault(signature, _ListShape()).occurrences.append(value)
                for entry in value:
                    _collect(entry, signature, shapes)
            elif isinstance(value, dict):
                _collect(value, (*path, key), shapes)


def _unique(occurrences: Sequence[Sequence[dict[str, Any]]], keys: Sequence[str]) -> bool:
    for entries in occurrences:
        seen = set()
        for entry in entries:
            value = tuple(str(entry.get(k)) for k in keys)
            if value in seen:
                return False
            seen.add(value)
    return True


def list_keys(trees: Iterable[Any]) -> dict[tuple[str, ...], tuple[str, ...]]:
    """The leaves that key each list in *trees*, by the list's path without keys."""
    shapes: dict[tuple[str, ...], _ListShape] = {}
    for tree in trees:
        _collect(tree, (), shapes)
    keys: dict[tuple[str, ...], tuple[str, ...]] = {}
    for signature, shape in shapes.items():
        entries = [entry for occurrence in shape.occurrences for entry in occurrence]
        common = [
            name
            for name in dict.fromkeys(name for entry in entries for name in entry)
            if all(name in entry and _scalar(entry[name]) for entry in entries)
        ]
        known = KNOWN_KEYS.get(signature[-1])
        if known and all(k in common for k in known):
            keys[signature] = known
            continue
        ordered = [k for k in KEY_NAMES if k in common] + sorted(k for k in common if k not in KEY_NAMES)
        chosen: list[str] = []
        for name in ordered:
            chosen.append(name)
            if _unique(shape.occurrences, chosen):
                break
        keys[signature] = tuple(chosen)
    return keys


_BARE = re.compile(r"^[^\s\"';{}\[\]]+$")


def _token(value: Any) -> str:
    """A value as SR Linux prints it in a flat line: quoted when it has to be."""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if text and _BARE.match(text):
        return text
    # A banner is one leaf: one line, its line breaks escaped.
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return '"' + escaped + '"'


def _natural(text: str) -> tuple[Any, ...]:
    """``ethernet-1/10`` after ``ethernet-1/2``."""
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"(\d+)", str(text)))


#: A flat line and its leaf: the line without its value, what its old and new versions share.
Line = tuple[str, str]


def lines(tree: Any, keys: Mapping[tuple[str, ...], tuple[str, ...]] | None = None) -> list[Line]:
    """*tree* as ``set / ...`` lines with their leaves, one per leaf, in the order SR Linux prints them."""
    return [
        ("set / " + " ".join(head if value is None else (*head, value)), "set / " + " ".join(head))
        for head, value in _lines(tree, (), (), list_keys([tree]) if keys is None else keys)
    ]


def flatten(tree: Any, keys: Mapping[tuple[str, ...], tuple[str, ...]] | None = None) -> list[str]:
    """*tree* as ``set / ...`` lines, one per leaf."""
    return [line for line, _ in lines(tree, keys)]


def _lines(
    tree: Any,
    words: tuple[str, ...],
    signature: tuple[str, ...],
    keys: Mapping[tuple[str, ...], tuple[str, ...]],
) -> Iterator[tuple[tuple[str, ...], str | None]]:
    """``(words, value)`` per leaf; no value for a list entry or a container with nothing else in it."""
    if not isinstance(tree, dict):
        return
    if not tree and words:
        # A presence container, or one configured with nothing in it.
        yield words, None
        return
    for key in sorted(tree, key=_natural):
        value = tree[key]
        if _is_list(value):
            list_signature = (*signature, key)
            key_names = keys.get(list_signature) or ()
            for entry in sorted(value, key=lambda e: tuple(_natural(str(e.get(k, ""))) for k in key_names)):
                head: list[str] = [key]
                for n, name in enumerate(key_names):
                    if n:
                        head.append(name)
                    head.append(_token(entry.get(name, "")))
                rest = {k: v for k, v in entry.items() if k not in key_names}
                if not rest:
                    yield (*words, *head), None
                    continue
                yield from _lines(rest, (*words, *head), list_signature, keys)
        elif isinstance(value, dict):
            yield from _lines(value, (*words, key), (*signature, key), keys)
        elif isinstance(value, list):
            # A leaf-list, printed whole: ``server-list [ 10.0.0.1 10.0.0.2 ]``.
            yield (*words, key), " ".join(("[", *(_token(v) for v in value), "]"))
        else:
            yield (*words, key), _token(value)


# --------------------------------------------------------------------------- #
# comparing
# --------------------------------------------------------------------------- #

SAME = "same"
ADDED = "added"
REMOVED = "removed"
CHANGED = "changed"
#: Unchanged lines left out between two hunks.
SKIP = "skip"


def _side(lines: Sequence[Line], i: int) -> dict[str, Any]:
    return {"n": i + 1, "text": lines[i][0]}


@dataclass(frozen=True)
class ConfigDiff:
    """Two configurations of a node compared as a split diff."""

    #: ``{"kind", "left": {"n", "text"} | None, "right": ... | None}``, or
    #: ``{"kind": "skip", "count": n}`` for unchanged lines left out.
    rows: tuple[dict[str, Any], ...]
    lines_before: int
    lines_after: int

    def count(self, kind: str) -> int:
        return sum(1 for r in self.rows if r["kind"] == kind)

    @property
    def summary(self) -> str:
        if not any(r["kind"] in (ADDED, REMOVED, CHANGED) for r in self.rows):
            return "no change"
        return f"+{self.count(ADDED)} -{self.count(REMOVED)} ~{self.count(CHANGED)}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "added": self.count(ADDED),
            "removed": self.count(REMOVED),
            "changed": self.count(CHANGED),
            "summary": self.summary,
            "lines_before": self.lines_before,
            "lines_after": self.lines_after,
            "rows": list(self.rows),
        }


def _replaced(old: Sequence[Line], new: Sequence[Line], i0: int, j0: int) -> Iterator[dict[str, Any]]:
    """Lines that differ: a leaf whose value changed is one row, its old and new line side by side."""
    paths = difflib.SequenceMatcher(a=[p for _, p in old], b=[p for _, p in new], autojunk=False)
    for tag, i1, i2, j1, j2 in paths.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                yield {
                    "kind": CHANGED,
                    "left": {"n": i0 + i1 + k + 1, "text": old[i1 + k][0]},
                    "right": {"n": j0 + j1 + k + 1, "text": new[j1 + k][0]},
                }
            continue
        for i in range(i1, i2):
            yield {"kind": REMOVED, "left": {"n": i0 + i + 1, "text": old[i][0]}, "right": None}
        for j in range(j1, j2):
            yield {"kind": ADDED, "left": None, "right": {"n": j0 + j + 1, "text": new[j][0]}}


def side_by_side(before: Sequence[Line], after: Sequence[Line], context: int = 3) -> ConfigDiff:
    """*after* against *before*, line by line, with *context* unchanged lines around each change."""
    rows: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(a=[x for x, _ in before], b=[x for x, _ in after], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            rows += [
                {"kind": SAME, "left": _side(before, i1 + k), "right": _side(after, j1 + k)}
                for k in range(i2 - i1)
            ]
        elif tag == "delete":
            rows += [{"kind": REMOVED, "left": _side(before, i), "right": None} for i in range(i1, i2)]
        elif tag == "insert":
            rows += [{"kind": ADDED, "left": None, "right": _side(after, j)} for j in range(j1, j2)]
        else:
            rows += _replaced(before[i1:i2], after[j1:j2], i1, j1)
    return ConfigDiff(tuple(_hunks(rows, context)), len(before), len(after))


def _hunks(rows: list[dict[str, Any]], context: int) -> Iterator[dict[str, Any]]:
    """*rows* with each run of unchanged lines further than *context* from a change left out."""
    near = [False] * len(rows)
    for i, row in enumerate(rows):
        if row["kind"] != SAME:
            for k in range(max(0, i - context), min(len(rows), i + context + 1)):
                near[k] = True
    skipped = 0
    for row, keep in zip(rows, near, strict=True):
        if keep:
            if skipped:
                yield {"kind": SKIP, "count": skipped}
                skipped = 0
            yield row
        else:
            skipped += 1
    if skipped:
        yield {"kind": SKIP, "count": skipped}


def compare(before: Any | None, after: Any, context: int = 3) -> ConfigDiff:
    """Two configuration trees, keyed together so a list reads the same on both sides."""
    keys = list_keys([t for t in (before, after) if t is not None])
    return side_by_side(lines(before, keys) if before is not None else [], lines(after, keys), context)
