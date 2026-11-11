from __future__ import annotations

from ...config import Settings
from .base import Cache, RemoteDoc, RmDoc, Transport


def make_transport(settings: Settings, cache: Cache | None = None) -> Transport:
    if settings.rm_transport == "ssh":
        from .ssh import SshTransport

        return SshTransport(settings.rm_ssh_host, settings.rm_ssh_key)
    if settings.rm_transport == "cloud":
        from .cloud import CloudTransport

        return CloudTransport(
            settings.rm_device_token,
            settings.rm_auth_host,
            settings.rm_sync_host,
            cache=cache,
            notifications_url=settings.rm_notifications_url,
        )
    raise ValueError(f"unknown RM_TRANSPORT {settings.rm_transport!r}")


__all__ = ["Cache", "RemoteDoc", "RmDoc", "Transport", "make_transport"]
