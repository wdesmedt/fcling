"""Talking to a running ``fcling serve``: the standard library only, so the CLI needs no extra."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from typing import Any


class RemoteError(Exception):
    pass


class Remote:
    def __init__(self, url: str, token: str | None = None, timeout: float = 120.0) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(
        self,
        path: str,
        query: Sequence[tuple[str, str]] = (),
        timeout: float | None = None,
        body: bytes | None = None,
    ) -> Any:
        url = f"{self.url}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(list(query))
        request = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            return urllib.request.urlopen(request, timeout=timeout or self.timeout)  # noqa: S310 - the user's URL
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read()).get("error") or exc.reason
            except (ValueError, AttributeError):
                message = exc.reason
            raise RemoteError(f"{self.url}: {message}") from exc
        except urllib.error.URLError as exc:
            raise RemoteError(f"{self.url}: {exc.reason}") from exc

    def get(self, path: str, query: Sequence[tuple[str, str]] = ()) -> Any:
        with self._request(path, query) as response:
            return json.loads(response.read())

    def records(
        self,
        report: str,
        params: Mapping[str, Any],
        filters: Sequence[str] = (),
        at: str | None = None,
    ) -> dict[str, Any]:
        query = [("inv", f) for f in filters]
        query += [(k, str(v)) for k, v in params.items() if v is not None]
        if at:
            query.append(("at", at))
        return self.get(f"/api/reports/{urllib.parse.quote(report)}", query)  # type: ignore[no-any-return]

    def history(self, **query: Any) -> list[dict[str, Any]]:
        pairs = []
        for key, value in query.items():
            for v in value if isinstance(value, list | tuple) else [value]:
                if v is not None:
                    pairs.append((key, str(v)))
        return self.get("/api/history", pairs)  # type: ignore[no-any-return]

    def events(self, query: Sequence[tuple[str, str]] = ()) -> Iterator[dict[str, Any]]:
        """Server-sent events, as they come; status events included."""
        with self._request("/api/events", query, timeout=3600) as response:
            yield from _sse(response)

    def chat(self, question: str, conversation: str | None = None) -> Iterator[dict[str, Any]]:
        """Ask the server's agent: its answer as events, as it is written."""
        body = json.dumps({"question": question, "conversation": conversation}).encode()
        with self._request("/api/chat", body=body, timeout=600) as response:
            yield from _sse(response)


def _sse(response: Any) -> Iterator[dict[str, Any]]:
    data: list[str] = []
    for raw in response:
        line = raw.decode().rstrip("\n").rstrip("\r")
        if line.startswith("data:"):
            data.append(line[5:].strip())
        elif not line and data:
            yield json.loads("\n".join(data))
            data = []
