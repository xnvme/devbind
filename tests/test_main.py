# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) Jaeyoon Choi <j_yoon.choi@samsung.com>
"""Unit tests for main(), the CLI entry point (runnable on any platform)."""

import errno
import sys

import pytest

from devbind import devbind
from devbind.devbind import Linux


def _argv(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["devbind", *args])


def _platform(monkeypatch, platform=None):
    monkeypatch.setattr(devbind, "get_platform", lambda: platform or Linux())


def _root(monkeypatch, is_root=True):
    monkeypatch.setattr(devbind.os, "geteuid", lambda: 0 if is_root else 1000)


def test_a_failing_probe_exits_instead_of_raising(monkeypatch):
    """A machine without PCI sysfs must not end in a traceback"""

    def raising_probe(self):
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", "/sys/bus/pci/drivers")

    monkeypatch.setattr(Linux, "probe_drivers", raising_probe)
    _argv(monkeypatch, "--list")
    _platform(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        devbind.main()
    assert exc.value.code == errno.ENOENT


def test_a_failing_scan_exits_instead_of_raising(monkeypatch):
    def raising_scan(self, classcode, bdf=None):
        raise OSError(errno.EIO, "lspci failed")
        yield  # pragma: no cover - generator marker

    monkeypatch.setattr(Linux, "probe_drivers", lambda self: {})
    monkeypatch.setattr(Linux, "scan_devices", raising_scan)
    _argv(monkeypatch, "--list")
    _platform(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        devbind.main()
    assert exc.value.code == errno.EIO


def test_an_unsupported_platform_exits_with_enosys(monkeypatch):
    def no_platform():
        raise NotImplementedError("devbind does not support platform 'darwin'")

    monkeypatch.setattr(devbind, "get_platform", no_platform)
    _argv(monkeypatch, "--list")

    with pytest.raises(SystemExit) as exc:
        devbind.main()
    assert exc.value.code == errno.ENOSYS


def test_an_unsupported_driver_is_reported_before_the_root_check(monkeypatch, caplog):
    """Re-running under sudo would not change the answer, so say so first"""

    # A driver-name the CLI accepts but the running platform does not provide
    monkeypatch.setattr(devbind, "KNOWN_DRIVERS", devbind.KNOWN_DRIVERS | {"other-platform-drv"})
    _argv(monkeypatch, "--bind", "other-platform-drv", "--device", "0000:01:00.0")
    _platform(monkeypatch)
    _root(monkeypatch, is_root=False)

    with pytest.raises(SystemExit) as exc:
        devbind.main()
    assert exc.value.code == errno.EINVAL
    assert "not supported on this platform" in caplog.text


def test_binding_without_root_asks_for_sudo(monkeypatch, caplog):
    _argv(monkeypatch, "--bind", "vfio-pci", "--device", "0000:01:00.0")
    _platform(monkeypatch)
    _root(monkeypatch, is_root=False)

    with pytest.raises(SystemExit) as exc:
        devbind.main()
    assert exc.value.code == errno.EPERM
    assert "requires root" in caplog.text
