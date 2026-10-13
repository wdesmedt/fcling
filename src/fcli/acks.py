"""Acknowledged findings: what someone has seen, and does not need shown again.

A known incident - a link waiting for a technician, BFD that the next change
window enables - keeps drawing the eye on every page until it is fixed.
Acknowledging it takes it out of the counts that say *look here*, without
hiding that it is still there.

An acknowledgement is kept per *finding*, not per incident, so it stays honest
as the fabric changes underneath it:

* an incident is acknowledged while every finding it holds is: a new finding
  joining it - a second session going down over the same link - brings it
  back, because nobody has seen that yet;
* an acknowledgement ends when its finding clears, so the same fault coming
  back later is an alarm again, not silently pre-acknowledged.

Acknowledgements live in the server's memory, shared by everyone looking at
it; with ``fcling serve --persist-acks`` they are also written to a JSON file
next to the history, so a restart keeps them.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: What identifies a finding across readings: its check, node and subject.
Key = tuple[str, str, str]


def finding_key(finding: Any) -> Key:
    """The key of a :class:`~fcli.checks.Finding`, or of its JSON form."""
    get = finding.get if isinstance(finding, dict) else lambda name: getattr(finding, name)
    return (str(get("check")), str(get("node")), str(get("subject")))


def path_for(db: Path) -> Path:
    """Where the acknowledgements of the fabric whose history is *db* are kept."""
    return db.with_name(f"{db.stem}.acks.json")


@dataclass(frozen=True)
class Ack:
    """One acknowledged finding."""

    check: str
    node: str
    subject: str
    #: When, as a Unix timestamp.
    at: float
    #: What the person acknowledging it wrote, if anything.
    note: str = ""
    #: The incident it was acknowledged as part of, and its title then.
    incident_id: str = ""
    incident: str = ""

    @property
    def key(self) -> Key:
        return (self.check, self.node, self.subject)


class AckStore:
    """The acknowledged findings of one fabric; kept in *path* when given."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._acks: dict[Key, Ack] = {}
        if path is not None:
            self._load(path)

    def acknowledge(
        self, findings: Iterable[Any], note: str = "", incident_id: str = "", incident: str = ""
    ) -> list[Ack]:
        """Acknowledge *findings*; returns the acknowledgements made."""
        at = time.time()
        made = [Ack(*finding_key(f), at, note.strip(), incident_id, incident) for f in findings]
        with self._lock:
            for ack in made:
                self._acks[ack.key] = ack
        self._save()
        return made

    def unacknowledge(self, keys: Iterable[Key]) -> list[Ack]:
        """Take the acknowledgement off *keys*; returns the ones there were."""
        with self._lock:
            dropped = [self._acks.pop(k) for k in map(tuple, keys) if k in self._acks]
        if dropped:
            self._save()
        return dropped  # type: ignore[return-value]

    def of_incident(self, incident_id: str) -> set[Key]:
        """The findings acknowledged as part of incident *incident_id*."""
        with self._lock:
            return {k for k, ack in self._acks.items() if ack.incident_id == incident_id}

    def keys(self) -> set[Key]:
        with self._lock:
            return set(self._acks)

    def get(self, key: Key) -> Ack | None:
        with self._lock:
            return self._acks.get(key)

    def all(self) -> list[Ack]:
        with self._lock:
            return sorted(self._acks.values(), key=lambda a: (-a.at, a.node, a.check, a.subject))

    # ------------------------------------------------------------- persistence

    def _load(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            logger.warning("acknowledgements in %s not read: %s", path, exc)
            return
        for item in raw if isinstance(raw, list) else ():
            try:
                ack = Ack(
                    str(item["check"]),
                    str(item["node"]),
                    str(item["subject"]),
                    float(item["at"]),
                    str(item.get("note", "")),
                    str(item.get("incident_id", "")),
                    str(item.get("incident", "")),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._acks[ack.key] = ack

    def _save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            payload = [asdict(a) for a in self._acks.values()]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Written aside and moved over: a crash mid-write never leaves half a file.
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("acknowledgements not kept in %s: %s", self.path, exc)
