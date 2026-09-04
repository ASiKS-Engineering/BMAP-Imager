"""Windows physical-disk enumeration, sizing and volume lock/dismount helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import struct

import pywintypes
import win32com.client
import win32file
import winioctlcon


class DiskError(Exception):
    """Raised for physical-disk access failures."""


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


@dataclass
class DiskInfo:
    number: int
    size: int
    sector_size: int
    model: str
    bus: str
    removable: bool
    is_system: bool
    volumes: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        kind = "Removable" if self.removable else "Fixed"
        letters = f" ({', '.join(self.volumes)})" if self.volumes else ""
        return f"{self.model} — {human_size(self.size)} — {kind}/{self.bus}{letters}"


def _wmi():
    return win32com.client.GetObject("winmgmts:")


def _query_geometry(number: int) -> tuple[int, int]:
    """Return (size_bytes, sector_size) via IOCTL_DISK_GET_DRIVE_GEOMETRY_EX."""
    path = r"\\.\PhysicalDrive%d" % number

    handle = win32file.CreateFile(
        path,
        0,
        win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
        None,
        win32file.OPEN_EXISTING,
        0,
        None,
    )

    try:
        raw = win32file.DeviceIoControl(
            handle, winioctlcon.IOCTL_DISK_GET_DRIVE_GEOMETRY_EX, None, 64
        )
    finally:
        handle.Close()

    # DISK_GEOMETRY_EX: DISK_GEOMETRY (Cylinders:int64, MediaType:u32,
    # TracksPerCylinder:u32, SectorsPerTrack:u32, BytesPerSector:u32)
    # followed by DiskSize:int64.
    sector_size = struct.unpack_from("<I", raw, 20)[0] or 512
    size = struct.unpack_from("<q", raw, 24)[0]

    return size, sector_size


def _volumes_for_disk(number: int) -> list[str]:
    wmi = _wmi()
    device_id = r"\\.\PhysicalDrive%d" % number

    letters: list[str] = []

    query = (
        "ASSOCIATORS OF {Win32_DiskDrive.DeviceID='%s'} "
        "WHERE AssocClass = Win32_DiskDriveToDiskPartition" % device_id
    )

    for partition in wmi.ExecQuery(query):
        query2 = (
            "ASSOCIATORS OF {Win32_DiskPartition.DeviceID='%s'} "
            "WHERE AssocClass = Win32_LogicalDiskToPartition" % partition.DeviceID
        )

        for logical_disk in wmi.ExecQuery(query2):
            letters.append(str(logical_disk.DeviceID))

    return letters


def _is_system_disk(volumes: list[str]) -> bool:
    """True if the disk holds the Windows system/boot drive letter.

    Note: Win32_DiskPartition.BootPartition merely reflects the MBR
    "active" flag, which removable/bootable USB sticks can also carry, so
    it is deliberately not used here to avoid false positives.
    """
    system_drive = os.environ.get("SystemDrive", "C:").upper()
    return any(letter.upper() == system_drive for letter in volumes)


def list_physical_drives() -> list[DiskInfo]:
    """Enumerate all physical drives visible to Windows via WMI + IOCTL."""
    drives: list[DiskInfo] = []
    wmi = _wmi()

    for drive in wmi.InstancesOf("Win32_DiskDrive"):
        device_id = str(drive.DeviceID)  # e.g. \\.\PHYSICALDRIVE4

        try:
            number = int(device_id.rsplit("PHYSICALDRIVE", 1)[1])
        except (IndexError, ValueError):
            continue

        try:
            size, sector_size = _query_geometry(number)
        except pywintypes.error:
            size = int(drive.Size) if drive.Size else 0
            sector_size = int(drive.BytesPerSector) if drive.BytesPerSector else 512

        media_type = str(drive.MediaType or "")
        interface = str(drive.InterfaceType or "")
        removable = "Removable" in media_type or interface.upper() in ("USB", "SD")
        volumes = _volumes_for_disk(number)

        drives.append(
            DiskInfo(
                number=number,
                size=size,
                sector_size=sector_size,
                model=str(drive.Model or f"PhysicalDrive{number}"),
                bus=interface or "?",
                removable=removable,
                is_system=_is_system_disk(volumes),
                volumes=volumes,
            )
        )

    drives.sort(key=lambda d: d.number)
    return drives


class VolumeLocks:
    """Locks and dismounts every volume living on a physical disk.

    Windows refuses raw writes to a disk while one of its volumes is
    mounted and in use. Locking + dismounting the volumes (like Raspberry
    Pi Imager / balenaEtcher do) releases the filesystem's hold on the
    disk for the duration of the flash.
    """

    def __init__(self, drive_letters: list[str]):
        self._handles = []
        self._drive_letters = drive_letters

    def acquire(self) -> None:
        for letter in self._drive_letters:
            path = r"\\.\%s" % letter.rstrip("\\")

            try:
                handle = win32file.CreateFile(
                    path,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                    None,
                    win32file.OPEN_EXISTING,
                    0,
                    None,
                )
            except pywintypes.error:
                continue

            try:
                win32file.DeviceIoControl(
                    handle, winioctlcon.FSCTL_LOCK_VOLUME, None, 0
                )
                win32file.DeviceIoControl(
                    handle, winioctlcon.FSCTL_DISMOUNT_VOLUME, None, 0
                )
            except pywintypes.error:
                pass

            self._handles.append(handle)

    def release(self) -> None:
        for handle in self._handles:
            try:
                win32file.DeviceIoControl(
                    handle, winioctlcon.FSCTL_UNLOCK_VOLUME, None, 0
                )
            except pywintypes.error:
                pass

            handle.Close()

        self._handles.clear()

    def __enter__(self) -> "VolumeLocks":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()
