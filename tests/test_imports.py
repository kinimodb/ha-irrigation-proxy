"""Smoke test: every integration module must be importable.

Fängt Syntaxfehler, kaputte Importe und fehlende Stub-Attribute ab,
bevor sie erst in einer echten HA-Installation auffallen.
"""

from __future__ import annotations

import importlib

import pytest

_MODULES = [
    "custom_components.irrigation_proxy",
    "custom_components.irrigation_proxy.config_flow",
    "custom_components.irrigation_proxy.const",
    "custom_components.irrigation_proxy.coordinator",
    "custom_components.irrigation_proxy.entity",
    "custom_components.irrigation_proxy.migration",
    "custom_components.irrigation_proxy.number",
    "custom_components.irrigation_proxy.safety",
    "custom_components.irrigation_proxy.scheduler",
    "custom_components.irrigation_proxy.sensor",
    "custom_components.irrigation_proxy.sequencer",
    "custom_components.irrigation_proxy.switch",
    "custom_components.irrigation_proxy.zone",
]


@pytest.mark.parametrize("module_name", _MODULES)
def test_module_imports(module_name: str) -> None:
    assert importlib.import_module(module_name) is not None
