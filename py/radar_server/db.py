"""Local-only database stub.

The on-device node keeps no accounts: the Android app has already passed
server-side login, and the local SOCKS5 listener is bound to 127.0.0.1 only.
"""

from __future__ import annotations


class Database:
    def __init__(self, *args, **kwargs):
        pass

    def init(self) -> None:
        pass

    def authenticate(self, username: str, password: str) -> dict | None:
        if not username:
            return None
        return {
            "username": username,
            "expired": False,
            "active": True,
            "expire_at": "",
            "days_left": 9999,
        }
