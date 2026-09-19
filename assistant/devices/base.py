"""Shared contracts for device branches."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceBranch:
    """A top-level device branch and its connection state."""

    name: str
    status: str
    description: str
    platform: str = "generic"
    transport: str = "local"
