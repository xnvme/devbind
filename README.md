![devbind: inspect and control PCI device-driver binding in Linux](https://raw.githubusercontent.com/xnvme/devbind/main/assets/banner.svg)

# devbind

[![PyPI](https://img.shields.io/pypi/v/devbind.svg)](https://pypi.org/project/devbind/)
[![Python](https://img.shields.io/pypi/pyversions/devbind.svg)](https://pypi.org/project/devbind/)
[![Test](https://github.com/xnvme/devbind/actions/workflows/test.yml/badge.svg)](https://github.com/xnvme/devbind/actions/workflows/test.yml)

`devbind` is a small CLI for binding and unbinding PCI devices to a
chosen kernel driver. The typical use is moving a device between its
native driver (e.g. `nvme`) and a user space driver framework for
DPDK/SPDK and xNVMe/uPCIe workloads. `devbind --list` also reports the
process `RLIMIT_MEMLOCK` and warns when the soft limit is below the
64 MiB threshold those frameworks inherit.

Both **Linux** and **FreeBSD** are supported; the platform-specific
operations are handled by a platform implementation selected at runtime:

| | Linux | FreeBSD |
|---|---|---|
| enumerate / inspect | `lspci`, sysfs | `pciconf -l`, `camcontrol` |
| user space framework | `vfio-pci`, `uio_pci_generic` | `nic_uio` |
| unbind | sysfs `driver/unbind` | `devctl detach` |
| bind (kernel driver) | sysfs `drivers/<drv>/bind` | `devctl set driver` |
| bind (user space framework) | sysfs `driver_override` + `bind` | `hw.nic_uio.bdfs` + `kldload` |

(The Linux-only `iommugroup` is reported as `None` on FreeBSD.)

## Install

```
pipx install devbind
```

Or standalone (single-file, stdlib only, no pip needed):

```
curl -fsSL https://raw.githubusercontent.com/xnvme/devbind/main/src/devbind/devbind.py \
  -o ~/.local/bin/devbind && chmod +x ~/.local/bin/devbind
```

## Shell completion

```
devbind --print-completion bash > ~/.local/share/bash-completion/completions/devbind
```

Open a new shell (or `source` the file) and tab-completion is live: `devbind --bind <TAB>` lists `nvme vfio-pci vfio-noiommu uio_pci_generic nic_uio`.

## Usage

```
$ devbind --help
usage: devbind [-h] [--version] [--classcode CLASSCODE] [--device DEVICE]
               [--list] [--unbind] [--bind BIND] [--verbose]
               [--print-completion SHELL]

Inspect and control PCI device-driver binding on Linux and FreeBSD

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
  --classcode CLASSCODE
                        The class of PCIe devices to scan for
  --device DEVICE       Instead of all; then only the given PCI address.
  --list                Print PCIe device(s); such as their 'bdf' and driver-
                        association.
  --unbind              Unbind if bound.
  --bind BIND           Unbind if bound; then bind to the given driver-name
                        [nvme, vfio-pci, uio_pci_generic, nic_uio] or to a
                        driver file (path)
  --verbose             Enable verbose logging
  --print-completion SHELL
                        Print shell completion script to stdout and exit
```

A few common invocations:

```
devbind --list                                       # list NVMe devices and their drivers
sudo devbind --bind vfio-pci --device 0000:01:00.0   # bind one device to vfio-pci
sudo devbind --bind nvme --device 0000:01:00.0       # rebind to the native driver
sudo devbind --unbind --device 0000:01:00.0          # unbind without rebinding
```

On FreeBSD, bind to `nic_uio` instead of `vfio-pci`/`uio_pci_generic`
(`--device` always takes the `domain:bus:device.function` form `0000:01:00.0` on both platforms):

```
sudo devbind --bind nic_uio --device 0000:01:00.0    # hand device to DPDK/SPDK
sudo devbind --bind nvme --device 0000:01:00.0       # rebind to the native driver
```

`devbind --list` sample output (stock WSL host, no NVMe devices visible):

```
system:
  drivers:
  - uio_pci_generic: {'available': False}
  - vfio-noiommu: {'available': False}
  - vfio-pci: {'available': False}
  - nvme: {'available': True}
  limits:
    memlock_soft: 64 MB
    memlock_hard: 64 MB
```

On a host with NVMe devices visible, a `props:` block is also printed per device with `bdf`, `vendor`, `device`, `classcode`, `driver`, `iommugroup`, `handles`, and `is_used`.

## Related

- [`iommu`](https://github.com/safl/iommu): inspect and configure the IOMMU in Linux.
- [`hugepages`](https://github.com/xnvme/hugepages): inspect and manage Linux hugepages.
