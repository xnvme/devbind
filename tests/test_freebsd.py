# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) Jaeyoon Choi <j_yoon.choi@samsung.com>
"""Unit tests for the FreeBSD platform (runnable on any platform)."""

import sys
from types import SimpleNamespace

import pytest

from devbind import devbind
from devbind.devbind import FreeBSD, Device, bdf_to_selector, selector_to_bdf


# Representative `pciconf -l` output: an NVMe controller (bound), an NVMe
# controller without a driver, and a non-matching ethernet device. The first
# two lines use the packed chip=/card= format of FreeBSD <= 14; the last NVMe
# line uses the split vendor=/device= format that FreeBSD 15 switched to.
PCICONF_L = (
    "nvme0@pci0:1:0:0:\tclass=0x010802 card=0xa801144d chip=0xa808144d rev=0x00 hdr=0x00\n"
    "none0@pci0:2:0:0:\tclass=0x010802 card=0x0000 chip=0x540a1b96 rev=0x00 hdr=0x00\n"
    "em0@pci0:3:0:0:\tclass=0x020000 card=0x00008086 chip=0x10d38086 rev=0x00 hdr=0x00\n"
    "nvme1@pci0:4:0:0:\tclass=0x010802 rev=0x02 hdr=0x00 vendor=0x1b36 device=0x0010 "
    "subvendor=0x1af4 subdevice=0x1100\n"
)


def _fake_run(outputs):
    """Build a run() replacement dispatching by command prefix

    Commands matching no prefix fail (returncode 1), so a test cannot pass by
    accident when the code under test never issues the expected command.
    """

    def runner(cmd):
        for prefix, result in outputs.items():
            if cmd.startswith(prefix):
                return SimpleNamespace(stdout=result, stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=1)

    return runner


def test_selector_bdf_roundtrip():
    assert selector_to_bdf("pci0:1:0:0") == "0000:01:00.0"
    assert bdf_to_selector("0000:01:00.0") == "pci0:1:0:0"
    assert selector_to_bdf("pci1:255:31:7") == "0001:ff:1f.7"
    assert bdf_to_selector(selector_to_bdf("pci0:130:5:3")) == "pci0:130:5:3"


def test_scan_devices_parses_and_filters(monkeypatch):
    monkeypatch.setattr(
        devbind, "run", _fake_run({"pciconf -l": PCICONF_L, "fstat": "USER CMD ...\n"})
    )
    # Avoid touching the real /dev during handle probing
    monkeypatch.setattr(FreeBSD, "_probe_handles", staticmethod(lambda device, inst: None))

    devices = list(FreeBSD().scan_devices(0x0108))

    # The ethernet device (class 0x0200) is filtered out
    assert [d.bdf for d in devices] == ["0000:01:00.0", "0000:02:00.0", "0000:04:00.0"]

    nvme = devices[0]
    assert nvme.vendor == "144d"  # low 16 bits of chip
    assert nvme.device == "a808"  # high 16 bits of chip
    assert nvme.classcode == "0108"
    assert nvme.driver == "nvme"

    assert devices[1].driver is None  # 'none0' -> no driver attached

    fb15 = devices[2]  # FreeBSD 15 vendor=/device= format
    assert fb15.vendor == "1b36"
    assert fb15.device == "0010"
    assert fb15.driver == "nvme"


def test_probe_drivers_nic_uio_loaded(monkeypatch):
    monkeypatch.setattr(
        devbind, "run", _fake_run({"kldstat -q -n nic_uio": ""})
    )  # returncode 0 -> loaded
    drivers = FreeBSD().probe_drivers()
    assert drivers["nvme"]["available"] is True
    assert drivers["nic_uio"]["available"] is True


def test_probe_drivers_nic_uio_not_loaded(monkeypatch):
    monkeypatch.setattr(devbind, "run", _fake_run({}))  # kldstat fails -> not loaded
    drivers = FreeBSD().probe_drivers()
    assert drivers["nvme"]["available"] is True
    assert drivers["nic_uio"]["available"] is False


def test_probe_usage_via_fstat(monkeypatch):
    device = Device(bdf="0000:01:00.0", vendor="144d", device="a808", classcode="0108")
    device.handles = ["/dev/nvme0", "/dev/nda0"]

    header = "USER     CMD          PID   FD PATH\n"

    monkeypatch.setattr(devbind, "run", _fake_run({"fstat": header}))
    FreeBSD._probe_usage(device)
    assert device.is_used is False  # header only -> no open handles

    monkeypatch.setattr(devbind, "run", _fake_run({"fstat": header + "root dd 71 3 /dev/nda0\n"}))
    FreeBSD._probe_usage(device)
    assert device.is_used is True


CAMCONTROL_V = (
    "scbus0 on ahcich0 bus 0:\n"
    "<Samsung SSD 860 EVO 1TB RVT04B6Q>  at scbus0 target 0 lun 0 (pass0,ada0)\n"
    "<>                                  at scbus0 target -1 lun ffffffff ()\n"
    "scbus1 on nvme0 bus 0:\n"
    "<SAMSUNG MZVL2512HCJQ GXA7401Q>     at scbus1 target 0 lun 1 (pass1,nda0)\n"
    "<>                                  at scbus1 target -1 lun ffffffff ()\n"
    "scbus-1 on xpt0 bus 0:\n"
    "<>                                  at scbus-1 target -1 lun ffffffff (xpt0)\n"
)


def test_cam_disks_maps_controller_to_disks(monkeypatch):
    monkeypatch.setattr(devbind, "run", _fake_run({"camcontrol devlist -v": CAMCONTROL_V}))
    assert FreeBSD._cam_disks("nvme0") == ["nda0"]
    assert FreeBSD._cam_disks("ahcich0") == []  # ada is not an NVMe disk
    assert FreeBSD._cam_disks("nvme1") == []  # no such bus


def _nic_uio_run(calls, loaded=False, bdfs="", attach_ok=True):
    """run() fake for the nic_uio bind flow, dispatching by exact command"""

    def runner(cmd):
        calls.append(cmd)
        if cmd == "kenv hw.nic_uio.bdfs":
            return SimpleNamespace(stdout=bdfs, stderr="", returncode=0 if bdfs else 1)
        if cmd.startswith("kldstat"):
            return SimpleNamespace(stdout="", stderr="", returncode=0 if loaded else 1)
        if cmd == "pciconf -l":
            inst = "nic_uio0" if attach_ok else "none0"
            line = f"{inst}@pci0:1:0:0:\tclass=0x010802 vendor=0x1b36 device=0x0010\n"
            return SimpleNamespace(stdout=line, stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    return runner


def _nvme_device(driver="nvme"):
    return Device(
        bdf="0000:01:00.0", vendor="144d", device="a808", classcode="0108", driver=driver
    )


def test_bind_nic_uio_registers_bdfs_and_loads_the_module(monkeypatch):
    calls = []
    monkeypatch.setattr(devbind, "run", _nic_uio_run(calls))

    FreeBSD().bind(_nvme_device(), "nic_uio")

    assert "kenv hw.nic_uio.bdfs=1:0:0" in calls
    assert "kldload nic_uio" in calls
    # pciconf -w takes the selector as a positional argument (no -s flag)
    assert "pciconf -w -h pci0:1:0:0 0x4 0x6" in calls
    # the module load detaches and claims the device by itself; devctl is
    # only used to drop a previously forced driver
    assert "devctl clear driver -f pci0:1:0:0" in calls
    assert not any(cmd.startswith(("devctl detach", "devctl set driver")) for cmd in calls)
    assert "kldunload nic_uio" not in calls


def test_bind_nic_uio_reloads_a_loaded_module_and_merges_bdfs(monkeypatch):
    calls = []
    monkeypatch.setattr(devbind, "run", _nic_uio_run(calls, loaded=True, bdfs="3:0:0"))

    FreeBSD().bind(_nvme_device(), "nic_uio")

    assert "kldunload nic_uio" in calls
    assert "kenv hw.nic_uio.bdfs=3:0:0,1:0:0" in calls
    assert calls.index("kldunload nic_uio") < calls.index("kldload nic_uio")


def test_bind_nic_uio_fails_loudly_when_the_module_does_not_attach(monkeypatch):
    calls = []
    monkeypatch.setattr(devbind, "run", _nic_uio_run(calls, attach_ok=False))

    with pytest.raises(OSError):
        FreeBSD().bind(_nvme_device(), "nic_uio")

    assert not any(cmd.startswith("pciconf -w") for cmd in calls)


def test_bind_nvme_uses_devctl_and_clears_the_bdfs_entry(monkeypatch):
    calls = []
    monkeypatch.setattr(devbind, "run", _nic_uio_run(calls, bdfs="1:0:0"))

    FreeBSD().bind(_nvme_device(driver="nic_uio"), "nvme")

    assert "devctl detach pci0:1:0:0" in calls
    assert "devctl set driver pci0:1:0:0 nvme" in calls
    assert "kenv -u hw.nic_uio.bdfs" in calls


def test_unbind_skips_when_unbound(monkeypatch):
    calls = []
    monkeypatch.setattr(devbind, "run", lambda cmd: calls.append(cmd) or SimpleNamespace())
    device = Device(
        bdf="0000:01:00.0", vendor="144d", device="a808", classcode="0108", driver=None
    )
    FreeBSD().unbind(device)
    assert calls == []


def test_get_platform_selection(monkeypatch):
    monkeypatch.setattr(sys, "platform", "freebsd14")
    assert isinstance(devbind.get_platform(), FreeBSD)

    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(NotImplementedError):
        devbind.get_platform()


def test_scan_devices_named_device_bypasses_class_filter(monkeypatch):
    monkeypatch.setattr(devbind, "run", _fake_run({"pciconf -l": PCICONF_L}))
    monkeypatch.setattr(FreeBSD, "_probe_handles", staticmethod(lambda device, inst: None))

    devices = list(FreeBSD().scan_devices(0x0108, "0000:03:00.0"))

    # The ethernet device is found by BDF even though its class is 0x0200
    assert [d.bdf for d in devices] == ["0000:03:00.0"]
    assert devices[0].driver == "em"
