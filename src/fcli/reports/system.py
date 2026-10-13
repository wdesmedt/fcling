"""sys-info: the chassis and the software it runs; config-commits: who changed it, when."""

from __future__ import annotations

from collections.abc import Iterator
from typing import ClassVar

from ..report import Column, NodeState, Read, Record, as_int, report

_CHASSIS_LEAVES = ("type", "serial-number", "part-number", "hw-mac-address", "last-booted")


class SystemInfo(Record):
    type: str = ""
    serial_number: str = ""
    part_number: str = ""
    hw_mac_address: str = ""
    last_booted: str = ""
    #: The release alone, ``26.7.1``: SR Linux reports ``v26.7.1-554-g78ed635f70a``.
    software_version: str = ""


@report(
    "sys-info",
    "System information of nodes",
    record=SystemInfo,
    # The leaves alone: the chassis container also carries power and environment readings.
    reads=[
        *(Read(f"/platform/chassis/{leaf}") for leaf in _CHASSIS_LEAVES),
        Read("/platform/control[slot=A]/software-version"),
    ],
)
def sys_info(node: NodeState) -> Iterator[SystemInfo]:
    chassis = node.tree.get("/platform/chassis") or {}
    version = str(node.tree.get("/platform/control[slot=A]/software-version") or "")
    yield SystemInfo(
        node=node.name,
        type=str(chassis.get("type") or ""),
        serial_number=str(chassis.get("serial-number") or ""),
        part_number=str(chassis.get("part-number") or ""),
        hw_mac_address=str(chassis.get("hw-mac-address") or ""),
        last_booted=str(chassis.get("last-booted") or ""),
        software_version=version.split("-")[0].lstrip("v"),
    )


class ConfigCommit(Record):
    """One commit to the running configuration, as the node logs it.

    SR Linux keeps a bounded log, so the oldest commits age out: it says who
    changed the configuration and when, not what.
    """

    key: ClassVar[tuple[str, ...]] = ("id",)

    id: int
    #: ``complete``, or how a commit that did not finish ended.
    status: str = ""
    username: str = ""
    comment: str = ""
    #: The candidate it was made in: ``shared``, ``private``, ``exclusive``.
    type: str = ""
    #: The candidate's name, ``default`` for the shared one.
    session: str = ""
    started: str = ""
    ended: str = ""


@report(
    "config-commits",
    "Commits each node logged to its configuration",
    record=ConfigCommit,
    reads=[Read("/system/configuration/commit")],
    columns=[
        Column("id", "id"),
        Column("status", "status"),
        Column("user", "username"),
        Column("comment", "comment"),
        Column("session", "session"),
        Column("type", "type"),
        Column("started", "started"),
        Column("ended", "ended"),
    ],
)
def config_commits(node: NodeState) -> Iterator[ConfigCommit]:
    commits = []
    for m in node.tree.select("/system/configuration/commit"):
        entry = m.value
        commit_id = as_int(entry.get("id"))
        if commit_id is None:
            continue
        commits.append(
            ConfigCommit(
                node=node.name,
                id=commit_id,
                status=str(entry.get("status") or ""),
                username=str(entry.get("username") or ""),
                comment=str(entry.get("comment") or ""),
                type=str(entry.get("type") or ""),
                session=str(entry.get("name") or ""),
                started=str(entry.get("started") or ""),
                ended=str(entry.get("ended") or ""),
            )
        )
    yield from sorted(commits, key=lambda c: c.id)
