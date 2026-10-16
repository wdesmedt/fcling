"""Running the server in the background: detached from the terminal, logging to a file.

The classic double fork: the server ends up in a session of its own, with no
terminal to lose, and its stdout and stderr - logging, uvicorn, a traceback -
written to the log file. The command that started it waits until the server
listens, so a port in use is told on the terminal, not found in the log later.

POSIX only: there is no ``fork`` on Windows.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import select
import sys
from collections.abc import Callable
from pathlib import Path

#: Seconds the starting command waits for the server to listen.
START_WAIT = 60.0


class DaemonError(Exception):
    pass


def running(pid_file: Path) -> int | None:
    """The pid in *pid_file*, if that process is still alive."""
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    return pid


def _tail(log: Path, lines: int = 15) -> str:
    try:
        return "\n".join(log.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def detach(log: Path, pid_file: Path) -> Callable[[], None]:
    """Go to the background; in the caller, return only in the detached server.

    The starting process waits for the call of the returned function - the
    server listens - and exits: 0 with the pid and the log on stderr, 1 with
    the end of the log if the server died first.
    """
    if not hasattr(os, "fork"):
        raise DaemonError("--daemon needs fork: not on this platform")
    if (pid := running(pid_file)) is not None:
        raise DaemonError(f"already running as pid {pid} (pid file {pid_file})")
    log.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout.flush()
    sys.stderr.flush()
    read, write = os.pipe()
    child = os.fork()
    if child:
        os.close(write)
        os.waitpid(child, 0)
        _wait(read, log, pid_file)
    # The first child: a session of its own, then a second fork, so the server
    # is not a session leader and can never take a terminal again.
    os.close(read)
    os.setsid()
    if os.fork():
        os._exit(0)

    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(out, 1)
    os.dup2(out, 2)
    os.close(out)
    pid_file.write_text(f"{os.getpid()}\n")
    atexit.register(_remove, pid_file, os.getpid())
    # A line in a file is read later: when it was written matters.
    root = logging.getLogger()
    logging.basicConfig(
        level=root.level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True
    )

    def ready() -> None:
        with contextlib.suppress(OSError):
            os.write(write, b"ok")
            os.close(write)

    return ready


def _wait(read: int, log: Path, pid_file: Path) -> None:
    """In the starting process: wait for the server to listen, say so, and exit."""
    answered = bool(select.select([read], [], [], START_WAIT)[0])
    # The server's end closed without a word: it exited before it listened.
    answer = os.read(read, 16) if answered else b""
    os.close(read)
    if answer == b"ok":
        pid = pid_file.read_text().strip() if pid_file.exists() else "?"
        sys.stderr.write(f"running in the background as pid {pid}, logging to {log}\n")
        sys.stderr.write(f"stop it with: kill {pid}\n")
        os._exit(0)
    if answered:
        sys.stderr.write(f"the server did not start; the end of {log}:\n{_tail(log)}\n")
    else:
        sys.stderr.write(f"the server is not listening after {START_WAIT:g}s; see {log}\n")
    os._exit(1)


def _remove(pid_file: Path, pid: int) -> None:
    with contextlib.suppress(OSError, ValueError):
        if int(pid_file.read_text().strip()) == pid:
            pid_file.unlink()
