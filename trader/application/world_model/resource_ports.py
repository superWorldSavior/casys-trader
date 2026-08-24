"""Consumer-owned port for one World Model resource snapshot per write batch.

Adapters implement this contract. Application code never stats a path, opens
SQLite, or reads the environment here.
"""

from __future__ import annotations

from typing import Protocol

from trader.domain.world_resource import WorldResourceUsage

__all__ = ["WorldResourceProbe"]


class WorldResourceProbe(Protocol):
    """Measure DB logical/on-disk size and filesystem free bytes once."""

    def measure(self) -> WorldResourceUsage: ...
