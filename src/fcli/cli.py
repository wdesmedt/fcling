"""``fcling``: reports over a fabric, one-shot or from a running engine.

Every report in the registry is a command; the options shared by all of them -
where the inventory comes from, how to reach the nodes, how to print - are on
the group. With ``--server`` (or ``FCLI_SERVER``) a report is answered by a
running ``fcling serve`` from what it streams, and ``--at`` asks what it was.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape

from . import inventory as inv
from . import render, views
from .checks import CHECKS, finding_event, required, run_checks
from .collect import collect
from .gnmi.client import Tls
from .incidents import correlate
from .remote import LOCAL, Remote, RemoteError, find_kept, find_token, keep_token, token_path
from .report import Param, Record, Report
from .reports import REGISTRY, bgp_rib
from .store import BeforeHistory, Store, default_path, matches, parse_time
from .tls import cert_names, cert_path, fingerprint, keep_certificate, loopback

try:
    from ._version import __version__
except ImportError:  # running from a checkout that was never built
    __version__ = "0.0.0"

# No emoji: a MAC's or an ESI's ":ab:" is none; data in markup is escaped.
err = Console(stderr=True, emoji=False)


@dataclass
class Settings:
    topo: Path | None
    inventory_file: Path | None
    filters: tuple[str, ...]
    #: Applied to every node, over what the inventory says.
    overrides: dict[str, Any]
    output: str
    box: str
    timeout: float
    server: str | None = None
    token: str | None = None
    server_ca: Path | None = None

    def remote(self) -> Remote | None:
        if not self.server:
            return None
        remote = Remote(self.server, self.token, ca=self.server_ca)
        if remote.token is None and remote.local:
            remote.token = self._kept_token()
        if remote.tls and remote.ca is None and remote.local:
            remote.ca = self._kept_cert()
        return remote

    def _history(self) -> Path | None:
        """This fabric's history, if the inventory tells which fabric it is."""
        source = self.inventory_file or self.topo or inv.discover()
        if source is None:
            return None
        try:
            loaded = inv.from_file(source) if self.inventory_file else inv.from_clab(source)
        except inv.InventoryError:
            return None
        return default_path(loaded.name)

    def _kept_token(self) -> str | None:
        """The token a server on this machine made up: its fabric's, or the only one kept."""
        history = self._history()
        preferred = token_path(history) if history else None
        return find_token(preferred, default_path("").parent.glob("*.token"))

    def _kept_cert(self) -> Path | None:
        """The certificate a server on this machine made up: its fabric's, or the only one kept."""
        history = self._history()
        preferred = cert_path(history) if history else None
        return find_kept(preferred, default_path("").parent.glob("*.crt"))

    def fabric(self) -> inv.Inventory:
        """The selected nodes. Loaded here, not in the group, so ``--help`` needs no inventory."""
        if self.topo and self.inventory_file:
            raise click.UsageError("-t/--topo and -c/--inventory are mutually exclusive")
        source = self.inventory_file or self.topo or inv.discover()
        if source is None:
            raise click.UsageError(
                "no inventory: pass -t <lab>.clab.yml or -c <inventory>.yml "
                "(or run from a directory holding exactly one *.clab.yml)"
            )
        try:
            loaded = inv.from_file(source) if self.inventory_file else inv.from_clab(source)
            fabric = loaded.select(self.filters)
        except inv.InventoryError as exc:
            raise click.UsageError(str(exc)) from exc
        if not fabric.nodes:
            raise click.UsageError(
                "no nodes in the inventory match" + (f" {' '.join(self.filters)}" if self.filters else "")
            )
        return fabric.override(**self.overrides)


@click.group(context_settings={"help_option_names": ["-h", "--help"], "auto_envvar_prefix": "FCLI"})
@click.option(
    "-t",
    "--topo",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="containerlab topology file",
)
@click.option(
    "-c",
    "--inventory",
    "inventory_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="YAML inventory file",
)
@click.option(
    "-i",
    "--inv-filter",
    "filters",
    multiple=True,
    metavar="KEY=GLOB",
    help="select nodes by name or label, e.g. -i role=leaf; repeat to AND",
)
@click.option("-u", "--username", help="gNMI username for every node")
@click.option("-P", "--password", help="gNMI password for every node")
@click.option("-p", "--gnmi-port", "port", type=int, help="gNMI port for every node")
@click.option(
    "--cert-file",
    type=click.Path(exists=True, dir_okay=False),
    help="PEM trust anchor for the nodes' gNMI certificates",
)
@click.option(
    "--verify/--skip-verify",
    default=None,
    help="verify node certificates [default: verify only with --cert-file]",
)
@click.option("--tls-server-name", help="name to verify certificates against, if not the address")
@click.option("--insecure", is_flag=True, default=None, help="plaintext gNMI, no TLS")
@click.option("-o", "--output", type=click.Choice(render.FORMATS), default="table", show_default=True)
@click.option("-b", "--box", default="MINIMAL", show_default=True, help="Rich box style of tables")
@click.option("--timeout", type=float, default=30.0, show_default=True, help="seconds per gNMI call")
@click.option(
    "--server", metavar="URL", help="answer from a running `fcling serve`, e.g. http://127.0.0.1:8765"
)
@click.option("--token", help="the bearer token the server was started with")
@click.option(
    "--server-ca",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="PEM certificate to verify an https --server against "
    "[default: on the server's machine, the one it kept; else the system's]",
)
@click.option(
    "-l",
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
    default="WARNING",
    show_default=True,
)
@click.version_option(__version__, "--version")
@click.pass_context
def main(
    ctx: click.Context,
    topo: Path | None,
    inventory_file: Path | None,
    filters: tuple[str, ...],
    username: str | None,
    password: str | None,
    port: int | None,
    cert_file: str | None,
    verify: bool | None,
    tls_server_name: str | None,
    insecure: bool | None,
    output: str,
    box: str,
    timeout: float,
    server: str | None,
    token: str | None,
    server_ca: Path | None,
    log_level: str,
) -> None:
    """Reports on an SR Linux fabric, straight from gNMI."""
    logging.basicConfig(level=log_level, format="%(levelname)s %(name)s: %(message)s")
    tls = None
    if any(v is not None for v in (cert_file, verify, tls_server_name, insecure)):
        tls = Tls(
            skip_verify=None if verify is None else not verify,
            ca_file=cert_file,
            server_name=tls_server_name,
            insecure=bool(insecure),
        )
    ctx.obj = Settings(
        topo=topo,
        inventory_file=inventory_file,
        filters=filters,
        overrides={"username": username, "password": password, "port": port, "tls": tls},
        output=output,
        box=box,
        timeout=timeout,
        server=server,
        token=token,
        server_ca=server_ca,
    )


def _broken(what: str, exc: ImportError) -> click.ClickException:
    """Every dependency comes with fcling: one that is missing is a broken install."""
    return click.ClickException(
        f"{what} cannot load {exc.name or exc}: the install is incomplete; "
        "reinstall it with `uv tool install --reinstall fcling`"
    )


def _login_url(host: str, port: int, token: str | None, tls: bool = False, names: Sequence[str] = ()) -> str:
    """The UI's address to open, or pass as ``--server``: with the token in it, if there is one.

    *names* are those a certificate given is for: the address is one of them, for it to verify.
    """
    import socket

    # Listening on every address: this machine's name is one that others reach it by.
    name = socket.gethostname() if host in ("", "0.0.0.0", "::") else host  # noqa: S104 - compared, not bound
    if names and name not in names:
        name = next((n for n in names if not n.startswith("*.")), name)
    if ":" in name:
        name = f"[{name}]"
    return f"{'https' if tls else 'http'}://{name}:{port}/" + (f"?token={token}" if token else "")


def _collect(
    settings: Settings, report: Report, params: dict[str, Any], interval: float | None, at: str | None
) -> tuple[list[Record], dict[str, str]]:
    """*report*'s records, and the nodes that failed: from the server, the history, or the nodes."""
    remote = settings.remote()
    if remote is not None:
        try:
            answer = remote.records(report.name, params, settings.filters, at)
        except RemoteError as exc:
            raise click.ClickException(str(exc)) from exc
        return [report.record.model_validate(r) for r in answer["records"]], answer["errors"]
    fabric = settings.fabric()
    if at is not None:
        path = default_path(fabric.name)
        if not path.exists():
            raise click.UsageError(
                f"no history for {fabric.name}: {path} does not exist (run `fcling serve`)"
            )
        try:
            when = parse_time(at)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--at") from exc
        store = Store(path)
        try:
            state = store.state_at(when, report.name, [n.name for n in fabric.nodes])
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--at") from exc
        finally:
            store.close()
        return [report.record.model_validate(r) for rows in state.values() for r in rows.values()], {}
    results = asyncio.run(
        collect(fabric.nodes, [report], rpc_timeout=settings.timeout, interval=interval, params=params)
    )
    records = [r for result in results for r in result.records.get(report.name, [])]
    return records, {result.node.name: result.error for result in results if result.error}


def _run(
    settings: Settings,
    report: Report,
    field_filters: tuple[str, ...],
    params: dict[str, Any],
    detail: bool = False,
    interval: float | None = None,
    at: str | None = None,
) -> None:
    """*report*'s records, printed; which nodes failed, said."""
    try:
        filters = render.parse_filters(report, field_filters)
    except render.FilterError as exc:
        raise click.UsageError(str(exc)) from exc
    records, errors = _collect(settings, report, params, interval, at)
    records = [r for r in records if render.keep(report, r, filters)]
    if settings.output == "table":
        render.table(report, records, Console(emoji=False), settings.box, detail)
    elif settings.output == "json":
        click.echo(render.as_json(records))
    elif settings.output == "yaml":
        click.echo(render.as_yaml(records), nl=False)
    else:
        click.echo(render.as_csv(report, records, detail), nl=False)
    for node, error in sorted(errors.items()):
        err.print(f"[yellow]{node}[/]: {escape(str(error))}", highlight=False)
    if errors:
        sys.exit(1)


_FIELD_FILTER = click.option(
    "-f",
    "--field-filter",
    "field_filters",
    multiple=True,
    metavar="COLUMN=REGEX",
    help="keep rows whose column matches, case-insensitive; repeat to AND",
)
_DETAIL = click.option("-d", "--detail", is_flag=True, help="show the detail columns too")
_AT = click.option(
    "--at",
    metavar="TIME",
    help="as it was then: 30m, 2h, 14:02, 2026-10-12T14:02 (from the history the server keeps)",
)


def _param_option(param: Param) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    flags = [f"--{param.name.replace('_', '-')}"] + ([f"-{param.short}"] if param.short else [])
    return click.option(
        *flags,
        param.name,
        type=click.Choice(param.choices) if param.choices else str,
        default=param.default,
        show_default=param.default is not None,
        help=param.help,
    )


def _command(report: Report) -> click.Command:
    @_FIELD_FILTER
    @_AT
    @click.pass_obj
    def run(
        settings: Settings,
        field_filters: tuple[str, ...],
        at: str | None,
        detail: bool = False,
        interval: float | None = None,
        **params: Any,
    ) -> None:
        _run(settings, report, field_filters, params, detail, interval, at)

    for param in report.params:
        run = _param_option(param)(run)
    if report.detail_columns:
        run = _DETAIL(run)
    if report.interval:
        run = click.option(
            "-s",
            "--interval",
            type=float,
            default=report.interval,
            show_default=True,
            help="seconds between the two samples",
        )(run)
    return click.command(report.name, help=report.help, hidden=report.hidden)(run)


for _report in REGISTRY.values():
    main.add_command(_command(_report))


def _bgp_rib_key_options(cmd: click.Command) -> click.Command:
    for yang in sorted({k for keys in bgp_rib.RIB_KEYS.values() for k in keys}):
        opt = bgp_rib._param_name(yang)
        cmd = click.option(f"--{yang}", opt, default="", help=bgp_rib._RIB_KEY_HELP.get(yang, ""))(cmd)
    return cmd


@_bgp_rib_key_options
@main.command("bgp-rib")
@click.option(
    "-r",
    "--route-fam",
    "family",
    type=click.Choice(sorted(bgp_rib.FAMILIES)),
    default="evpn",
    show_default=True,
    help="address family",
)
@click.option(
    "-t",
    "--route-type",
    type=click.Choice(sorted(bgp_rib.EVPN_ROUTES) + ["all"]),
    default="2",
    show_default=True,
    help="EVPN route type (all: every type in one table)",
)
@click.option("--all", "list_all", is_flag=True, help="every route on every node (slow on large fabrics)")
@click.option("--ni", default="", show_default=False, help="network-instance to read")
@_param_option(bgp_rib.PATHS)
@_DETAIL
@_FIELD_FILTER
@click.pass_obj
def bgp_rib_command(
    settings: Settings,
    family: str,
    route_type: str,
    list_all: bool,
    ni: str,
    paths: str,
    detail: bool,
    field_filters: tuple[str, ...],
    **keys: str,
) -> None:
    """BGP RIB of one address family"""
    model = bgp_rib.FAMILIES[family]
    if model == "evpn" and route_type == "all":
        report = REGISTRY[bgp_rib.EVPN_ALL_REPORT]
        rt = ""
    else:
        report = REGISTRY[bgp_rib.report_name(family, route_type)]
        rt = route_type if model == "evpn" else ""
    params: dict[str, Any] = {
        "paths": paths,
        "ni": ni,
        "list_all": "all" if list_all else "",
        **{k: v for k, v in keys.items() if v},
    }
    if rt == "" and model == "evpn":
        asked = bgp_rib.evpn_rib_requested(params)
    else:
        asked = bgp_rib.rib_requested(model, rt, params)
    if not asked:
        raise click.UsageError("give a route key, --ni, or --all")
    _run(settings, report, field_filters, params, detail)


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="address to listen on")
@click.option("--port", default=8765, show_default=True, type=int)
@click.option(
    "--db", type=click.Path(dir_okay=False, path_type=Path), help="history database [default: per fabric]"
)
@click.option("--poll", default=10.0, show_default=True, type=float, help="seconds between polls of counters")
@click.option(
    "-r", "--report", "names", multiple=True, metavar="NAME", help="reports to stream [default: all of them]"
)
@click.option(
    "--api-token",
    envvar="FCLI_API_TOKEN",
    help="require this bearer token [default: none on localhost, else one kept next to the history]",
)
@click.option(
    "--tls-cert",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="PEM certificate to serve HTTPS with "
    "[default: beyond loopback, one made up and kept next to the history]",
)
@click.option(
    "--tls-key",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="the PEM private key of --tls-cert",
)
@click.option("--no-tls", is_flag=True, help="plain HTTP beyond loopback, e.g. behind a proxy that does TLS")
@click.option(
    "--persist-acks",
    is_flag=True,
    help="keep acknowledged incidents across restarts, in a file next to the history",
)
@click.option(
    "-d",
    "--daemon",
    is_flag=True,
    help="detach from the terminal and log to a file; a pid file next to the history says where it runs",
)
@click.option(
    "--log-file",
    type=click.Path(dir_okay=False, path_type=Path),
    help="with --daemon: where the log goes [default: next to the history]",
)
@click.pass_obj
def serve(
    settings: Settings,
    host: str,
    port: int,
    db: Path | None,
    poll: float,
    names: tuple[str, ...],
    api_token: str | None,
    tls_cert: Path | None,
    tls_key: Path | None,
    no_tls: bool,
    persist_acks: bool,
    daemon: bool,
    log_file: Path | None,
) -> None:
    """Stream the fabric, keep its history, and serve both over HTTP"""
    try:
        import uvicorn  # noqa: F401

        from .acks import AckStore
        from .acks import path_for as acks_path
        from .engine import Engine
        from .server import create_app, run
        from .store import AsyncStore
    except ImportError as exc:
        raise _broken("the server", exc) from exc
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        raise click.UsageError(f"no report {', '.join(unknown)}")
    fabric = settings.fabric()
    reports = [REGISTRY[n] for n in names] or [r for r in REGISTRY.values() if not r.hidden]
    if log_file and not daemon:
        raise click.UsageError("--log-file goes with --daemon")
    if bool(tls_cert) != bool(tls_key):
        raise click.UsageError("--tls-cert and --tls-key go together")
    if no_tls and tls_cert:
        raise click.UsageError("--no-tls and --tls-cert are mutually exclusive")
    path = (db or default_path(fabric.name)).resolve()
    tls = None
    kept_cert = False
    given: list[str] = []
    if tls_cert and tls_key:
        tls = (tls_cert.resolve(), tls_key.resolve())
        given = cert_names(tls[0])
    elif not no_tls and not loopback(host):
        # Beyond loopback the token and the fabric's state cross the network: never in the clear.
        tls = keep_certificate(path, host)
        kept_cert = True
    kept = None
    if host not in LOCAL and not api_token:
        # Made up once and kept: a restart lets the same browsers and clients in.
        kept = token_path(path)
        api_token = keep_token(kept)
    if daemon:
        from .daemon import running

        if (pid := running(path.with_suffix(".pid"))) is not None:
            raise click.ClickException(f"already running as pid {pid}, for history {path}")
    acks = AckStore(acks_path(path) if persist_acks else None)
    login = _login_url(host, port, api_token, tls is not None, given)
    banner = (
        f"{fabric.name}: {len(fabric.nodes)} nodes, {len(reports)} reports, history in {path}"
        + (f", acknowledgements in {acks.path} ({len(acks.all())} kept)" if acks.path else "")
        + f"; UI at {login}, API at /api/status, MCP at /mcp"
        + (f"; token kept in {kept}" if kept else "")
        + (f"; certificate kept in {tls[0]}, SHA-256 {fingerprint(tls[0])}" if tls and kept_cert else "")
    )
    # Soft-wrapped: the login URL stays one string to copy, whatever the width of the terminal.
    # Printed as it is: a fingerprint's ":AB:" is no emoji, and a path's "[...]" no markup.
    err.print(banner, highlight=False, soft_wrap=True, markup=False, emoji=False)
    ready = None
    if daemon:
        from .daemon import DaemonError, detach

        log = (log_file or path.with_suffix(".log")).resolve()
        try:
            # Before the store and the engine: a fork keeps no threads.
            ready = detach(log, path.with_suffix(".pid"))
        except DaemonError as exc:
            raise click.ClickException(str(exc)) from exc
        logging.getLogger(__name__).warning("started: %s", banner)
    store = AsyncStore(path)
    engine = Engine(fabric.nodes, reports, store, poll_interval=poll)
    try:
        run(
            create_app(engine, fabric, api_token, host=host, rpc_timeout=settings.timeout, acks=acks),
            host,
            port,
            ready,
            tls,
        )
    finally:
        store.close()


def _when(ts: float | None) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def _summary(change: dict[str, Any]) -> str:
    """What a change changed, in a line: the fields that moved, or what came or went."""
    before, after = change.get("before") or {}, change.get("after") or {}
    if change["report"] == "checks" and change["op"] != "change":
        f = after if change["op"] == "add" else before
        return f"{f.get('check', '')} {f.get('subject', '')}: {f.get('detail', '')}"
    if change["op"] == "change":
        moved = [k for k in after if k != "node" and before.get(k) != after.get(k)]
        return "; ".join(f"{k}: {_short(before.get(k))} -> {_short(after.get(k))}" for k in moved)
    return ""


def _short(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    return text if len(text) <= 60 else text[:57] + "..."


def _print_changes(changes: list[dict[str, Any]], output: str) -> None:
    if output == "json":
        click.echo(json.dumps(changes, indent=2))
        return
    if output == "yaml":
        import yaml

        click.echo(yaml.safe_dump(changes, sort_keys=False), nl=False)
        return
    from rich import box as rich_box
    from rich.table import Table
    from rich.text import Text

    table = Table(box=rich_box.MINIMAL, title="History")
    for column in ("time", "node", "report", "op", "key", "what", "after"):
        table.add_column(column)
    for c in changes:
        table.add_row(
            _when(c["ts"]),
            c["node"],
            c["report"],
            finding_event(c) or c["op"],
            Text(str(c["key"])),
            Text(_summary(c)),
            Text(c.get("cause") or ""),
        )
    Console(emoji=False).print(table)


@main.command()
@click.option("--since", default="1h", show_default=True, metavar="TIME", help="1h, 30m, 2d, or a date")
@click.option(
    "--since-baseline",
    "baseline",
    is_flag=False,
    flag_value="",
    default=None,
    metavar="[NAME]",
    help="only what changed since that baseline [default: the latest one]",
)
@click.option("--until", metavar="TIME")
@click.option("-n", "--node", "nodes", multiple=True)
@click.option("-r", "--report", "reports", multiple=True)
@click.option(
    "-q",
    "--grep",
    "match",
    default="",
    metavar="TEXT",
    help="only changes with every word of TEXT in their node, report, key or a value",
)
@click.option("--limit", default=1000, show_default=True)
@click.option("--follow", is_flag=True, help="keep printing changes as they happen (needs --server)")
@click.pass_obj
def history(
    settings: Settings,
    since: str,
    baseline: str | None,
    until: str | None,
    nodes: tuple[str, ...],
    reports: tuple[str, ...],
    match: str,
    limit: int,
    follow: bool,
) -> None:
    """What changed: the history the server keeps"""
    remote = settings.remote()
    try:
        start = parse_time(since) if baseline is None else _baseline_at(settings, baseline)
        end = parse_time(until) if until else None
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    if remote is not None:
        try:
            changes = remote.history(
                since=start, until=end, node=list(nodes), report=list(reports), q=match or None, limit=limit
            )
        except RemoteError as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        if follow:
            raise click.UsageError("--follow needs --server")
        path = default_path(settings.fabric().name)
        if not path.exists():
            raise click.UsageError(f"no history: {path} does not exist (run `fcling serve`)")
        store = Store(path)
        try:
            changes = views.history_with_causes(
                store, since=start, until=end, nodes=nodes, reports=reports, match=match, limit=limit
            )
        finally:
            store.close()
    _print_changes(changes, settings.output)
    if follow and remote is not None:
        query = [("node", n) for n in nodes] + [("report", r) for r in reports]
        if changes:
            query.append(("after", str(changes[-1]["seq"])))
        try:
            for event in remote.events(query):
                if event.get("type") == "change" and event.get("op") != "update":
                    if not match or matches(event, match):
                        _print_changes([event], settings.output)
                elif event.get("type") == "node":
                    err.print(
                        f"[dim]{event['name']}: {event['status']} {escape(str(event.get('error', '')))}[/]"
                    )
        except KeyboardInterrupt:
            pass
        except RemoteError as exc:
            raise click.ClickException(str(exc)) from exc


def _baseline_at(settings: Settings, name: str) -> float:
    """When baseline *name* was set, or the latest baseline when *name* is empty."""
    if settings.remote() is not None:
        kept = _remote_get(settings, "/api/baselines", [])
    else:
        store = _local_store(settings)
        try:
            kept = store.baselines()
        finally:
            store.close()
    picked = [b for b in kept if b["name"] == name] if name else kept[-1:]
    if not picked:
        hint = "no baseline is kept: set one with `fcling baseline`"
        raise click.UsageError(f"no baseline {name!r}" if name else hint)
    return float(picked[0]["ts"])


# --------------------------------------------------------------------------- #
# what is wrong
# --------------------------------------------------------------------------- #


def _local_store(settings: Settings) -> Store:
    path = default_path(settings.fabric().name)
    if not path.exists():
        raise click.UsageError(f"no history: {path} does not exist (run `fcling serve`)")
    return Store(path)


def _fabric_view(settings: Settings, at: str | None, names: Sequence[str] = ()) -> Any:
    """The fabric the checks look at: the nodes now, or the local history at *at*."""
    fabric = settings.fabric()
    reports = required(names or None)
    if at is None:
        return asyncio.run(views.fabric_now(fabric.nodes, reports, settings.timeout))
    store = _local_store(settings)
    try:
        return views.fabric_at(store, fabric.nodes, parse_time(at), reports)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--at") from exc
    finally:
        store.close()


def _remote_get(settings: Settings, path: str, query: list[tuple[str, str]]) -> Any:
    remote = settings.remote()
    assert remote is not None
    try:
        return remote.get(path, [("inv", f) for f in settings.filters] + query)
    except RemoteError as exc:
        raise click.ClickException(str(exc)) from exc


_SEVERITY_STYLE = {"error": "bold red", "warning": "yellow", "info": "dim"}


def _dump(data: Any, output: str) -> bool:
    """Print *data* in a structured format; whether it did."""
    if output == "json":
        click.echo(json.dumps(data, indent=2, default=str))
    elif output == "yaml":
        import yaml

        click.echo(yaml.safe_dump(data, sort_keys=False), nl=False)
    elif output == "csv" and isinstance(data, list) and data and isinstance(data[0], dict):
        import csv
        import io

        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(data[0]), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(data)
        click.echo(out.getvalue(), nl=False)
    else:
        return False
    return True


@main.command("checks")
@click.option(
    "-k", "--check", "names", multiple=True, type=click.Choice(sorted(CHECKS)), help="only these checks"
)
@_AT
@click.pass_obj
def checks_command(settings: Settings, names: tuple[str, ...], at: str | None) -> None:
    """What is wrong with the fabric, check by check"""
    if settings.remote() is not None:
        query = [("check", n) for n in names] + ([("at", at)] if at else [])
        rows = _remote_get(settings, "/api/checks", query)
    else:
        findings = run_checks(_fabric_view(settings, at, names), names or None)
        rows = [{**f.model_dump(mode="json"), "raised": None} for f in findings]
    if _dump(rows, settings.output):
        return
    from rich import box as rich_box
    from rich.table import Table
    from rich.text import Text

    table = Table(box=getattr(rich_box, settings.box.upper(), rich_box.MINIMAL), title="Checks")
    for column in ("severity", "check", "node", "subject", "detail", "since"):
        table.add_column(column)
    for f in rows:
        style = _SEVERITY_STYLE.get(f["severity"], "")
        # As they are: an ESI's ":ab:" is no emoji, a path's "[...]" no markup.
        table.add_row(
            f"[{style}]{f['severity']}[/]",
            f["check"],
            f["node"],
            Text(f["subject"]),
            Text(f["detail"]),
            _when(f.get("raised")),
        )
    Console(emoji=False).print(table if rows else "[green]No findings.[/]")
    if any(f["severity"] == "error" for f in rows):
        sys.exit(1)


@main.command("incidents")
@click.option("-v", "--verbose", is_flag=True, help="list every finding of each incident")
@_AT
@click.pass_obj
def incidents_command(settings: Settings, verbose: bool, at: str | None) -> None:
    """What is wrong, grouped by root cause, worst first"""
    if settings.remote() is not None:
        found = _remote_get(settings, "/api/incidents", [("at", at)] if at else [])
    else:
        fabric = _fabric_view(settings, at)
        found = [i.as_dict() for i in correlate(run_checks(fabric), fabric)]
    if _dump(found, settings.output):
        return
    from rich.text import Text

    console = Console(emoji=False)
    if not found:
        console.print("[green]No incidents.[/]")
        return
    for i in found:
        style = _SEVERITY_STYLE.get(i["severity"], "")
        count = 1 + len(i["related"])
        # As they are: an ESI's ":ab:" is no emoji, a path's "[...]" no markup.
        console.print(
            Text.assemble(
                (f"{i['severity'].upper():7}", style),
                " ",
                (i["title"], "bold"),
                (f"  ({count} finding{'s' if count != 1 else ''})", "dim"),
                (f"  since {_when(i['first_seen'])}" if i.get("first_seen") else "", "dim"),
            )
        )
        console.print(Text(f"        {i['explanation']}"))
        if verbose:
            for f in [i["root"], *i["related"]]:
                console.print(
                    Text(f"          {f['check']:20} {f['node']:12} {f['subject']}: {f['detail']}", "dim")
                )
    if any(i["severity"] == "error" for i in found):
        sys.exit(1)


# --------------------------------------------------------------------------- #
# baselines and drift
# --------------------------------------------------------------------------- #


@main.command()
@click.argument("name")
@click.option("--at", metavar="TIME", help="the baseline is the fabric as it was then [default: now]")
@click.option("-m", "--comment", default="", help="what it is the baseline of")
@click.pass_obj
def baseline(settings: Settings, name: str, at: str | None, comment: str) -> None:
    """Name the fabric as it is now (or was then), to compare against later"""
    remote = settings.remote()
    if remote is not None:
        import urllib.request

        body = json.dumps({"name": name, "at": at, "comment": comment}).encode()
        request = urllib.request.Request(f"{remote.url}/api/baselines", data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        if remote.token:
            request.add_header("Authorization", f"Bearer {remote.token}")
        try:
            urllib.request.urlopen(request, timeout=remote.timeout).read()  # noqa: S310 - the user's URL
        except OSError as exc:
            raise click.ClickException(f"{remote.url}: {exc}") from exc
    else:
        store = _local_store(settings)
        try:
            store.set_baseline(name, parse_time(at) if at else time.time(), comment)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--at") from exc
        finally:
            store.close()
    err.print(f"baseline [bold]{name}[/] kept", highlight=False)


@main.command()
@click.pass_obj
def baselines(settings: Settings) -> None:
    """The baselines kept for this fabric"""
    if settings.remote() is not None:
        rows = _remote_get(settings, "/api/baselines", [])
    else:
        store = _local_store(settings)
        try:
            rows = store.baselines()
        finally:
            store.close()
    if _dump(rows, settings.output):
        return
    for b in rows:
        click.echo(f"{b['name']:24} {_when(b['ts'])}  {b['comment']}")


@main.command()
@click.argument("since", metavar="BASELINE|TIME")
@click.option("--to", "until", metavar="TIME", help="compare with then rather than now")
@click.option(
    "-r", "--report", "reports", multiple=True, help="only these reports [default: every report kept]"
)
@click.pass_obj
def drift(settings: Settings, since: str, until: str | None, reports: tuple[str, ...]) -> None:
    """How the fabric differs from a baseline: what came, went and changed since"""
    if settings.remote() is not None:
        query = [("from", since), *(("report", r) for r in reports)] + ([("to", until)] if until else [])
        answer = _remote_get(settings, "/api/drift", query)
        changes = answer["changes"]
    else:
        store = _local_store(settings)
        try:
            kept = store.baseline(since)
            start = kept["ts"] if kept else parse_time(since)
            end = parse_time(until) if until else None
            names = [n.name for n in settings.fabric().nodes]
            changes = [
                c.as_dict()
                for r in (reports or views.drift_reports(store))
                for c in views.drift(store, r, start, end, names)
            ]
        except BeforeHistory as exc:
            raise click.UsageError(str(exc)) from exc
        except ValueError as exc:
            raise click.UsageError(f"{since!r} is neither a baseline nor a time") from exc
        finally:
            store.close()
    if _dump(changes, settings.output):
        return
    if not changes:
        Console(emoji=False).print("[green]No drift.[/]")
        return
    _print_changes(changes, "table")


_DIFF_STYLE = {"added": "green", "removed": "red", "changed": "yellow"}


@main.command("config-diff")
@click.argument("node")
@click.argument("commit", type=int, required=False)
@click.option(
    "--base", type=int, help="compare with the configuration after this commit [default: the one before]"
)
@click.option(
    "--at", "at", metavar="TIME", help="the commit made at TIME (ISO 8601, 14:02, 1h), in place of COMMIT"
)
@click.option("-C", "--context", default=3, show_default=True, help="unchanged lines around each change")
@click.option("--list", "listing", is_flag=True, help="list the configurations kept for NODE instead")
@click.pass_obj
def config_diff(
    settings: Settings,
    node: str,
    commit: int | None,
    base: int | None,
    at: str | None,
    context: int,
    listing: bool,
) -> None:
    """What a commit changed in a node's configuration, side by side

    The server keeps each node's configuration after every commit it sees,
    secrets redacted. Without COMMIT, the latest change. Commit ids repeat:
    --at, when the commit was made, compares the first configuration kept
    from then on with the last kept before.
    """
    if at is not None and commit is not None:
        raise click.UsageError("give COMMIT or --at, not both")
    query = [("node", node), ("context", str(context))]
    query += [("commit", str(commit))] if commit is not None else []
    query += [("base", str(base))] if base is not None else []
    try:
        when = parse_time(at) if at is not None else None
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--at") from exc
    # As an instant here: a time of day is this side's, not the server's.
    query += [("at", str(when))] if when is not None else []
    if settings.remote() is not None:
        answer = _remote_get(settings, "/api/configs" if listing else "/api/configs/diff", query)
    else:
        store = _local_store(settings)
        try:
            answer = (
                views.configs(store, [node])
                if listing
                else views.config_diff(store, node, commit, base, context, at=when)
            )
        except LookupError as exc:
            raise click.ClickException(str(exc)) from exc
        finally:
            store.close()
    if _dump(answer, settings.output):
        return
    if listing:
        for c in answer:
            click.echo(f"commit {c['commit_id'] or '-':>6}  {_when(c['ts'])}  {c['lines']:6} lines")
        return
    _print_config_diff(answer)


def _print_config_diff(answer: dict[str, Any]) -> None:
    from rich import box as rich_box
    from rich.table import Table
    from rich.text import Text

    console = Console(emoji=False)
    spans = ", ".join(
        f"{c.get('id')} by {c.get('username') or '?'}" + (f" ({c['comment']})" if c.get("comment") else "")
        for c in answer["commits"]
    )
    before, after = answer["before"], answer["after"]
    console.print(
        f"[bold]{answer['node']}[/]: {escape(answer['summary'])}"
        + (f" — commit {escape(spans)}" if spans else "")
    )
    if before is None:
        console.print("[dim]nothing kept before it to compare with[/]")
        return
    table = Table(box=rich_box.SIMPLE_HEAD, expand=True, show_edge=False, pad_edge=False)
    table.add_column("", justify="right", style="dim", no_wrap=True)
    table.add_column(f"after commit {before['commit_id']} · {_when(before['ts'])}", ratio=1, overflow="fold")
    table.add_column("", justify="right", style="dim", no_wrap=True)
    table.add_column(f"after commit {after['commit_id']} · {_when(after['ts'])}", ratio=1, overflow="fold")
    for row in answer["rows"]:
        if row["kind"] == "skip":
            table.add_row("", Text(f"⋯ {row['count']} unchanged lines", style="dim"), "", "")
            continue
        left, right = row.get("left"), row.get("right")
        style = _DIFF_STYLE.get(row["kind"], "")
        table.add_row(
            str(left["n"]) if left else "",
            Text(left["text"], style=style) if left else "",
            str(right["n"]) if right else "",
            Text(right["text"], style=style) if right else "",
        )
    console.print(table)


# --------------------------------------------------------------------------- #
# models: MCP and the agent
# --------------------------------------------------------------------------- #


def _service(settings: Settings) -> Any:
    from .service import LocalService, RemoteService

    remote = settings.remote()
    if remote is not None:
        return RemoteService(remote, settings.filters)
    return LocalService(settings.fabric(), settings.timeout)


@main.command("mcp")
@click.pass_obj
def mcp_command(settings: Settings) -> None:
    """Serve fcli's tools over MCP on stdio, for an MCP client to start

    With --server it answers from that running `fcling serve` - live state and
    history; without, from the nodes one-shot. `fcling serve` itself serves the same
    tools at /mcp, for clients that connect over HTTP.
    """
    try:
        from .mcp import serve_stdio
    except ImportError as exc:
        raise _broken("MCP", exc) from exc
    asyncio.run(serve_stdio(_service(settings), __version__))


def _show(event: dict[str, Any], console: Console) -> bool:
    """One agent event: the answer to stdout as it streams, what it does to stderr.
    Whether it was an error."""
    kind = event.get("type")
    if kind == "text":
        console.print(event["text"], end="", markup=False, highlight=False, soft_wrap=True)
    elif kind == "tool":
        args = json.dumps(event.get("input") or {}, separators=(",", ":"))
        err.print(f"[dim]-> {event['name']} {args[:160]}[/]", highlight=False)
    elif kind == "tool_result" and event.get("error"):
        err.print(f"[dim yellow]   {event['name']} failed[/]", highlight=False)
    elif kind == "error":
        err.print(f"[red]{event['message']}[/]", highlight=False)
        return True
    elif kind == "done":
        console.print()
        u = event.get("usage") or {}
        err.print(
            f"[dim]{event.get('rounds', 0)} tool rounds; tokens in {u.get('input', 0)}"
            f" (cached {u.get('cache_read', 0)}), out {u.get('output', 0)}[/]",
            highlight=False,
        )
    return False


@main.command()
@click.argument("question", required=False)
@click.option("-C", "--conversation", help="continue this conversation (with --server)")
@click.option(
    "--provider",
    envvar="FCLI_LLM_PROVIDER",
    type=click.Choice(["claude", "openai", "grok"]),
    help="whose model [default: the first with a key, Claude first]",
)
@click.option("--model", envvar="FCLI_MODEL", help="the model [default: the provider's]")
@click.option(
    "--effort",
    envvar="FCLI_EFFORT",
    help="how hard it reasons, as the provider names it: low, medium, high...",
)
@click.pass_obj
def ask(
    settings: Settings,
    question: str | None,
    conversation: str | None,
    provider: str | None,
    model: str | None,
    effort: str | None,
) -> None:
    """Ask the agent about the fabric; without a question, keep asking

    It reads the fabric with the same read-only tools MCP clients get. With
    --server it runs inside that server, with its live state and history.
    Needs a model provider's key: ANTHROPIC_API_KEY (or `ant auth login`),
    OPENAI_API_KEY (and OPENAI_BASE_URL for a compatible proxy) or XAI_API_KEY.
    """
    console = Console(emoji=False)
    remote = settings.remote()
    failed = False
    if remote is not None:
        if provider or model or effort:
            raise click.UsageError("with --server the server's provider, model and effort apply")

        def turn(q: str) -> None:
            nonlocal conversation, failed
            try:
                for event in remote.chat(q, conversation):
                    if event.get("type") == "start":
                        conversation = event["conversation"]
                    failed = _show(event, console) or failed
            except RemoteError as exc:
                raise click.ClickException(str(exc)) from exc
    else:
        if conversation:
            raise click.UsageError("--conversation continues a conversation a server keeps: use --server")
        try:
            from .agent import Agent, Conversation
        except ImportError as exc:
            raise _broken("the agent", exc) from exc
        from .llm import ProviderError

        service = _service(settings)
        try:
            agent = Agent(
                service, fabric=settings.fabric().name, provider=provider, model=model, effort=effort
            )
        except ProviderError as exc:
            raise click.UsageError(str(exc)) from exc
        local = Conversation()

        async def run(q: str) -> None:
            nonlocal failed
            async for event in agent.turn(local, q):
                failed = _show(event, console) or failed

        def turn(q: str) -> None:
            asyncio.run(run(q))

    if question:
        turn(question)
        if conversation:
            err.print(f"[dim]continue with: fcling ask -C {conversation} ...[/]", highlight=False)
        if failed:
            sys.exit(1)
        return
    while True:
        try:
            q = console.input("[bold]> [/]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if q:
            turn(q)


# --------------------------------------------------------------------------- #
# lenses and the topology
# --------------------------------------------------------------------------- #


def _lens_answer(settings: Settings, name: str, args: dict[str, Any], at: str | None) -> dict[str, Any]:
    """A lens's records: from the server, from the local history at *at*, or from the nodes."""
    from .lenses import LENSES
    from .service import ServiceError

    if settings.remote() is None and at is not None:
        spec = LENSES[name]
        store = _local_store(settings)
        try:
            fabric = views.fabric_at(store, settings.fabric().nodes, parse_time(at), spec.requires)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--at") from exc
        finally:
            store.close()
        from .lens import LensError

        try:
            return {"records": [r.model_dump(mode="json") for r in spec.run(fabric, **args)]}
        except LensError as exc:
            raise click.UsageError(str(exc)) from exc
    try:
        return asyncio.run(_service(settings).lens(name, args, at))  # type: ignore[no-any-return]
    except ServiceError as exc:
        raise click.UsageError(str(exc)) from exc


def _lens_command(spec: Any) -> click.Command:
    @_FIELD_FILTER
    @_AT
    @click.pass_obj
    def run(settings: Settings, field_filters: tuple[str, ...], at: str | None, **args: Any) -> None:
        try:
            filters = render.parse_filters(spec, field_filters)
        except render.FilterError as exc:
            raise click.UsageError(str(exc)) from exc
        answer = _lens_answer(settings, spec.name, args, at)
        records = [spec.record.model_validate(r) for r in answer["records"]]
        records = [r for r in records if render.keep(spec, r, filters)]
        if settings.output == "table":
            render.table(spec, records, Console(emoji=False), settings.box)
        elif settings.output == "json":
            click.echo(render.as_json(records, spec))
        elif settings.output == "yaml":
            click.echo(render.as_yaml(records, spec), nl=False)
        else:
            click.echo(render.as_csv(spec, records), nl=False)

    # Click lists the parameters in the reverse of the order they are added.
    for arg in reversed(spec.args):
        if arg.required or arg.positional:
            metavar = arg.name.upper() if arg.required else f"[{arg.name.upper()}]"
            run = click.argument(arg.name.upper().replace("-", "_"), metavar=metavar, required=arg.required)(
                run
            )
        else:
            run = click.option(f"--{arg.name}", arg.name, help=arg.help)(run)
    args = " ".join(
        a.name.upper() if a.required else f"[{a.name.upper()}]" if a.positional else f"[--{a.name}]"
        for a in spec.args
    )
    return click.command(spec.name, help=f"{spec.help}\n\n{args}" if args else spec.help)(run)


def _add_lenses() -> None:
    from .lenses import LENSES

    for spec in LENSES.values():
        main.add_command(_lens_command(spec))


_add_lenses()


@main.command()
@_AT
@click.pass_obj
def topology(settings: Settings, at: str | None) -> None:
    """The fabric as a graph: tiers, cables, clients, segments, health"""
    from .service import ServiceError

    try:
        if settings.remote() is None and at is not None:
            from .topology import REQUIRES, build

            store = _local_store(settings)
            try:
                fabric = views.fabric_at(
                    store, settings.fabric().nodes, parse_time(at), sorted({*REQUIRES, *required()})
                )
            finally:
                store.close()
            graph = build(fabric, run_checks(fabric), settings.fabric().name)
        else:
            graph = asyncio.run(_service(settings).topology(at))
    except (ServiceError, ValueError) as exc:
        raise click.UsageError(str(exc)) from exc
    if _dump(graph, settings.output):
        return
    _print_topology(graph, Console(emoji=False))


_HEALTH_MARK = {"error": "[bold red]![/]", "warning": "[yellow]~[/]", "ok": ""}


def _print_topology(graph: dict[str, Any], console: Console) -> None:
    from rich.text import Text

    nodes = {n["name"]: n for n in graph["nodes"]}
    for component in graph["components"]:
        members = component["nodes"]
        links = [x for x in graph["links"] if x["a"] in members]
        clients = [c for c in graph["clients"] if set(c["nodes"]) & set(members)]
        console.print(
            f"[bold]{component['name']}[/]: {len(members)} nodes, {sum(x['count'] for x in links)} cables, "
            f"{len(clients)} clients",
            highlight=False,
        )
        from .topology import TIERS

        for tier in TIERS:
            names = [n for n in members if nodes[n]["tier"] == tier]
            if names:
                cells = [f"{n}{_HEALTH_MARK.get(nodes[n]['health'], '')}" for n in names]
                console.print(f"  [dim]{tier:13}[/] {'  '.join(cells)}", highlight=False)
        down = [x for x in links if x["state"] not in ("up", "")]
        if down:
            console.print("  [dim]cables not up[/]")
            for x in down:
                for c in x["cables"]:
                    if c["state"] != "up":
                        console.print(
                            Text.assemble(
                                f"    {x['a']} {c['a_port']} <-> {x['b']} {c['b_port']}  ",
                                (c["state"], "red"),
                            )
                        )
        if clients:
            console.print("  [dim]clients[/]")
            for c in clients:
                services = sorted({a["service"] for a in c["attachments"]})
                ports = ", ".join(sorted({f"{a['node']} {a['port']}" for a in c["attachments"]}))
                es = f"  ES {c['esi']}" if c["esi"] else ""
                console.print(Text.assemble(f"    {c['label']}: {ports}{es}  ", (", ".join(services), "dim")))
    segments = graph["segments"]
    if segments:
        console.print("[bold]segments[/]")
        for s in segments:
            where = ", ".join(f"{n['node']} {' '.join(n['interfaces']) or '-'}" for n in s["nodes"])
            nh = f"  next-hop {', '.join(s['next_hops'])}" if s["next_hops"] else ""
            console.print(Text(f"  {'/'.join(s['names']) or s['esi']}  {s['mode']}  {where}{nh}"))
    if graph["outside"]:
        console.print(
            "[bold]outside[/]  "
            + ", ".join(f"{o['name']} ({', '.join(o['nodes'])})" for o in graph["outside"])
        )
