"""Private loopback TLS and exact process identities; no desktop access."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import sys

import psutil

from windows_app import settings


def certificate():
    return settings.state() / "localhost-cert.pem"


def private_key():
    return settings.state() / "localhost-key.pem"


def prepare_endpoint():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    if not certificate().exists() or not private_key().exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Alex Telegram MCP")])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=True,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        settings.atomic_write(
            private_key(),
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode("ascii"),
        )
        settings.atomic_write(
            certificate(), cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
        )
    config = settings.state() / "service-settings.json"
    if not config.exists():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        settings.atomic_write(config, json.dumps({"port": port}))


def tls_context():
    # Trust only this installation's certificate, never the system CA store.
    return ssl.create_default_context(cafile=str(certificate()))


def identity(pid=None):
    process = psutil.Process(pid or os.getpid())
    return {"pid": process.pid, "created": process.create_time(), "exe": process.exe()}


def matching_process(record):
    try:
        process = psutil.Process(int(record["pid"]))
        if process.create_time() == record["created"] and process.exe() == record["exe"]:
            return process
    except (KeyError, ValueError, psutil.Error):
        pass
    return None


def record_process():
    process = identity()
    parent = identity(os.getppid())
    # Only the PyInstaller bootloader is part of our process lifetime.
    records = [process]
    if getattr(sys, "frozen", False) and parent["exe"] == process["exe"]:
        records.append(parent)
    settings.atomic_write(settings.state() / "backend-process.json", json.dumps(records))
    return records


def recorded_processes():
    path = settings.state() / "backend-process.json"
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
        return [process for item in records if (process := matching_process(item)) is not None]
    except (OSError, ValueError):
        return []


def stop_bridges():
    """Uninstall only: close our stdio transports, leaving client apps open."""
    owner = psutil.Process().username()
    app_root = settings.root().resolve()
    for process in psutil.process_iter():
        try:
            image = Path(process.exe())
            ours = (
                image.parent == app_root
                and (
                    image.name == "TelegramMCP.exe"
                    or image.name.startswith("TelegramMCP.exe.old-")
                )
                and process.username() == owner
                and "--stdio" in process.cmdline()
            )
        except psutil.Error:
            continue
        if ours:
            try:
                record = identity(process.pid)
                matched = matching_process(record)
                if matched:
                    matched.kill()
                    matched.wait(timeout=8)
            except psutil.NoSuchProcess:
                pass
