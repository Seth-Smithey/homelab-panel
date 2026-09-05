"""Collector registry — order here is the order panels appear on the board."""

from .authentik import AuthentikCollector
from .backups import BackupCollector
from .base import Collector
from .certificates import CertificateCollector
from .cloudflare import CloudflareCollector
from .dns import DnsCollector
from .fleet import FleetCollector
from .host_metrics import HostMetricsCollector
from .jellyfin import JellyfinCollector
from .pihole import PiholeCollector
from .proxmox import ProxmoxCollector
from .services import ServiceCollector
from .splunk import SplunkCollector
from .unifi import UnifiCollector
from .updates import UpdateCollector
from .ups import UpsCollector
from .wan import WanCollector
from .wazuh import WazuhCollector

REGISTRY: list[type[Collector]] = [
    ProxmoxCollector,
    UnifiCollector,
    WanCollector,
    FleetCollector,
    ServiceCollector,
    BackupCollector,
    WazuhCollector,
    SplunkCollector,
    UpsCollector,
    AuthentikCollector,
    PiholeCollector,
    DnsCollector,
    CloudflareCollector,
    CertificateCollector,
    JellyfinCollector,
    HostMetricsCollector,
    UpdateCollector,
]

__all__ = ["REGISTRY", "Collector"]
