"""A gNMI target in-process, answering the way SR Linux does.

It serves a fixed set of payloads by path. A Get answers each path it knows
with one update at ``/`` holding the payload, prefixes and all - which is how
SR Linux answers a wildcard Get - and rejects the whole request with
NOT_FOUND when any path is outside its "schema".

The server runs on a loop of its own in a thread, so a test can drive it with
the synchronous CLI as well as from async code.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import grpc
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from fcli.gnmi import path as gp
from fcli.gnmi.proto import gnmi_pb2, gnmi_pb2_grpc

USERNAME, PASSWORD = "admin", "secret"


HEARTBEAT = "system/information/current-datetime"


def _update(path: str, value: Any) -> gnmi_pb2.Update:
    return gnmi_pb2.Update(
        path=gp.to_proto(path), val=gnmi_pb2.TypedValue(json_ietf_val=json.dumps(value).encode())
    )


class _Servicer(gnmi_pb2_grpc.gNMIServicer):
    def __init__(self, data: Mapping[str, Any], unknown: frozenset[str] = frozenset()) -> None:
        self.data = {gp.schema(gp.parse(p)): v for p, v in data.items()}
        #: Elements this target's schema lacks: a Subscribe naming one is refused whole.
        self.unknown = unknown
        self.gets: list[list[str]] = []
        self.subscribes: list[list[str]] = []
        self.streams: list[asyncio.Queue[gnmi_pb2.Notification]] = []

    async def _auth(self, context: grpc.aio.ServicerContext) -> None:
        md = dict(context.invocation_metadata() or ())
        if (md.get("username"), md.get("password")) != (USERNAME, PASSWORD):
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "bad credentials")

    async def Capabilities(self, request, context):  # noqa: N802
        await self._auth(context)
        return gnmi_pb2.CapabilityResponse(
            gNMI_version="0.10.0",
            supported_encodings=[gnmi_pb2.JSON_IETF, 52],
            supported_models=[
                gnmi_pb2.ModelData(name="urn:srl_nokia/bgp:srl_nokia-bgp", version="2026-07-31")
            ],
        )

    async def Get(self, request, context):  # noqa: N802
        await self._auth(context)
        paths = [gp.schema(gp.from_proto(p)) for p in request.path]
        self.gets.append(paths)
        missing = [p for p in paths if p not in self.data]
        if missing:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"unknown path {missing[0]}")
        return gnmi_pb2.GetResponse(
            notification=[
                gnmi_pb2.Notification(
                    timestamp=1,
                    update=[
                        gnmi_pb2.Update(
                            path=gnmi_pb2.Path(),
                            val=gnmi_pb2.TypedValue(json_ietf_val=json.dumps(self.data[p]).encode()),
                        )
                    ],
                )
                for p in paths
            ]
        )

    async def Subscribe(self, request_iterator, context):  # noqa: N802
        await self._auth(context)
        request = await anext(request_iterator)
        paths = [gp.from_proto(s.path) for s in request.subscribe.subscription]
        self.subscribes.append([gp.schema(p) for p in paths])
        for path in paths:
            for elem in path:
                if elem.name in self.unknown:
                    await context.abort(
                        grpc.StatusCode.INVALID_ARGUMENT, f"Path not valid - unknown element '{elem.name}'"
                    )
        for path in paths:
            value = self.data.get(gp.schema(path))
            if value is not None:
                yield gnmi_pb2.SubscribeResponse(
                    update=gnmi_pb2.Notification(timestamp=1, update=[_update("/", value)])
                )
        yield gnmi_pb2.SubscribeResponse(sync_response=True)
        queue: asyncio.Queue[gnmi_pb2.Notification] = asyncio.Queue()
        self.streams.append(queue)
        try:
            while True:
                yield gnmi_pb2.SubscribeResponse(update=await queue.get())
        finally:
            self.streams.remove(queue)


@dataclass
class Certificate:
    cert_pem: bytes
    key_pem: bytes


def self_signed(name: str = "leaf1.lab") -> Certificate:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return Certificate(
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
    )


class FakeTarget:
    def __init__(
        self,
        data: Mapping[str, Any],
        certificate: Certificate | None = None,
        unknown: frozenset[str] = frozenset(),
    ) -> None:
        self.servicer = _Servicer(data, unknown)
        self.certificate = certificate
        self.port = 0
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._server: grpc.aio.Server | None = None

    async def _start(self) -> None:
        self._server = grpc.aio.server()
        gnmi_pb2_grpc.add_gNMIServicer_to_server(self.servicer, self._server)
        if self.certificate:
            creds = grpc.ssl_server_credentials([(self.certificate.key_pem, self.certificate.cert_pem)])
            self.port = self._server.add_secure_port("127.0.0.1:0", creds)
        else:
            self.port = self._server.add_insecure_port("127.0.0.1:0")
        await self._server.start()

    def __enter__(self) -> FakeTarget:
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._start(), self._loop).result(10)
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._server is not None
        asyncio.run_coroutine_threadsafe(self._server.stop(None), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)

    def push(self, updates: Mapping[str, Any] | None = None, deletes: list[str] | None = None) -> None:
        """Send one notification on every open subscription, as an ON_CHANGE stream would."""
        notification = gnmi_pb2.Notification(
            timestamp=2,
            update=[_update(p, v) for p, v in (updates or {}).items()],
            delete=[gp.to_proto(p) for p in deletes or []],
        )

        def send() -> None:
            for queue in self.servicer.streams:
                queue.put_nowait(notification)

        self._loop.call_soon_threadsafe(send)

    @property
    def streaming(self) -> int:
        return len(self.servicer.streams)
