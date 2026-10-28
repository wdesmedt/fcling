"""What the CLI prints of the fabric's data, as it is: a MAC's or an ESI's
":ab:" is no emoji, a path's "[...]" no markup."""

import io

from rich.console import Console

from fcli.cli import _print_changes, _print_topology

MAC = "00:ab:cd:00:00:01"
ESI = "01:ab:cd:00:00:00:00:00:00:01"


def _console() -> tuple[Console, io.StringIO]:
    # Rich's defaults, emoji and markup on: what is printed must not depend on them.
    out = io.StringIO()
    return Console(file=out, width=300), out


def test_topology_prints_a_client_mac_and_an_esi_as_they_are() -> None:
    graph = {
        "nodes": [{"name": "leaf1", "tier": "leaf", "health": "ok"}],
        "components": [{"name": "fabric", "nodes": ["leaf1"]}],
        "links": [],
        "clients": [
            {
                "label": MAC,
                "nodes": ["leaf1"],
                "esi": ESI,
                "attachments": [{"node": "leaf1", "port": "ethernet-1/1", "service": "mac-vrf[bold]"}],
            }
        ],
        "segments": [{"names": [], "esi": ESI, "mode": "all-active", "nodes": [], "next_hops": []}],
        "outside": [],
    }
    console, out = _console()
    _print_topology(graph, console)
    assert f"{MAC}: leaf1 ethernet-1/1  ES {ESI}  mac-vrf[bold]" in out.getvalue()
    assert f"  {ESI}  all-active" in out.getvalue()


def test_the_cli_consoles_print_no_emoji(capsys) -> None:  # noqa: ANN001
    _print_changes(
        [
            {
                "ts": 0,
                "node": "leaf1",
                "report": "mac-table",
                "op": "add",
                "key": f"mac-vrf[1] {MAC}",
                "after": {},
            }
        ],
        "table",
    )
    assert f"mac-vrf[1] {MAC}" in " ".join(capsys.readouterr().out.split())
