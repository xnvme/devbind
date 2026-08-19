#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) Simon Andreas Frimann Lund <os@safl.dk>
#
# Get info about and control driver associated with NVMe devices
#
# Platform-specific operations (device enumeration, driver binding) are handled by
# a Platform selected at runtime via get_platform():
#
# * Linux   -- sysfs (/sys/bus/pci/...) + lspci / lsof / setpci
#
# Kept as a single, stdlib-only file so it can be installed by copying this script.
#
import sys
import os
import abc
import subprocess
import argparse
import errno
import resource
import time
import logging as log
from itertools import chain
from typing import Iterable, Optional
from pathlib import Path
from dataclasses import dataclass, asdict, field

__version__ = "0.3.10"

PCIE_DEFAULT_CLASSCODE = 0x0108  # Mass Storage - NVM

# Driver-names recognized across platforms; used for argument parsing and
# completion. The active platform reports which are actually available.
KNOWN_DRIVERS = {"nvme", "vfio-pci", "vfio-noiommu", "uio_pci_generic"}

BASH_COMPLETION = r"""# bash completion for devbind
_devbind() {
    local cur="${COMP_WORDS[COMP_CWORD]}"
    local prev="${COMP_WORDS[COMP_CWORD-1]}"
    local drivers="nvme vfio-pci vfio-noiommu uio_pci_generic"
    local opts="--classcode --device --list --unbind --bind --verbose --help --print-completion"
    case "${prev}" in
        --bind)
            COMPREPLY=($(compgen -W "${drivers}" -- "${cur}"))
            compopt -o default 2>/dev/null
            return 0
            ;;
    esac
    if [[ ${cur} == -* ]]; then
        COMPREPLY=($(compgen -W "${opts}" -- "${cur}"))
    fi
}
complete -F _devbind devbind
"""


def run(cmd: str):
    """Run a command and capture the output"""
    log.info(f"cmd({cmd})")
    return subprocess.run(cmd, capture_output=True, shell=True, text=True)


@dataclass
class Device:
    """Encapsulation of a PCIe device"""

    bdf: str  # PCI address of the device, e.g. "0000:02:00.0"
    vendor: str  # Vendor ID (hex), e.g. "144d" for Samsung
    device: str  # Device ID (hex), identifies the specific device model
    classcode: str  # PCI class code (hex), e.g. "0108" for NVMe controller

    driver: Optional[str] = None  # Name of the driver bound to the device, e.g. "nvme"
    iommugroup: Optional[int] = None  # IOMMU group number the device belongs to

    is_used: bool = True  # Whether or not the device is in use; assume it is
    handles: list = field(default_factory=list)


class Platform(abc.ABC):
    """Platform-specific PCI device-driver binding operations"""

    #: Drivers this platform knows how to bind devices to
    DRIVERS: set = set()

    def driver_names(self) -> set:
        """Return the set of driver-names this platform can bind to"""
        return set(self.DRIVERS)

    @abc.abstractmethod
    def probe_drivers(self) -> dict:
        """Return ``{driver_name: {"available": bool}}`` for the known drivers"""

    @abc.abstractmethod
    def scan_devices(self, classcode: int, bdf: Optional[str] = None) -> Iterable[Device]:
        """Yield a fully-probed Device for each matching PCIe device

        A bdf (domain:bus:device.function, e.g. "0000:01:00.0") bypasses the class filter:
        only that device is yielded, whatever its class. Otherwise devices
        whose class matches classcode are yielded.
        """

    @abc.abstractmethod
    def unbind(self, device: Device):
        """Detach device from its current driver"""

    @abc.abstractmethod
    def bind(self, device: Device, driver_name: str):
        """Unbind if bound, then bind device to driver_name"""

    @abc.abstractmethod
    def memlock_remediation_hint(self) -> str:
        """Platform-specific guidance for raising RLIMIT_MEMLOCK"""


# --- Linux platform -----------------------------------------------------------

# Mapping from ``lspci -Dvmmnk`` record keys to Device fields
_LSPCI_KEYS = {
    "slot": "bdf",
    "vendor": "vendor",
    "device": "device",
    "classcode": "classcode",
}


def sysfs_write(path: Path, text):
    log.info(f'{path} "{text}"')
    with os.fdopen(os.open(path, os.O_WRONLY), "w") as f:
        f.write(f"{text}\n")


class Linux(Platform):
    """Linux implementation, via sysfs and CLI tools

    This makes use of the following tools:

    * lspci -Dvmmnk
    * lsof {devhandle1, devhandle2, ... devhandleN}

    And the following sysfs entries for driver bindings:

    * /sys/bus/pci/devices/{bdf}/driver
    * /sys/bus/pci/devices/{bdf}/driver_override
    * /sys/bus/pci/devices/{bdf}/driver/unbind
    * /sys/bus/pci/devices/{bdf}/iommu_group
    * /sys/bus/pci/devices/{bdf}/nvme/nvme*
    * /sys/bus/pci/devices/{bdf}/nvme/nvme*/ng*
    * /sys/bus/pci/devices/{bdf}/nvme/nvme*/nvme*
    * /sys/bus/pci/drivers/{driver_name}/bind

    The following could, but currently are not, be used for automatic
    detection based on class-code etc.

    * /sys/bus/pci/drivers/{driver_name}/new_id
    * /sys/bus/pci/drivers_probe
    """

    DRIVERS = {"nvme", "vfio-pci", "vfio-noiommu", "uio_pci_generic"}

    def probe_drivers(self):
        loaded = set(
            (path.name for path in Path("/sys/bus/pci/drivers").resolve(strict=True).glob("*"))
        )

        missing = self.DRIVERS - loaded

        drivers = {}
        for driver_name in self.DRIVERS:
            drivers[driver_name] = {
                "available": driver_name not in missing,
            }
        return drivers

    def memlock_remediation_hint(self):
        return "Raise via /etc/security/limits.d/, prlimit, or systemd LimitMEMLOCK="

    @staticmethod
    def _device_from_dict(data: dict) -> Device:
        cdata = {}
        for src, tgt in _LSPCI_KEYS.items():
            cdata[tgt] = data.copy().get(src)

        return Device(**cdata)

    @staticmethod
    def _probe_driver(device: Device):
        """Populate driver via sysfs; returns False if no driver is found"""
        try:
            device.driver = (
                Path(f"/sys/bus/pci/devices/{device.bdf}/driver").resolve(strict=True).name
            )
        except FileNotFoundError:
            pass
        return device.driver is not None

    @staticmethod
    def _probe_iommugroup(device: Device):
        """Populate iommugroup via sysfs; returns False if no iommugroup is found"""
        try:
            device.iommugroup = int(
                Path(f"/sys/bus/pci/devices/{device.bdf}/iommu_group").resolve(strict=True).name
            )
        except FileNotFoundError:
            device.iommugroup = None
        return device.iommugroup is not None

    @staticmethod
    def _probe_handles(device: Device):
        """Determine possible handles to the NVMe device"""
        for top in Path(f"/sys/bus/pci/devices/{device.bdf}/nvme").glob("nvme*"):
            for bottom in chain(top.glob("ng*"), top.glob("nvme*")):
                for path in Path("/dev").glob(f"{bottom.name}*"):
                    device.handles.append(str(path))

    @staticmethod
    def _probe_usage(device: Device):
        """Attempt to determine whether the device is in use"""
        if not device.handles:
            device.is_used = False
            return
        handles = " ".join(device.handles)
        proc = run(f"lsof {handles}")
        device.is_used = bool(proc.stdout)

    def scan_devices(self, classcode: int, bdf: Optional[str] = None):
        proc = run("lspci -Dvmmnk")

        props = {}
        for line in proc.stdout.splitlines():
            if not line:
                if bdf:
                    matches = bdf == props.get("slot", "")
                else:
                    matches = int(props.get("classcode", "0"), 16) == classcode
                if matches:
                    device = self._device_from_dict(props)
                    self._probe_handles(device)
                    self._probe_usage(device)
                    self._probe_driver(device)
                    self._probe_iommugroup(device)
                    yield device

                props = {}
                continue

            key, val = [txt.strip().lower() for txt in str(line).split(":", 1)]
            if key == "class":
                key = "classcode"

            props[key] = val

    def unbind(self, device: Device):
        log.info(f"Unbinding({device.bdf}) from '{device.driver}'")

        driver_path = Path("/sys") / "bus" / "pci" / "devices" / device.bdf / "driver"

        unbind = driver_path / "unbind"
        if not unbind.exists():
            log.info("Not bound; skipping unbind()")
            return

        sysfs_write(unbind, device.bdf)

    def bind(self, device: Device, driver_name: str):
        """Bind the driver named 'driver_name' with 'device'"""

        self.unbind(device)

        log.info(f"Binding({device.bdf}) to '{driver_name}'")

        sysfs = Path("/sys") / "bus" / "pci"

        sysfs_write(sysfs / "devices" / device.bdf / "driver_override", driver_name)

        max_attempts = 10
        for attempt in range(1, max_attempts + 1):
            try:
                sysfs_write(sysfs / "drivers" / driver_name / "bind", device.bdf)
                break
            except OSError as exc:
                if attempt == max_attempts or exc.errno != errno.EBUSY:
                    log.error(f"Could not bind despite {max_attempts} retries.")
                    raise
                delay = attempt * 1
                log.info(f"Retrying in in {delay} second(s)")
                time.sleep(delay)

        # Enable BUS-mastering (tell it that it can initiate DMA)
        if driver_name == "uio_pci_generic":
            log.info(f"Running setpci to enable bus-mastering; driver_name({driver_name})")
            run(f"setpci -s {device.bdf} COMMAND=0x06")
        else:
            log.info(f"Not running setpci; driver_name({driver_name})")


def get_platform() -> Platform:
    """Return the Platform implementation for the running platform"""
    if sys.platform.startswith("linux"):
        return Linux()

    raise NotImplementedError(f"devbind does not support platform '{sys.platform}'")


class System:
    # DPDK/SPDK and xNVMe/uPCIe convention: VFIO_IOMMU_MAP_DMA pins user
    # space pages against RLIMIT_MEMLOCK. Below 64 MiB the buffer-pool
    # allocation fails outright.
    MEMLOCK_MIN_BYTES = 64 * 1024 * 1024

    def __init__(self):
        self.drivers: dict = {}
        self.limits: dict = {}

    def probe_limits(self, remediation_hint: str = ""):
        """Read process resource limits relevant to vfio-pci consumers"""

        soft, hard = resource.getrlimit(resource.RLIMIT_MEMLOCK)
        self.limits["memlock_soft"] = soft
        self.limits["memlock_hard"] = hard

        if soft != resource.RLIM_INFINITY and soft < self.MEMLOCK_MIN_BYTES:
            log.warning(
                f"memlock soft limit ({self._fmt_bytes(soft)}) is below "
                f"{self._fmt_bytes(self.MEMLOCK_MIN_BYTES)}; "
                "VFIO_IOMMU_MAP_DMA will fail for DPDK/SPDK and xNVMe/uPCIe. "
                f"{remediation_hint}"
            )

    @staticmethod
    def _fmt_bytes(n):
        if n == resource.RLIM_INFINITY:
            return "unlimited"
        for unit in ("B", "kB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n} {unit}"
            n //= 1024
        return f"{n} PB"

    def pp(self):
        print("system:")
        print("  drivers:")
        for driver_name, props in self.drivers.items():
            print(f"  - {driver_name}: {props}")
        print("  limits:")
        for name, val in self.limits.items():
            print(f"    {name}: {self._fmt_bytes(val)}")


def print_props(args, device: Device):
    """Pretty-print the properties of a device"""

    print("props:")
    for key, val in asdict(device).items():
        if isinstance(val, int) or isinstance(val, list):
            print(f"  {key}: {val}")
        else:
            print(f"  {key}: '{val}'")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Inspect and control PCI device-driver binding in Linux"
    )

    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    parser.add_argument(
        "--classcode",
        default=PCIE_DEFAULT_CLASSCODE,
        type=lambda v: int(v, 16),
        help="The class of PCIe devices to scan for (hex, e.g. 0x0108 for NVMe)",
    )

    parser.add_argument(
        "--device",
        required=False,
        help="Instead of all; then only the given PCI address.",
    )

    parser.add_argument(
        "--list",
        action="store_true",
        help="Print PCIe device(s); such as their 'bdf' and driver-association.",
    )

    parser.add_argument("--unbind", action="store_true", help="Unbind if bound.")

    def parse_bind(value):
        if value in KNOWN_DRIVERS:
            return value
        return Path(value)

    parser.add_argument(
        "--bind",
        type=parse_bind,
        help="Unbind if bound; then bind to the given driver-name [nvme, vfio-pci, uio_pci_generic] or to a .ko driver file (path)",
    )

    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")

    parser.add_argument(
        "--print-completion",
        choices=["bash"],
        metavar="SHELL",
        help="Print shell completion script to stdout and exit",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    if args.print_completion == "bash":
        sys.stdout.write(BASH_COMPLETION)
        return

    log.basicConfig(
        level=log.DEBUG if args.verbose else log.INFO,
        format="# %(levelname)s: %(message)s",
    )

    try:
        platform = get_platform()
    except NotImplementedError as exc:
        log.error(str(exc))
        sys.exit(errno.ENOSYS)

    # Before the root check: an unsupported driver-name is the error the user
    # can act on, and re-running under sudo would not change it
    if isinstance(args.bind, str) and args.bind not in platform.driver_names():
        log.error(
            f"driver '{args.bind}' is not supported on this platform; "
            f"expected one of: {', '.join(sorted(platform.driver_names()))}"
        )
        sys.exit(errno.EINVAL)

    if (args.bind or args.unbind) and os.geteuid() != 0:
        log.error("Binding/unbinding PCIe devices requires root. Re-run with sudo.")
        sys.exit(errno.EPERM)

    system = System()

    try:
        system.drivers = platform.probe_drivers()
        system.probe_limits(platform.memlock_remediation_hint())

        if args.list:
            system.pp()

        devices = list(platform.scan_devices(args.classcode, args.device))

        for cur, device in enumerate(devices, 1):
            log.info(f"Device({device.bdf}) -- {cur}/{len(devices)}")

            if args.list:
                print_props(args, device)

            if args.unbind:
                if device.is_used:
                    log.info(f"Skipping unbind({device.driver}); device is in use.")
                else:
                    platform.unbind(device)

            if args.bind:
                if device.is_used:
                    log.info(f"Skipping bind({args.bind}); device is in use.")
                else:
                    platform.bind(device, args.bind)
    except PermissionError as exc:
        log.error(str(exc))
        log.error("Binding/unbinding PCIe devices requires root. Re-run with sudo.")
        sys.exit(errno.EPERM)
    except OSError as exc:
        log.error(str(exc))
        sys.exit(exc.errno or 1)


if __name__ == "__main__":
    main()
