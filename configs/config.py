from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


DEFAULT_CONFIG: dict[str, Any] = {
    "system": {
        "demo_mode": False,
        "log_level": "INFO",
        "require_cable_confirmation": True,
    },
    "timeouts": {
        "ssh_reboot": 600.0,
        "ssh_reconnect": 300.0,
        "migration": 1800.0,
        "baremetal_migration": 5400.0,
        "baremetal_initial_delay": 60.0,
        "baremetal_check_interval": 30.0,
    },
    "migration": {
        "mode": "offline",
        "offline_shutdown_timeout": 120.0,
        "offline_force_stop": False,
        "restart_after_offline_migration": True,
    },
    "network_defaults": {
        "gateway": "192.168.8.1",
        "prefixlen": 21,
        "wired_interface": "enp86s0",
        "wifi_interface": "wlo1",
    },
    "usb_whitelist": [
        "0403:6001",
        "10c4:ea60",
        "1a86:7523",
        "0bda:8153",
        "0bda:8156",
        "0b95:1790",
    ],
}


class ConfigNode:
    def __init__(self, data: dict[str, Any]):
        self._data = data

    def __getattr__(self, name: str) -> Any:
        if name in self._data:
            value = self._data.get(name)
            if isinstance(value, dict):
                return ConfigNode(value)
            return value
        raise AttributeError(name)

    def get(self, key: str, default: Any = None) -> Any:
        value = self._data.get(key, default)
        if isinstance(value, dict):
            return ConfigNode(value)
        return value

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(self._data)

    def __repr__(self) -> str:  # pragma: no cover
        return f"ConfigNode({self._data!r})"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged.get(k, {}), v)
        else:
            merged[k] = v
    return merged


def load_config(config_path: str | None = None) -> ConfigNode:
    base_dir = Path(__file__).resolve().parent
    default_path = base_dir / "config.yaml"
    cfg_path = Path(config_path).expanduser() if config_path else default_path

    data: dict[str, Any] = {}
    if yaml is not None and cfg_path.exists():
        try:
            raw = cfg_path.read_text(encoding="utf-8")
            loaded = yaml.safe_load(raw) if raw else None
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    merged = _deep_merge(DEFAULT_CONFIG, data)
    return ConfigNode(merged)


GlobalConfig = load_config()
