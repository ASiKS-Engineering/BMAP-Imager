"""Core flashing engine: writes an image (optionally guided by a .bmap) to a
physical drive."""
from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from typing import Callable, Optional

import pywintypes
import win32file

from . import diskutil
from .bmap import Bmap, parse_bmap

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB


class FlashError(Exception):
    """Raised when flashing fails."""


class FlashCancelled(Exception):
    """Raised when the user cancels an in-progress flash."""


@dataclass
class FlashProgress:
    bytes_written: int
    total_bytes: int
    current_range: int
    total_ranges: int

    @property
    def fraction(self) -> float:
        return self.bytes_written / self.total_bytes if self.total_bytes else 0.0


ProgressCallback = Callable[[FlashProgress], None]
LogCallback = Callable[[str], None]


def _open_disk_for_write(number: int):
    path = r"\\.\PhysicalDrive%d" % number
    return win32file.CreateFile(
        path,
        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
        win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
        None,
        win32file.OPEN_EXISTING,
        win32file.FILE_FLAG_WRITE_THROUGH,
        None,
    )


def _copy_span(
    image_file,
    disk_handle,
    offset: int,
    length: int,
    checksum: Optional[str],
    checksum_type: str,
    bytes_written: int,
    total_bytes: int,
    range_index: int,
    total_ranges: int,
    progress: Optional[ProgressCallback],
    cancel_event: Optional[threading.Event],
) -> int:
    image_file.seek(offset)
    win32file.SetFilePointer(disk_handle, offset, win32file.FILE_BEGIN)

    hasher = hashlib.new(checksum_type) if checksum else None

    remaining = length

    while remaining > 0:
        if cancel_event is not None and cancel_event.is_set():
            raise FlashCancelled()

        chunk = image_file.read(min(CHUNK_SIZE, remaining))

        if not chunk:
            raise FlashError("unexpected end of image file")

        if hasher:
            hasher.update(chunk)

        win32file.WriteFile(disk_handle, chunk)

        remaining -= len(chunk)
        bytes_written += len(chunk)

        if progress:
            progress(
                FlashProgress(
                    bytes_written,
                    total_bytes,
                    range_index + 1,
                    total_ranges,
                )
            )

    if hasher and checksum and hasher.hexdigest() != checksum:
        raise FlashError(
            f"checksum mismatch for range at offset {offset} "
            f"(expected {checksum}, got {hasher.hexdigest()})"
        )

    return bytes_written


def flash(
    image_path: str,
    bmap_path: Optional[str],
    disk_number: int,
    *,
    verify: bool = False,
    log: Optional[LogCallback] = None,
    progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """Flash `image_path` onto PhysicalDrive`disk_number`.

    If `bmap_path` is given, only the mapped ranges are written (and,
    when `verify` is set, checked against the checksums in the .bmap).
    Otherwise the whole image is written sequentially.
    """

    def _log(message: str) -> None:
        if log:
            log(message)

    image_size = os.path.getsize(image_path)

    bmap: Optional[Bmap] = None

    if bmap_path:
        bmap = parse_bmap(bmap_path)

        if bmap.image_size != image_size:
            raise FlashError(
                f"image size ({image_size} bytes) does not match "
                f".bmap image-size ({bmap.image_size} bytes)"
            )

    disks = {d.number: d for d in diskutil.list_physical_drives()}
    disk = disks.get(disk_number)

    if disk is None:
        raise FlashError(f"PhysicalDrive{disk_number} not found")

    if disk.size < image_size:
        raise FlashError(
            f"target ({diskutil.human_size(disk.size)}) is smaller than "
            f"the image ({diskutil.human_size(image_size)})"
        )

    total_bytes = bmap.mapped_bytes if bmap else image_size
    total_ranges = len(bmap.ranges) if bmap else 1

    _log(f"Locking {len(disk.volumes)} volume(s) on PhysicalDrive{disk_number}...")

    locks = diskutil.VolumeLocks(disk.volumes)
    locks.acquire()

    try:
        try:
            handle = _open_disk_for_write(disk_number)
        except pywintypes.error as exc:
            raise FlashError(
                f"cannot open PhysicalDrive{disk_number} for writing: "
                f"{exc.strerror}. Run as Administrator and make sure no "
                "other program is using the disk."
            ) from exc

        try:
            with open(image_path, "rb", buffering=0) as image_file:
                bytes_written = 0

                if bmap:
                    for index, rng in enumerate(bmap.ranges):
                        bytes_written = _copy_span(
                            image_file,
                            handle,
                            rng.first * bmap.block_size,
                            rng.block_count * bmap.block_size,
                            rng.checksum if verify else None,
                            bmap.checksum_type,
                            bytes_written,
                            total_bytes,
                            index,
                            total_ranges,
                            progress,
                            cancel_event,
                        )
                else:
                    _copy_span(
                        image_file,
                        handle,
                        0,
                        image_size,
                        None,
                        "sha256",
                        0,
                        total_bytes,
                        0,
                        1,
                        progress,
                        cancel_event,
                    )

            win32file.FlushFileBuffers(handle)
        finally:
            handle.Close()
    finally:
        _log("Unlocking volumes...")
        locks.release()

    _log("Flash finished successfully.")
