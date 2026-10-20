"""HTTPS beyond loopback: a certificate kept next to the history, served, and trusted by the CLI."""

from __future__ import annotations

import socket
import stat
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from fcli import server
from fcli.cli import Settings, main
from fcli.remote import Remote, RemoteError
from fcli.tls import cert_path, fingerprint, keep_certificate, key_path, loopback

INVENTORY = "name: lab\nnodes: {leaf1: {address: 127.0.0.1}}\n"


def test_loopback_is_127_8_and_localhost() -> None:
    assert all(loopback(h) for h in ("127.0.0.1", "127.1.2.3", "::1", "[::1]", "localhost"))
    assert not any(loopback(h) for h in ("0.0.0.0", "::", "10.0.0.1", "labhost", ""))  # noqa: S104


def test_a_made_up_certificate_is_kept_and_reused(tmp_path: Path) -> None:
    history = tmp_path / "fcli" / "lab.db"
    cert, key = keep_certificate(history, "0.0.0.0")  # noqa: S104
    assert (cert, key) == (cert_path(history), key_path(history))
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    first = fingerprint(cert)
    assert keep_certificate(history, "0.0.0.0") == (cert, key)  # noqa: S104
    assert fingerprint(cert) == first
    # Listening on an address it does not name: made again, naming it.
    keep_certificate(history, "192.0.2.7")
    assert fingerprint(cert) != first


def _serve(tmp_path: Path) -> tuple[Any, threading.Thread, int, Path]:
    import uvicorn

    app = Starlette(routes=[Route("/api/status", lambda request: JSONResponse({"ok": True}))])
    cert, key = keep_certificate(tmp_path / "lab.db", "127.0.0.1")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ssl_certfile=cert, ssl_keyfile=key
    )
    running = uvicorn.Server(config)
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not running.started and time.monotonic() < deadline:
        time.sleep(0.05)
    return running, thread, port, cert


def test_the_client_verifies_the_kept_certificate(tmp_path: Path) -> None:
    running, thread, port, cert = _serve(tmp_path)
    try:
        url = f"https://127.0.0.1:{port}"
        assert Remote(url, ca=cert).get("/api/status") == {"ok": True}
        # Not in the system's trust store: refused without it.
        with pytest.raises(RemoteError, match="CERTIFICATE_VERIFY_FAILED"):
            Remote(url).get("/api/status")
    finally:
        running.should_exit = True
        thread.join(10)


def test_the_cli_finds_the_certificate_a_local_server_keeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    inventory = tmp_path / "inv.yml"
    inventory.write_text(INVENTORY)
    state = tmp_path / "state" / "fcli"
    keep_certificate(state / "lab.db", "0.0.0.0")  # noqa: S104

    def remote(url: str, ca: Path | None = None) -> Remote:
        settings = Settings(None, inventory, (), {}, "table", "MINIMAL", 30.0, server=url, server_ca=ca)
        found = settings.remote()
        assert found is not None
        return found

    assert remote("https://127.0.0.1:8765").ca == state / "lab.crt"
    assert remote("https://127.0.0.1:8765", tmp_path / "mine.pem").ca == tmp_path / "mine.pem"
    assert remote("http://127.0.0.1:8765").ca is None
    assert remote("https://elsewhere.example:8765").ca is None


@pytest.mark.parametrize(
    ("args", "tls"),
    [
        (["--host", "127.0.0.1"], False),
        (["--host", "127.0.0.2"], False),
        (["--host", "0.0.0.0"], True),  # noqa: S104
        (["--host", "0.0.0.0", "--no-tls"], False),  # noqa: S104
    ],
)
def test_serve_uses_tls_beyond_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], tls: bool
) -> None:
    given: list[Any] = []
    monkeypatch.setattr(server, "run", lambda app, host, port, ready=None, tls=None: given.append(tls))
    inventory = tmp_path / "inv.yml"
    inventory.write_text(INVENTORY)
    db = tmp_path / "h.db"
    result = CliRunner().invoke(main, ["-c", str(inventory), "serve", "--db", str(db), *args])
    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    if tls:
        assert given == [(db.with_suffix(".crt"), db.with_suffix(".key"))]
        assert "UI at https://" in output and f"SHA-256 {fingerprint(db.with_suffix('.crt'))}" in output
    else:
        assert given == [None]
        assert "UI at http://" in output and not db.with_suffix(".crt").exists()


def test_serve_uses_a_certificate_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    given: list[Any] = []
    monkeypatch.setattr(server, "run", lambda app, host, port, ready=None, tls=None: given.append(tls))
    inventory = tmp_path / "inv.yml"
    inventory.write_text(INVENTORY)
    cert, key = keep_certificate(tmp_path / "mine.db", "127.0.0.1")
    base = ["-c", str(inventory), "serve", "--db", str(tmp_path / "h.db")]
    result = CliRunner().invoke(main, [*base, "--tls-cert", str(cert), "--tls-key", str(key)])
    assert result.exit_code == 0, result.output
    assert given == [(cert, key)] and "UI at https://127.0.0.1" in result.output
    assert not (tmp_path / "h.crt").exists()
    result = CliRunner().invoke(main, [*base, "--tls-cert", str(cert)])
    assert result.exit_code != 0 and "go together" in result.output
