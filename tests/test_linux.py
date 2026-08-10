# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) Jaeyoon Choi <j_yoon.choi@samsung.com>
"""Unit tests for the Linux platform (runnable on any platform)."""

import sys
from types import SimpleNamespace

from devbind import devbind
from devbind.devbind import Linux, Device


# Representative `lspci -Dvmmnk` output: an NVMe controller and a
# non-matching ethernet device.
LSPCI_DVMMNK = (
    "Slot:\t0000:01:00.0\n"
    "Class:\t0108\n"
    "Vendor:\t144d\n"
    "Device:\ta808\n"
    "SVendor:\t144d\n"
    "SDevice:\ta801\n"
    "Driver:\tnvme\n"
    "Module:\tnvme\n"
    "\n"
    "Slot:\t0000:03:00.0\n"
    "Class:\t0200\n"
    "Vendor:\t8086\n"
    "Device:\t10d3\n"
    "\n"
)


def test_scan_devices_parses_and_filters(monkeypatch):
    monkeypatch.setattr(
        devbind,
        "run",
        lambda cmd: SimpleNamespace(stdout=LSPCI_DVMMNK, stderr="", returncode=0),
    )
    # Avoid touching the real /sys and /dev during probing
    for name in ("_probe_handles", "_probe_usage", "_probe_driver", "_probe_iommugroup"):
        monkeypatch.setattr(Linux, name, staticmethod(lambda device: None))

    devices = list(Linux().scan_devices(0x0108))

    # The ethernet device (class 0x0200) is filtered out
    assert [d.bdf for d in devices] == ["0000:01:00.0"]

    nvme = devices[0]
    assert nvme.vendor == "144d"
    assert nvme.device == "a808"
    assert nvme.classcode == "0108"


def test_bind_writes_override_then_bind_then_setpci(monkeypatch):
    writes = []
    calls = []
    monkeypatch.setattr(
        devbind, "sysfs_write", lambda path, text: writes.append((str(path), text))
    )
    monkeypatch.setattr(
        devbind,
        "run",
        lambda cmd: calls.append(cmd) or SimpleNamespace(stdout="", stderr="", returncode=0),
    )
    monkeypatch.setattr(Linux, "unbind", lambda self, device: None)

    device = Device(
        bdf="0000:01:00.0", vendor="144d", device="a808", classcode="0108", driver="nvme"
    )
    Linux().bind(device, "uio_pci_generic")

    assert writes == [
        ("/sys/bus/pci/devices/0000:01:00.0/driver_override", "uio_pci_generic"),
        ("/sys/bus/pci/drivers/uio_pci_generic/bind", "0000:01:00.0"),
    ]
    assert "setpci -s 0000:01:00.0 COMMAND=0x06" in calls


def test_get_platform_selects_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert isinstance(devbind.get_platform(), Linux)


def test_scan_devices_named_device_bypasses_class_filter(monkeypatch):
    monkeypatch.setattr(
        devbind,
        "run",
        lambda cmd: SimpleNamespace(stdout=LSPCI_DVMMNK, stderr="", returncode=0),
    )
    for name in ("_probe_handles", "_probe_usage", "_probe_driver", "_probe_iommugroup"):
        monkeypatch.setattr(Linux, name, staticmethod(lambda device: None))

    devices = list(Linux().scan_devices(0x0108, "0000:03:00.0"))

    # The ethernet device is found by BDF even though its class is 0x0200
    assert [d.bdf for d in devices] == ["0000:03:00.0"]
