"""The pid file of a server in the background. Detaching itself forks, and is tried by hand."""

from __future__ import annotations

import os
from pathlib import Path

from fcli.daemon import _remove, running


def test_a_pid_file_names_a_running_server_only_while_it_runs(tmp_path: Path) -> None:
    pid_file = tmp_path / "fabric.pid"
    assert running(pid_file) is None
    pid_file.write_text(f"{os.getpid()}\n")
    assert running(pid_file) == os.getpid()
    pid_file.write_text("999999999\n")
    assert running(pid_file) is None
    pid_file.write_text("garbage")
    assert running(pid_file) is None


def test_a_server_removes_only_its_own_pid_file(tmp_path: Path) -> None:
    pid_file = tmp_path / "fabric.pid"
    pid_file.write_text("12345\n")
    _remove(pid_file, 54321)
    assert pid_file.exists()
    _remove(pid_file, 12345)
    assert not pid_file.exists()
