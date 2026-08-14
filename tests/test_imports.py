"""Every module must at least import.

This exists because the suite once passed 152 tests with a syntax error in
daemon/main.py: nothing imports the capture loop, so nothing noticed. The
daemon is the one component whose failure is invisible until a night is lost,
which makes "does it even load" worth asserting explicitly.
"""
from __future__ import annotations

import importlib
import pkgutil

import pytest

import skylapse

# picamera2 comes from apt and is absent on dev boxes; pidng needs a C
# toolchain. Both are optional at runtime and guarded at their call sites.
OPTIONAL = {"skylapse.daemon.drivers.picam"}


def _modules() -> list[str]:
    found = []
    for info in pkgutil.walk_packages(skylapse.__path__, prefix="skylapse."):
        if not info.ispkg:
            found.append(info.name)
    return sorted(found)


@pytest.mark.parametrize("name", _modules())
def test_module_imports(name):
    try:
        importlib.import_module(name)
    except ImportError as exc:
        if name in OPTIONAL:
            pytest.skip(f"{name}: optional dependency missing ({exc})")
        raise


def test_the_capture_daemon_is_constructible():
    """Import alone would miss a NameError in the constructor."""
    from skylapse.daemon.main import CaptureDaemon
    assert CaptureDaemon() is not None
