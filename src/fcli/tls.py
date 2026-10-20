"""HTTPS for ``fcling serve`` beyond loopback: a certificate of its own, kept next to the history.

A server reached from other machines sends its token and the fabric's state
over the network, so it speaks TLS there. Without a certificate given, it
makes a self-signed one up once and keeps it, like the token: a restart
presents the same certificate, so a browser's exception and a client's trust
hold. It is made again when it expires or does not name the address listened on.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import os
import socket
from pathlib import Path

#: How long a certificate made up here lasts.
VALIDITY = datetime.timedelta(days=825)
#: Made again this long before it expires.
RENEW_BEFORE = datetime.timedelta(days=30)


def loopback(host: str) -> bool:
    """Whether *host* is reached from this machine only: ``localhost``, 127.0.0.0/8 or ::1."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def cert_path(history: Path) -> Path:
    """Where ``fcling serve`` keeps the certificate it made up, next to its history."""
    return history.with_suffix(".crt")


def key_path(history: Path) -> Path:
    return history.with_suffix(".key")


def names_for(host: str) -> list[str]:
    """What the certificate must name for a server listening on *host*."""
    names = ["localhost", "127.0.0.1", "::1", socket.gethostname(), socket.getfqdn()]
    if host not in ("", "0.0.0.0", "::"):  # noqa: S104 - compared, not bound
        names.append(host)
    return list(dict.fromkeys(n for n in names if n))


def keep_certificate(history: Path, host: str) -> tuple[Path, Path]:
    """The certificate and key kept for *history*; made up and written there if missing or unfit."""
    cert, key = cert_path(history), key_path(history)
    names = names_for(host)
    if not _fit(cert, key, names):
        _make(cert, key, names)
    return cert, key


def cert_names(cert: Path) -> list[str]:
    """The names *cert* is for: its DNS names in order, then its addresses; none if it cannot be read."""
    from cryptography import x509

    try:
        loaded = x509.load_pem_x509_certificate(cert.read_bytes())
        san = loaded.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (OSError, ValueError, x509.ExtensionNotFound):
        return []
    return [*san.get_values_for_type(x509.DNSName), *map(str, san.get_values_for_type(x509.IPAddress))]


def fingerprint(cert: Path) -> str:
    """The SHA-256 fingerprint of the PEM certificate in *cert*, as browsers show it."""
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    der = x509.load_pem_x509_certificate(cert.read_bytes()).public_bytes(Encoding.DER)
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


def _fit(cert: Path, key: Path, names: list[str]) -> bool:
    from cryptography import x509

    if not key.exists():
        return False
    try:
        loaded = x509.load_pem_x509_certificate(cert.read_bytes())
        san = loaded.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (OSError, ValueError, x509.ExtensionNotFound):
        return False
    now = datetime.datetime.now(datetime.UTC)
    if loaded.not_valid_after_utc - RENEW_BEFORE < now:
        return False
    named = set(san.get_values_for_type(x509.DNSName))
    named |= {str(a) for a in san.get_values_for_type(x509.IPAddress)}
    return all(_canonical(n) in named for n in names)


def _canonical(name: str) -> str:
    try:
        return str(ipaddress.ip_address(name))
    except ValueError:
        return name


def _make(cert: Path, key: Path, names: list[str]) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    private = ec.generate_private_key(ec.SECP256R1())
    alternatives: list[x509.GeneralName] = []
    for name in names:
        try:
            alternatives.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            alternatives.append(x509.DNSName(name))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"fcling serve on {socket.gethostname()}")])
    now = datetime.datetime.now(datetime.UTC)
    made = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + VALIDITY)
        .add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
        # Its own trust anchor: what a client given this file as its CA verifies against.
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(private.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(private.public_key()), critical=False
        )
        .sign(private, hashes.SHA256())
    )
    cert.parent.mkdir(parents=True, exist_ok=True)
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    with os.fdopen(os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as out:
        out.write(pem)
    cert.write_bytes(made.public_bytes(serialization.Encoding.PEM))
