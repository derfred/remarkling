"""Settings read from the environment (a Kubernetes Secret / ConfigMap in production)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # reMarkable
    rm_transport: str = field(default_factory=lambda: _env("RM_TRANSPORT", "cloud"))
    rm_device_token: str = field(default_factory=lambda: _env("RM_DEVICE_TOKEN"))
    rm_auth_host: str = field(
        default_factory=lambda: _env("RM_AUTH_HOST", "https://webapp-prod.cloud.remarkable.engineering")
    )
    rm_sync_host: str = field(default_factory=lambda: _env("RM_SYNC_HOST", "https://internal.cloud.remarkable.com"))
    rm_ssh_host: str = field(default_factory=lambda: _env("RM_SSH_HOST", "10.11.99.1"))
    rm_ssh_key: str = field(default_factory=lambda: _env("RM_SSH_KEY"))
    # Width of a page in .rm coordinate units; strokes are mapped onto the PDF width with it.
    # 1404 on reMarkable 2; calibrate on the Paper Pro during M0.
    rm_page_width_units: float = field(default_factory=lambda: float(_env("RM_PAGE_WIDTH_UNITS", "1404")))
    inbox_folder: str = field(default_factory=lambda: _env("RM_INBOX", "Inbox"))
    outbox_folder: str = field(default_factory=lambda: _env("RM_OUTBOX", "Outbox"))
    archive_folder: str = field(default_factory=lambda: _env("RM_ARCHIVE", "Archive"))

    # Claude
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    interpret_model: str = field(default_factory=lambda: _env("INTERPRET_MODEL", "claude-sonnet-5-5"))

    # Trilium
    trilium_url: str = field(default_factory=lambda: _env("TRILIUM_URL").rstrip("/"))
    trilium_public_url: str = field(default_factory=lambda: _env("TRILIUM_PUBLIC_URL").rstrip("/"))
    trilium_etapi_token: str = field(default_factory=lambda: _env("TRILIUM_ETAPI_TOKEN"))
    trilium_parent_note: str = field(default_factory=lambda: _env("TRILIUM_PARENT_NOTE"))

    # Service
    api_token: str = field(default_factory=lambda: _env("API_TOKEN"))
    mcp_token: str = field(default_factory=lambda: _env("MCP_TOKEN"))
    mcp_allowed_hosts: list[str] = field(
        default_factory=lambda: [h for h in _env("MCP_ALLOWED_HOSTS").split(",") if h]
    )
    poll_interval_s: int = field(default_factory=lambda: int(_env("POLL_INTERVAL_S", "300")))
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")))
    # postgres://user:pass@host/db to keep document state in Postgres; empty means SQLite in DATA_DIR.
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL"))
    retention_days: int = field(default_factory=lambda: int(_env("RETENTION_DAYS", "30")))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "inkbridge.sqlite3"

    @property
    def trilium_enabled(self) -> bool:
        return bool(self.trilium_url and self.trilium_etapi_token and self.trilium_parent_note)


@lru_cache
def get_settings() -> Settings:
    return Settings()
