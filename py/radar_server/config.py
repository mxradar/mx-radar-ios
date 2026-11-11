from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Config:
    """Runtime configuration for the self-hosted radar service."""

    http_host: str = "0.0.0.0"
    http_port: int = 8000
    socks_host: str = "0.0.0.0"
    socks_port: int = 1080
    udp_port_start: int = 20000
    udp_port_end: int = 29999

    # Address advertised to clients in /api/login and /api/config.
    # Leave empty to advertise the address the client connected to (best effort).
    public_host: str = ""

    # Radar page URL. Empty means http://<public_host>:<http_port>
    radar_url: str = ""

    db_path: str = str(SERVER_ROOT / "data" / "radar.db")
    parser_exe: str = ""
    parser_assets: str = ""
    parser_models: str = ""
    parser_port: int = 17891
    parser_output: str = str(SERVER_ROOT / "data" / "parser")

    announce: str = "自建雷达服务"
    room_lease_seconds: int = 75

    # Web admin (/admin/). Empty admin_password disables the admin panel.
    admin_user: str = "admin"
    admin_password: str = ""
    admin_secret: str = ""
    max_udp_flows: int = 1024
    max_bank_candidates: int = 64
    capture_raw_udp: bool = False

    # Per-room event logs: data/parser/rooms/<account-slug>/events.jsonl
    room_log_dir: str = ""
    room_log_max_bytes: int = 500 * 1024 * 1024
    room_log_keep: int = 3

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        cfg = cls()
        if path is None:
            path = os.environ.get("RADAR_CONFIG", str(SERVER_ROOT / "config.json"))
        path = Path(path)
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            for key, value in payload.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
        # Environment overrides
        cfg.http_host = _env("RADAR_HTTP_HOST", cfg.http_host)
        cfg.http_port = int(_env("RADAR_HTTP_PORT", str(cfg.http_port)))
        cfg.socks_host = _env("RADAR_SOCKS_HOST", cfg.socks_host)
        cfg.socks_port = int(_env("RADAR_SOCKS_PORT", str(cfg.socks_port)))
        cfg.udp_port_start = int(_env("RADAR_UDP_PORT_START", str(cfg.udp_port_start)))
        cfg.udp_port_end = int(_env("RADAR_UDP_PORT_END", str(cfg.udp_port_end)))
        if not 1024 <= cfg.udp_port_start <= cfg.udp_port_end <= 65535:
            raise ValueError("UDP port range must be within 1024..65535, start <= end")
        cfg.public_host = _env("RADAR_PUBLIC_HOST", cfg.public_host)
        cfg.radar_url = _env("RADAR_RADAR_URL", cfg.radar_url)
        cfg.db_path = _env("RADAR_DB", cfg.db_path)
        cfg.parser_exe = _env("RADAR_PARSER_EXE", cfg.parser_exe)
        cfg.parser_assets = _env("RADAR_PARSER_ASSETS", cfg.parser_assets)
        cfg.parser_models = _env("RADAR_PARSER_MODELS", cfg.parser_models)
        cfg.parser_port = int(_env("RADAR_PARSER_PORT", str(cfg.parser_port)))
        cfg.parser_output = _env("RADAR_PARSER_OUTPUT", cfg.parser_output)
        cfg.announce = _env("RADAR_ANNOUNCE", cfg.announce)
        cfg.admin_user = _env("RADAR_ADMIN_USER", cfg.admin_user)
        cfg.admin_password = _env("RADAR_ADMIN_PASSWORD", cfg.admin_password)
        cfg.admin_secret = _env("RADAR_ADMIN_SECRET", cfg.admin_secret)
        base = path.resolve().parent if path.is_file() else SERVER_ROOT
        for name in ("db_path", "parser_exe", "parser_assets", "parser_models", "parser_output", "room_log_dir"):
            value = getattr(cfg, name)
            if value and not Path(value).is_absolute():
                setattr(cfg, name, str((base / value).resolve()))
        return cfg

    def advertised_host(self, fallback: str = "127.0.0.1") -> str:
        return self.public_host or fallback

    def service_url(self, fallback_host: str = "127.0.0.1") -> str:
        if self.radar_url:
            return self.radar_url.rstrip("/")
        host = self.advertised_host(fallback_host)
        return f"http://{host}:{self.http_port}"

    def client_config(self, fallback_host: str = "127.0.0.1") -> dict:
        return {
            "socks5_host": self.advertised_host(fallback_host),
            "socks5_port": self.socks_port,
            "radar_url": self.service_url(fallback_host),
            "announce": self.announce,
        }
