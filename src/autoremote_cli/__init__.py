"""Public package API for autoremote-cli."""

from .cli import AutoRemote
from .cli import AutoRemoteConfigError
from .cli import AutoRemoteError
from .cli import AutoRemoteUsageError
from .cli import main

__all__ = [
    "AutoRemote",
    "AutoRemoteError",
    "AutoRemoteConfigError",
    "AutoRemoteUsageError",
    "main",
]
