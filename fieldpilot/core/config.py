"""Configuration loader.

Loads `config.yaml` and applies environment overrides of the form
`FIELDPILOT_<SECTION>__<KEY>[__<SUBKEY>...]` (double underscore denotes nesting). Values are parsed
with YAML semantics, so `FIELDPILOT_APP__PERSPECTIVE=BOTH` and
`FIELDPILOT_ALERTS__HAPTICS__ENABLED=false` both coerce to the right types.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ENV_PREFIX = "FIELDPILOT_"
_MISSING = object()


class Config:
    """Thin wrapper over the parsed config dict with dotted-path access."""

    def __init__(self, data: dict[str, Any], path: Path | None = None):
        self._data = data
        self.path = path

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def require(self, dotted: str) -> Any:
        value = self.get(dotted, _MISSING)
        if value is _MISSING:
            raise KeyError(f"Required config key missing: {dotted}")
        return value

    def section(self, name: str) -> dict[str, Any]:
        node = self._data.get(name, {})
        return node if isinstance(node, dict) else {}

    def as_dict(self) -> dict[str, Any]:
        return self._data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    for env_key, raw_value in os.environ.items():
        if not env_key.startswith(ENV_PREFIX):
            continue
        path = env_key[len(ENV_PREFIX):].lower().split("__")
        if not path or not path[0]:
            continue
        try:
            value = yaml.safe_load(raw_value)
        except yaml.YAMLError:
            value = raw_value
        node = data
        for part in path[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[path[-1]] = value
    return data


def load_config(path: str | os.PathLike[str] = "config.yaml") -> Config:
    cfg_path = Path(path)
    with cfg_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    data = _apply_env_overrides(data)
    return Config(data, path=cfg_path)
