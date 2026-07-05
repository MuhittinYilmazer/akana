"""Akana core library — provider-neutral driver + memory primitives.

This package is the clean core that the ``akana_server`` application depends on:
``akana_server`` imports ``akana.memory`` (the unified memory stack) and
``akana.driver`` (provider-neutral chat backends). Kept deliberately small and
free of web/app concerns so it can be reused and tested in isolation.
"""

from __future__ import annotations

__version__ = "0.0.1"
