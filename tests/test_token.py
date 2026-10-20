"""The token of a server beyond localhost: kept across restarts, printed in a URL, found by the CLI."""

from __future__ import annotations

import re
import stat
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from fcli import server
from fcli.cli import Settings, _login_url, main
from fcli.remote import Remote, find_token, keep_token, token_path

INVENTORY = "name: lab\nnodes: {leaf1: {address: 127.0.0.1}}\n"


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state" / "fcli"


def _settings(server_url: str, inventory: Path | None = None, token: str | None = None) -> Settings:
    return Settings(None, inventory, (), {}, "table", "MINIMAL", 30.0, server=server_url, token=token)


def test_the_token_in_a_server_url_is_taken_out_of_it() -> None:
    remote = Remote("http://lab:8765/?token=s3cret")
    assert (remote.url, remote.token) == ("http://lab:8765", "s3cret")
    assert Remote("http://lab:8765/?token=s3cret", "given").token == "given"
    assert Remote("http://lab:8765/api?x=1&token=s3cret").url == "http://lab:8765/api?x=1"
    assert Remote("http://lab:8765/").token is None


def test_a_made_up_token_is_kept_for_its_owner_only(tmp_path: Path) -> None:
    path = token_path(tmp_path / "fcli" / "lab.db")
    assert path == tmp_path / "fcli" / "lab.token"
    token = keep_token(path)
    assert len(token) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert keep_token(path) == token


def test_the_fabric_token_comes_first_then_the_only_one_kept(tmp_path: Path) -> None:
    (tmp_path / "a.token").write_text("aaa\n")
    (tmp_path / "b.token").write_text("bbb\n")
    assert find_token(tmp_path / "b.token", tmp_path.glob("*.token")) == "bbb"
    assert find_token(tmp_path / "none.token", tmp_path.glob("*.token")) is None
    assert find_token(None, [tmp_path / "a.token"]) == "aaa"


def test_the_cli_reads_the_token_a_local_server_keeps(state: Path, tmp_path: Path) -> None:
    inventory = tmp_path / "inv.yml"
    inventory.write_text(INVENTORY)
    state.mkdir(parents=True)
    (state / "lab.token").write_text("lab-token\n")
    (state / "other.token").write_text("other-token\n")
    remote = _settings("http://127.0.0.1:8765", inventory).remote()
    assert remote is not None and remote.token == "lab-token"
    # Given one way or another, it is not looked for.
    assert _settings("http://127.0.0.1:8765", inventory, "mine").remote().token == "mine"  # type: ignore[union-attr]
    assert _settings("http://127.0.0.1:8765/?token=url", inventory).remote().token == "url"  # type: ignore[union-attr]
    # Another machine's server keeps its token there, not here.
    assert _settings("http://elsewhere.example:8765", inventory).remote().token is None  # type: ignore[union-attr]


def test_without_an_inventory_the_only_kept_token_is_used(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    state.mkdir(parents=True)
    (state / "lab.token").write_text("lab-token\n")
    assert _settings("http://localhost:8765").remote().token == "lab-token"  # type: ignore[union-attr]
    (state / "other.token").write_text("other-token\n")
    assert _settings("http://localhost:8765").remote().token is None  # type: ignore[union-attr]


def test_the_login_url_names_a_reachable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: "labhost")
    assert _login_url("127.0.0.1", 8765, None) == "http://127.0.0.1:8765/"
    assert _login_url("0.0.0.0", 8765, "s3cret") == "http://labhost:8765/?token=s3cret"  # noqa: S104
    assert _login_url("fd00::1", 80, None) == "http://[fd00::1]:80/"
    assert _login_url("0.0.0.0", 8765, None, tls=True) == "https://labhost:8765/"  # noqa: S104


def test_serve_beyond_localhost_keeps_its_token_across_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served: list[Any] = []
    monkeypatch.setattr(server, "run", lambda app, host, port, ready=None, tls=None: served.append(app))
    inventory = tmp_path / "inv.yml"
    inventory.write_text(INVENTORY)
    db = tmp_path / "h.db"
    args = ["-c", str(inventory), "serve", "--host", "0.0.0.0", "--db", str(db)]  # noqa: S104

    def started() -> str:
        result = CliRunner().invoke(main, args)
        assert result.exit_code == 0, result.output
        assert f"token kept in {db.with_suffix('.token')}" in " ".join(result.output.split())
        match = re.search(r"\?token=([\w-]+)", result.output)
        assert match, result.output
        return match.group(1)

    first = started()
    assert started() == first
    assert db.with_suffix(".token").read_text().strip() == first
    assert len(served) == 2
    # A token of one's own is used, not written down.
    db.with_suffix(".token").unlink()
    result = CliRunner().invoke(main, [*args, "--api-token", "mine"])
    assert result.exit_code == 0, result.output
    assert "?token=mine" in result.output
    assert not db.with_suffix(".token").exists()
