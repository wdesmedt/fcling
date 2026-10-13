"""``fcling serve``'s way of running the app, over a fake target: for the shutdown tests to signal."""

from __future__ import annotations

import socket
import sys
import tempfile
from pathlib import Path

from fcli.engine import Engine
from fcli.gnmi.client import Tls
from fcli.inventory import Inventory, Node
from fcli.reports import REGISTRY
from fcli.server import create_app, run
from fcli.store import AsyncStore

from .fake_gnmi import PASSWORD, USERNAME, FakeTarget
from .test_engine import DATA


def main() -> None:
    with FakeTarget(DATA) as target, tempfile.TemporaryDirectory() as tmp:
        node = Node("leaf1", "127.0.0.1", target.port, USERNAME, PASSWORD, Tls(insecure=True))
        store = AsyncStore(Path(tmp) / "h.db")
        engine = Engine((node,), [REGISTRY["bgp-peers"]], store, settle=0.01)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        print(port, flush=True)
        try:
            run(create_app(engine, Inventory("lab", (node,))), "127.0.0.1", port)
        finally:
            store.close()
        print("closed", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
