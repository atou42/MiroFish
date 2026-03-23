from __future__ import annotations

import os
from typing import Dict, Iterable, Mapping


DEFAULT_PROXY_VARS = (
    "ALL_PROXY",
    "all_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "SOCKS_PROXY",
    "socks_proxy",
)


def clear_proxy_env(env: Mapping[str, str] | None = None) -> Dict[str, str]:
    payload = dict(env or os.environ)
    for key in DEFAULT_PROXY_VARS:
        payload.pop(key, None)
    return payload


def drop_proxy_env_inplace(keys: Iterable[str] = DEFAULT_PROXY_VARS) -> list[str]:
    cleared: list[str] = []
    for key in keys:
        if key in os.environ:
            os.environ.pop(key, None)
            cleared.append(key)
    return cleared
