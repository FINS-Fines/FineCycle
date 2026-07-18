from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ClonezillaImage:
    iso_name: str
    node: str
    ip: str
    status: str = "free"
