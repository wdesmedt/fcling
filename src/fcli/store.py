"""History in SQLite: an append-only log of record changes, and the state they fold to.

``changes`` is the history: every record that appeared, went or changed,
with what it was before and after. ``current`` is where those changes have
got to, kept alongside so that a restarted engine diffs against the last
state it knew - not against nothing, which would log the whole fabric as new.

The state at any earlier moment is ``current`` with the changes since then
undone, newest first: ``what was wrong at 14:02`` is a fold, not a snapshot.

A store is used from one thread at a time; :class:`AsyncStore` runs it on a
single worker thread of its own so the engine's loop never waits on disk.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
import time
import zlib
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .delta import Change, Op


class BeforeHistory(ValueError):
    """An instant earlier than anything the history kept: no answer, rather than an empty one."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    node TEXT NOT NULL,
    report TEXT NOT NULL,
    key TEXT NOT NULL,
    op TEXT NOT NULL,
    before TEXT,
    after TEXT
);
CREATE INDEX IF NOT EXISTS changes_ts ON changes (ts);
CREATE INDEX IF NOT EXISTS changes_report ON changes (report, node, ts);
CREATE TABLE IF NOT EXISTS baselines (
    name TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    created REAL NOT NULL,
    comment TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS configs (
    node TEXT NOT NULL,
    ts REAL NOT NULL,
    commit_id INTEGER,
    digest TEXT NOT NULL,
    lines INTEGER NOT NULL,
    tree BLOB NOT NULL,
    PRIMARY KEY (node, ts)
);
CREATE TABLE IF NOT EXISTS meta (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS current (
    node TEXT NOT NULL,
    report TEXT NOT NULL,
    key TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (node, report, key)
);
"""


def default_path(fabric: str) -> Path:
    """``$XDG_STATE_HOME/fcli/<fabric>.db``, ``~/.local/state`` by default."""
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "fcli" / f"{fabric}.db"


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, separators=(",", ":"))


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "seq": row["seq"],
        "ts": row["ts"],
        "node": row["node"],
        "report": row["report"],
        "key": row["key"],
        "op": row["op"],
        "before": json.loads(row["before"]) if row["before"] else None,
        "after": json.loads(row["after"]) if row["after"] else None,
    }


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ----------------------------------------------------------------- write

    def write(self, changes: Iterable[Change]) -> list[int]:
        """Log *changes* - the kept ones - and fold them into ``current``; their sequence numbers."""
        seqs = []
        with self.db:
            for c in changes:
                if not c.kept:
                    continue
                cur = self.db.execute(
                    "INSERT INTO changes (ts, node, report, key, op, before, after) VALUES (?,?,?,?,?,?,?)",
                    (c.ts, c.node, c.report, c.key, str(c.op), _json(c.before), _json(c.after)),
                )
                seqs.append(cur.lastrowid or 0)
                if c.op is Op.REMOVE:
                    self.db.execute(
                        "DELETE FROM current WHERE node=? AND report=? AND key=?", (c.node, c.report, c.key)
                    )
                else:
                    self.db.execute(
                        "INSERT OR REPLACE INTO current (node, report, key, record) VALUES (?,?,?,?)",
                        (c.node, c.report, c.key, _json(c.after)),
                    )
        return seqs

    # ------------------------------------------------------------------ read

    def current(self) -> dict[tuple[str, str], dict[str, dict[str, Any]]]:
        """``{(node, report): {key: record}}``: where the history has got to."""
        out: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        for row in self.db.execute("SELECT node, report, key, record FROM current"):
            out.setdefault((row["node"], row["report"]), {})[row["key"]] = json.loads(row["record"])
        return out

    def history(
        self,
        since: float | None = None,
        until: float | None = None,
        nodes: Sequence[str] = (),
        reports: Sequence[str] = (),
        after_seq: int = 0,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Changes, oldest first."""
        where, args = self._where(since, until, nodes, reports)
        if after_seq:
            where.append("seq > ?")
            args.append(after_seq)
        sql = "SELECT * FROM changes"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq DESC LIMIT ?"
        rows = self.db.execute(sql, [*args, limit]).fetchall()
        return [_row(r) for r in reversed(rows)]

    def state_at(
        self, ts: float, report: str, nodes: Sequence[str] = ()
    ) -> dict[str, dict[str, dict[str, Any]]]:
        """``{node: {key: record}}`` of *report* as it was at *ts*: ``current`` with
        every later change undone, newest first."""
        first = self.first()
        if first is None or ts < first:
            began = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(first)) if first else ""
            asked = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            raise BeforeHistory(
                f"{asked} is before the history, which starts at {began}" if began else "the history is empty"
            )
        state: dict[str, dict[str, dict[str, Any]]] = {}
        node_filter = f" AND node IN ({','.join('?' * len(nodes))})" if nodes else ""
        for row in self.db.execute(
            f"SELECT node, key, record FROM current WHERE report=?{node_filter}", [report, *nodes]
        ):
            state.setdefault(row["node"], {})[row["key"]] = json.loads(row["record"])
        later = self.db.execute(
            f"SELECT * FROM changes WHERE report=? AND ts>?{node_filter} ORDER BY seq DESC",
            [report, ts, *nodes],
        )
        for row in later:
            records = state.setdefault(row["node"], {})
            if row["op"] == Op.ADD:
                records.pop(row["key"], None)
            else:
                records[row["key"]] = json.loads(row["before"])
        return state

    def first(self) -> float | None:
        """When the history starts: its first change."""
        row = self.db.execute("SELECT min(ts) FROM changes").fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def raised(self, report: str) -> dict[tuple[str, str], float]:
        """``{(node, key): when}`` each current record of *report* last appeared:
        for findings, since when each has been wrong."""
        rows = self.db.execute(
            """SELECT c.node, c.key, MAX(ch.ts) AS ts FROM current c
               JOIN changes ch
                 ON ch.node = c.node AND ch.report = c.report AND ch.key = c.key AND ch.op = 'add'
               WHERE c.report = ? GROUP BY c.node, c.key""",
            (report,),
        )
        return {(r["node"], r["key"]): r["ts"] for r in rows}

    # ---------------------------------------------------------- configurations

    def salt(self) -> str:
        """This history's own salt for secret digests: they say nothing about another's."""
        row = self.db.execute("SELECT value FROM meta WHERE name = 'salt'").fetchone()
        if row:
            return str(row[0])
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO meta (name, value) VALUES ('salt', ?)", (secrets.token_hex(16),)
            )
        return self.salt()

    def keep_config(
        self, node: str, ts: float, commit_id: int | None, tree: Any, digest: str, lines: int
    ) -> bool:
        """Keep *node*'s configuration as of *ts*, unless it is the one kept last; whether it was kept."""
        last = self.db.execute(
            "SELECT digest FROM configs WHERE node = ? ORDER BY ts DESC LIMIT 1", (node,)
        ).fetchone()
        if last and last[0] == digest:
            return False
        blob = zlib.compress(json.dumps(tree, separators=(",", ":")).encode())
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO configs (node, ts, commit_id, digest, lines, tree)"
                " VALUES (?,?,?,?,?,?)",
                (node, ts, commit_id, digest, lines, blob),
            )
        return True

    def configs(self, nodes: Sequence[str] = ()) -> list[dict[str, Any]]:
        """The configurations kept, newest first, without their trees."""
        where = f" WHERE node IN ({','.join('?' * len(nodes))})" if nodes else ""
        rows = self.db.execute(
            f"SELECT node, ts, commit_id, digest, lines FROM configs{where} ORDER BY ts DESC", list(nodes)
        )
        return [dict(r) for r in rows]

    def config(
        self, node: str, commit_id: int | None = None, before: float | None = None
    ) -> dict[str, Any] | None:
        """A kept configuration of *node*, with its tree: the latest; the first kept at or after
        *commit_id*; or the last kept before *before*."""
        args: list[Any] = []
        if commit_id is not None:
            sql, args = "commit_id >= ? ORDER BY ts ASC", [commit_id]
        elif before is not None:
            sql, args = "ts < ? ORDER BY ts DESC", [before]
        else:
            sql = "1 ORDER BY ts DESC"
        row = self.db.execute(
            f"SELECT * FROM configs WHERE node = ? AND {sql} LIMIT 1", [node, *args]
        ).fetchone()
        if row is None:
            return None
        out = {k: row[k] for k in ("node", "ts", "commit_id", "digest", "lines")}
        out["tree"] = json.loads(zlib.decompress(row["tree"]))
        return out

    # ------------------------------------------------------------- baselines

    def set_baseline(self, name: str, ts: float, comment: str = "") -> None:
        """Name the fabric as it was at *ts*: a pointer into the history, not a copy."""
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO baselines (name, ts, created, comment) VALUES (?,?,?,?)",
                (name, ts, time.time(), comment),
            )

    def baselines(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM baselines ORDER BY ts")]

    def baseline(self, name: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM baselines WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def latest_baseline(self) -> dict[str, Any] | None:
        """The baseline at the latest instant: the one "since the baseline" means."""
        row = self.db.execute("SELECT * FROM baselines ORDER BY ts DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def delete_baseline(self, name: str) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM baselines WHERE name = ?", (name,)).rowcount > 0

    @staticmethod
    def _where(
        since: float | None, until: float | None, nodes: Sequence[str], reports: Sequence[str]
    ) -> tuple[list[str], list[Any]]:
        where: list[str] = []
        args: list[Any] = []
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        if until is not None:
            where.append("ts <= ?")
            args.append(until)
        if nodes:
            where.append(f"node IN ({','.join('?' * len(nodes))})")
            args.extend(nodes)
        if reports:
            where.append(f"report IN ({','.join('?' * len(reports))})")
            args.extend(reports)
        return where, args


class AsyncStore:
    """A :class:`Store` on a worker thread of its own: calls are serialised and off the loop."""

    def __init__(self, path: str | Path) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fcli-store")
        self.store = self._executor.submit(Store, path).result()

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(*args, **kwargs))

    async def write(self, changes: list[Change]) -> list[int]:
        return await self._call(self.store.write, changes)  # type: ignore[no-any-return]

    async def current(self) -> dict[tuple[str, str], dict[str, dict[str, Any]]]:
        return await self._call(self.store.current)  # type: ignore[no-any-return]

    async def history(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await self._call(self.store.history, **kwargs)  # type: ignore[no-any-return]

    async def state_at(self, ts: float, report: str, nodes: Sequence[str] = ()) -> dict[str, Any]:
        return await self._call(self.store.state_at, ts, report, nodes)  # type: ignore[no-any-return]

    async def raised(self, report: str) -> dict[tuple[str, str], float]:
        return await self._call(self.store.raised, report)  # type: ignore[no-any-return]

    async def run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """``fn(store, *args, **kwargs)`` on the store's thread."""
        return await self._call(fn, self.store, *args, **kwargs)

    def close(self) -> None:
        self._executor.submit(self.store.close).result()
        self._executor.shutdown()


def parse_time(text: str, now: float | None = None) -> float:
    """An instant from ``1h``, ``30m``, ``2d`` (that long ago), epoch seconds, ISO 8601,
    or a time of day - ``14:02`` - meaning the last time it was that."""
    from datetime import datetime, timedelta
    from datetime import time as clock

    now = time.time() if now is None else now
    text = text.strip()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if text and text[-1] in units and text[:-1].replace(".", "", 1).isdigit():
        return now - float(text[:-1]) * units[text[-1]]
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        pass
    try:
        of_day = clock.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{text!r} is not a time: try 1h, 30m, 2d, 14:02 or 2026-10-12T14:02") from exc
    today = datetime.fromtimestamp(now)
    when = datetime.combine(today.date(), of_day)
    if when.timestamp() > now:
        when -= timedelta(days=1)
    return when.timestamp()
