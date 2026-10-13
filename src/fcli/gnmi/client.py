"""An asyncio gNMI client on ``grpc.aio``.

One :class:`GnmiClient` is one channel to one target. It speaks the three RPCs
fcli needs - Capabilities, Get and Subscribe - and hands back
:class:`Notification` values with decoded payloads and parsed paths, so
nothing past this module sees protobuf.

TLS comes in three flavours (:class:`Tls`): verified against a CA, trusted on
first use (``skip_verify``: the certificate the target presents is fetched and
made the trust anchor, which is all a containerlab node with a self-signed
certificate can offer), and plaintext.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import json
import logging
import socket
import ssl
import time
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path as FilePath
from typing import Any

import grpc
from cryptography import x509
from cryptography.x509.oid import NameOID

from . import path as gpath
from .path import Path
from .proto import gnmi_pb2, gnmi_pb2_grpc

logger = logging.getLogger(__name__)

DEFAULT_PORT = 57400

#: How long a target has to accept a TCP connection before it is reported as
#: not responding. Without it an address that drops packets takes the
#: operating system's connect timeout - minutes - to fail.
CONNECT_TIMEOUT = 10.0

_CHANNEL_OPTIONS: list[tuple[str, Any]] = [
    ("grpc.max_receive_message_length", -1),
    # gRPC backs off reconnects up to two minutes by default, so a node that
    # rebooted reads as gone for far longer than it was down.
    ("grpc.max_reconnect_backoff_ms", 10_000),
]


class DataType(StrEnum):
    ALL = "all"
    CONFIG = "config"
    STATE = "state"
    OPERATIONAL = "operational"


class SubscriptionMode(StrEnum):
    ON_CHANGE = "on_change"
    SAMPLE = "sample"
    TARGET_DEFINED = "target_defined"


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


class GnmiError(Exception):
    """Something went wrong talking to a target, in words about the target."""


class Unreachable(GnmiError):
    """Nothing answered on the gNMI port, or it refused the connection."""


class TlsFailed(GnmiError):
    """The target answered, and TLS could not be agreed on."""


class AuthFailed(GnmiError):
    """The target rejected the credentials."""


class RpcFailed(GnmiError):
    """The target answered an RPC with an error status."""

    def __init__(self, message: str, code: grpc.StatusCode | None = None) -> None:
        super().__init__(message)
        self.code = code

    @property
    def path_missing(self) -> bool:
        """Whether the target does not know a path asked for.

        SR Linux answers a Get for a path its schema lacks - a feature this
        release or platform does not have - with NOT_FOUND or INVALID_ARGUMENT
        rather than an empty answer.
        """
        return self.code in (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.INVALID_ARGUMENT)


def _socket_error(cause: BaseException, where: str) -> GnmiError:
    if isinstance(cause, ssl.SSLError):
        return TlsFailed(f"TLS handshake with {where} failed: {cause}")
    if isinstance(cause, ConnectionRefusedError):
        return Unreachable(f"{where} refused the connection - is gNMI enabled and listening there?")
    if isinstance(cause, TimeoutError):
        return Unreachable(f"no answer from {where}")
    if isinstance(cause, socket.gaierror):
        return Unreachable(f"{where.split(' ')[0]} does not resolve ({cause.strerror or cause})")
    if isinstance(cause, OSError):
        reason = cause.strerror or str(cause)
        if cause.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN):
            return Unreachable(f"no route to {where} ({reason})")
        return Unreachable(f"{where}: {reason}")
    return Unreachable(f"{where}: {cause}")


def _rpc_error(exc: grpc.aio.AioRpcError, where: str) -> GnmiError:
    code, details = exc.code(), exc.details() or ""
    if code == grpc.StatusCode.UNAUTHENTICATED:
        return AuthFailed(f"{where} rejected the credentials: {details}")
    if code == grpc.StatusCode.UNAVAILABLE:
        if "handshake" in details.lower() or "ssl" in details.lower() or "certificate" in details.lower():
            return TlsFailed(f"TLS with {where} failed: {details}")
        return Unreachable(f"{where} is unavailable: {details}")
    if code == grpc.StatusCode.DEADLINE_EXCEEDED:
        return Unreachable(f"{where} did not answer in time")
    return RpcFailed(f"{where}: {code.name}: {details}", code)


# --------------------------------------------------------------------------- #
# values
# --------------------------------------------------------------------------- #


def _merge_json_dict(into: dict[str, Any], extra: Mapping[str, Any]) -> None:
    for key, value in extra.items():
        current = into.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _merge_json_dict(current, value)
        else:
            into[key] = value


def decode_json_ietf(raw: bytes) -> Any:
    """Parse gNMI JSON-IETF; some targets append a second JSON document in one value."""
    if not raw:
        return None
    text = raw.decode("utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        if exc.msg != "Extra data":
            raise
    decoder = json.JSONDecoder()
    merged: Any = None
    idx = 0
    while idx < len(text):
        chunk = text[idx:].lstrip()
        if not chunk:
            break
        obj, end = decoder.raw_decode(chunk)
        if merged is None:
            merged = obj
        elif isinstance(merged, dict) and isinstance(obj, dict):
            _merge_json_dict(merged, obj)
        else:
            logger.warning("gNMI JSON had extra non-dict payload; keeping the first value")
            break
        idx += len(text) - len(chunk) + end
    return merged


def decode(value: gnmi_pb2.TypedValue) -> Any:
    """A TypedValue as plain Python: JSON is parsed, scalars are unwrapped."""
    kind = value.WhichOneof("value")
    if kind in ("json_ietf_val", "json_val"):
        raw = getattr(value, kind)
        return decode_json_ietf(raw) if kind == "json_ietf_val" else (json.loads(raw) if raw else None)
    if kind == "leaflist_val":
        return [decode(v) for v in value.leaflist_val.element]
    if kind == "decimal_val":
        return value.decimal_val.digits / 10**value.decimal_val.precision
    if kind == "bytes_val":
        return base64.b64encode(value.bytes_val).decode()
    if kind in ("any_val", "proto_bytes"):
        return None
    if kind is None:
        return None
    return getattr(value, kind)


@dataclass(frozen=True, slots=True)
class Notification:
    """One gNMI notification, paths joined to their prefix and values decoded."""

    timestamp: int
    updates: tuple[tuple[Path, Any], ...] = ()
    deletes: tuple[Path, ...] = ()
    #: Subscribe only: the target has sent everything it had (``sync_response``).
    sync: bool = False

    @classmethod
    def from_proto(cls, n: gnmi_pb2.Notification) -> Notification:
        prefix = gpath.from_proto(n.prefix)
        return cls(
            timestamp=n.timestamp,
            updates=tuple((prefix + gpath.from_proto(u.path), decode(u.val)) for u in n.update),
            deletes=tuple(prefix + gpath.from_proto(d) for d in n.delete),
        )


def _encoding_name(value: int) -> str:
    # Targets advertise vendor encodings the proto has no name for (SR Linux: 52).
    try:
        return str(gnmi_pb2.Encoding.Name(value)).lower()
    except ValueError:
        return str(value)


@dataclass(frozen=True, slots=True)
class Capabilities:
    version: str
    encodings: tuple[str, ...]
    #: ``{model name: version}``
    models: dict[str, str]

    @classmethod
    def from_proto(cls, r: gnmi_pb2.CapabilityResponse) -> Capabilities:
        return cls(
            version=r.gNMI_version,
            encodings=tuple(_encoding_name(e) for e in r.supported_encodings),
            models={m.name: m.version for m in r.supported_models},
        )


@dataclass(frozen=True, slots=True)
class Subscription:
    path: str | Path
    mode: SubscriptionMode = SubscriptionMode.ON_CHANGE
    sample_interval: float = 0.0  # seconds

    def to_proto(self) -> gnmi_pb2.Subscription:
        return gnmi_pb2.Subscription(
            path=gpath.to_proto(self.path),
            mode=gnmi_pb2.SubscriptionMode.Value(self.mode.upper()),
            sample_interval=int(self.sample_interval * 1e9),
        )


# --------------------------------------------------------------------------- #
# the client
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Tls:
    """How the target's certificate is checked."""

    #: Trust whatever certificate the target presents. ``None`` decides by
    #: :attr:`ca_file`: configuring a trust anchor means it should be used.
    skip_verify: bool | None = None
    #: PEM trust anchor; without one a verified connection uses the system roots.
    ca_file: str | None = None
    #: The name to verify the certificate against, when not the address.
    server_name: str | None = None
    #: No TLS at all.
    insecure: bool = False


@dataclass
class Target:
    address: str
    port: int = DEFAULT_PORT
    username: str = ""
    password: str = field(default="", repr=False)
    tls: Tls = field(default_factory=Tls)

    @property
    def skip_verify(self) -> bool:
        if self.tls.skip_verify is not None:
            return self.tls.skip_verify
        return not self.tls.ca_file

    @property
    def where(self) -> str:
        return f"{self.address} on gNMI port {self.port}"


async def fetch_certificate(address: str, port: int, connect_timeout: float = CONNECT_TIMEOUT) -> bytes:
    """The DER certificate *address* presents on *port*, unverified.

    Doubles as the reachability probe: a node that is down, unreachable or not
    listening fails here, quickly and in its own words, rather than as an
    opaque UNAVAILABLE from gRPC much later.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # gRPC servers may refuse a handshake that does not offer HTTP/2.
    ctx.set_alpn_protocols(["h2"])
    where = f"{address} on gNMI port {port}"
    try:
        async with asyncio.timeout(connect_timeout):
            _, writer = await asyncio.open_connection(address, port, ssl=ctx)
    except (OSError, TimeoutError) as exc:
        raise _socket_error(exc, where) from exc
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        writer.close()
    if not der:
        raise TlsFailed(f"{where} presented no certificate")
    return der


def certificate_name(der: bytes) -> str | None:
    """A name the certificate is issued to, for ``ssl_target_name_override``."""
    cert = x509.load_der_x509_certificate(der)
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = san.get_values_for_type(x509.DNSName)
        if names:
            return names[0]
    except x509.ExtensionNotFound:
        pass
    cns = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(cns[0].value) if cns else None


async def _probe(address: str, port: int, connect_timeout: float = CONNECT_TIMEOUT) -> None:
    where = f"{address} on gNMI port {port}"
    try:
        async with asyncio.timeout(connect_timeout):
            _, writer = await asyncio.open_connection(address, port)
    except (OSError, TimeoutError) as exc:
        raise _socket_error(exc, where) from exc
    writer.close()


#: Unary RPCs one client runs at once. SR Linux counts every RPC against a
#: ``session-limit`` of 20 per target, shared with every other client - and
#: a running engine already holds its subscriptions.
MAX_CONCURRENT_RPCS = 4


class GnmiClient:
    """One gNMI channel to one target. Use as ``async with GnmiClient(t) as c:``."""

    def __init__(self, target: Target, rpc_timeout: float = 30.0) -> None:
        self.target = target
        self.timeout = rpc_timeout
        self._rpcs = asyncio.Semaphore(MAX_CONCURRENT_RPCS)
        self._channel: grpc.aio.Channel | None = None
        self._stub: gnmi_pb2_grpc.gNMIStub | None = None
        self.capabilities: Capabilities | None = None

    async def __aenter__(self) -> GnmiClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def _metadata(self) -> list[tuple[str, str]]:
        t = self.target
        return [("username", t.username), ("password", t.password)] if t.username else []

    async def connect(self) -> Capabilities:
        """Open the channel and ask for capabilities, which proves it works."""
        t = self.target
        address = f"{t.address}:{t.port}"
        options = list(_CHANNEL_OPTIONS)
        started = time.perf_counter()
        if t.tls.insecure:
            await _probe(t.address, t.port)
            self._channel = grpc.aio.insecure_channel(address, options=options)
        else:
            roots: bytes | None
            if t.skip_verify:
                der = await fetch_certificate(t.address, t.port)
                roots = ssl.DER_cert_to_PEM_cert(der).encode()
                server_name = t.tls.server_name or certificate_name(der)
            else:
                await _probe(t.address, t.port)
                roots = None
                if t.tls.ca_file:
                    roots = await asyncio.to_thread(FilePath(t.tls.ca_file).read_bytes)
                server_name = t.tls.server_name
            if server_name:
                options.append(("grpc.ssl_target_name_override", server_name))
            creds = grpc.ssl_channel_credentials(root_certificates=roots)
            self._channel = grpc.aio.secure_channel(address, creds, options=options)
        self._stub = gnmi_pb2_grpc.gNMIStub(self._channel)
        self.capabilities = await self.get_capabilities()
        logger.debug(
            "%s: connected in %.3fs, gNMI %s, %d models",
            t.address,
            time.perf_counter() - started,
            self.capabilities.version,
            len(self.capabilities.models),
        )
        return self.capabilities

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    @property
    def stub(self) -> gnmi_pb2_grpc.gNMIStub:
        if self._stub is None:
            raise GnmiError(f"{self.target.where}: not connected")
        return self._stub

    async def get_capabilities(self) -> Capabilities:
        try:
            resp = await self.stub.Capabilities(
                gnmi_pb2.CapabilityRequest(), metadata=self._metadata, timeout=self.timeout
            )
        except grpc.aio.AioRpcError as exc:
            raise _rpc_error(exc, self.target.where) from exc
        return Capabilities.from_proto(resp)

    async def get(
        self,
        paths: Sequence[str | Path],
        datatype: DataType = DataType.ALL,
        encoding: str = "json_ietf",
    ) -> list[Notification]:
        request = gnmi_pb2.GetRequest(
            path=[gpath.to_proto(p) for p in paths],
            type=gnmi_pb2.GetRequest.DataType.Value(DataType(datatype).upper()),
            encoding=gnmi_pb2.Encoding.Value(encoding.upper()),
        )
        started = time.perf_counter()
        try:
            async with self._rpcs:
                resp = await self.stub.Get(request, metadata=self._metadata, timeout=self.timeout)
        except grpc.aio.AioRpcError as exc:
            raise _rpc_error(exc, self.target.where) from exc
        notifications = [Notification.from_proto(n) for n in resp.notification]
        logger.debug(
            "%s: Get %d path(s) (%s): %d notification(s) in %.3fs",
            self.target.address,
            len(paths),
            datatype,
            len(notifications),
            time.perf_counter() - started,
        )
        return notifications

    async def subscribe(
        self,
        subscriptions: Iterable[Subscription],
        mode: str = "stream",
        encoding: str = "json_ietf",
    ) -> AsyncIterator[Notification]:
        """Stream notifications; a ``Notification(sync=True)`` marks the initial sync.

        With ``mode="once"`` the stream ends after the sync.
        """
        request = gnmi_pb2.SubscribeRequest(
            subscribe=gnmi_pb2.SubscriptionList(
                subscription=[s.to_proto() for s in subscriptions],
                mode=gnmi_pb2.SubscriptionList.Mode.Value(mode.upper()),
                encoding=gnmi_pb2.Encoding.Value(encoding.upper()),
            )
        )

        # Written on the call rather than from a request iterator: an iterator
        # left waiting when the call is cancelled is a task grpc never reaps.
        call = self.stub.Subscribe(metadata=self._metadata)
        try:
            await call.write(request)
            if mode != "stream":
                await call.done_writing()
            while True:
                resp = await call.read()
                if resp is grpc.aio.EOF:
                    return
                which = resp.WhichOneof("response")
                if which == "update":
                    yield Notification.from_proto(resp.update)
                elif which == "sync_response":
                    yield Notification(timestamp=time.time_ns(), sync=True)
                    if mode == "once":
                        return
        except grpc.aio.AioRpcError as exc:
            raise _rpc_error(exc, self.target.where) from exc
        finally:
            call.cancel()
