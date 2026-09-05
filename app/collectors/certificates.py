"""TLS certificates — days remaining on anything you publish.

Validation is deliberately turned off while fetching: a self-signed internal
cert should still report its expiry rather than blow up the whole check.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import socket
import ssl

from cryptography import x509
from cryptography.x509.oid import NameOID

from ..models import CRITICAL, OK, UNKNOWN, WARNING, Check, Panel
from .base import Collector


def _fetch_der(host: str, port: int, timeout: float = 6.0) -> bytes:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # Verification is off ON PURPOSE: the point is to read the certificate
    # and report on it — including one that is expired, self-signed or for
    # the wrong name — not to trust the connection. Nothing is sent over it.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection((host, port), timeout=timeout) as sock,
        ctx.wrap_socket(sock, server_hostname=host) as tls,
    ):
        der = tls.getpeercert(binary_form=True)
    if not der:
        raise ValueError("server presented no certificate")
    return der


def _issuer_name(cert: x509.Certificate) -> str:
    for oid in (NameOID.ORGANIZATION_NAME, NameOID.COMMON_NAME):
        values = cert.issuer.get_attributes_for_oid(oid)
        if values:
            return str(values[0].value)
    return "unknown issuer"


class CertificateCollector(Collector):
    key = "certificates"
    title = "Certificates"

    async def collect(self) -> Panel:
        panel = Panel(key=self.key, title=self.title)
        warn_days = int(self.opt("warn_days", 21))
        crit_days = int(self.opt("crit_days", 7))
        hosts = self.opt("hosts", [])
        if not hosts:
            panel.error = "No certificate hosts configured"
            return panel

        for entry in hosts:
            host, _, port_s = str(entry).partition(":")
            try:
                port = int(port_s or 443)
            except ValueError:
                panel.checks.append(
                    Check(
                        id=f"cert.{entry}",
                        name=str(entry),
                        severity=UNKNOWN,
                        value="bad port",
                        detail=f"'{port_s}' is not a port number — use hostname or hostname:port",
                        group="Certificates",
                    )
                )
                continue
            try:
                der = await asyncio.to_thread(_fetch_der, host, port)
                cert = x509.load_der_x509_certificate(der)
                not_after = cert.not_valid_after_utc
                days = (not_after - dt.datetime.now(dt.UTC)).days
                sev = CRITICAL if days <= crit_days else WARNING if days <= warn_days else OK
                panel.checks.append(
                    Check(
                        id=f"cert.{host}",
                        name=host,
                        severity=sev,
                        value=f"{days} days left",
                        detail=f"{_issuer_name(cert)} · expires {not_after:%d %b %Y}",
                        group="TLS",
                        metric=float(days),
                        metric_unit="d",
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad host shouldn't hide the rest
                panel.checks.append(
                    Check(
                        id=f"cert.{host}",
                        name=host,
                        severity=WARNING,
                        value="no certificate",
                        detail=f"{type(exc).__name__}: {str(exc)[:80]}",
                        group="TLS",
                    )
                )

        soonest = min((c.metric for c in panel.checks if c.metric is not None), default=None)
        panel.summary = (
            f"nearest expiry in {int(soonest)} days" if soonest is not None else "no readings"
        )
        return panel
