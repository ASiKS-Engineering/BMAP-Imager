"""Parser for .bmap block-map files.

Real-world .bmap files (produced by bmaptool / mender-artifact / custom
tooling) vary in tag naming (hyphenated vs. CamelCase). Tag names are
normalized before matching so both styles are accepted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import xml.etree.ElementTree as ET


class BmapError(Exception):
    """Raised when a .bmap file cannot be parsed."""


@dataclass
class BmapRange:
    first: int
    last: int
    checksum: str | None = None

    @property
    def block_count(self) -> int:
        return self.last - self.first + 1


@dataclass
class Bmap:
    image_size: int
    block_size: int
    checksum_type: str
    ranges: list[BmapRange] = field(default_factory=list)

    @property
    def mapped_blocks(self) -> int:
        return sum(r.block_count for r in self.ranges)

    @property
    def mapped_bytes(self) -> int:
        return self.mapped_blocks * self.block_size


def _normalize(tag: str) -> str:
    # Strip XML namespace ("{ns}tag" -> "tag") and punctuation.
    return tag.rsplit("}", 1)[-1].lower().replace("-", "").replace("_", "")


def _find_text(root: ET.Element, name: str) -> str | None:
    wanted = _normalize(name)
    for child in root.iter():
        if _normalize(child.tag) == wanted and child.text:
            return child.text.strip()
    return None


def parse_bmap(path: str) -> Bmap:
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise BmapError(f"invalid XML: {exc}") from exc

    root = tree.getroot()

    image_size_text = _find_text(root, "image-size")
    block_size_text = _find_text(root, "block-size")
    checksum_type = _find_text(root, "checksum-type") or "sha256"

    if image_size_text is None or not image_size_text.isdigit():
        raise BmapError("missing or invalid <image-size>")

    if block_size_text is None or not block_size_text.isdigit():
        raise BmapError("missing or invalid <block-size>")

    image_size = int(image_size_text)
    block_size = int(block_size_text)

    if block_size == 0:
        raise BmapError("block size must not be zero")

    ranges: list[BmapRange] = []

    for element in root.iter():
        if _normalize(element.tag) != "range" or not element.text:
            continue

        text = element.text.strip()

        checksum = None
        for key, value in element.attrib.items():
            if _normalize(key) in ("chksum", "checksum"):
                checksum = value.strip().lower()

        if "-" in text:
            first_text, _, last_text = text.partition("-")
        else:
            first_text = last_text = text

        try:
            first = int(first_text)
            last = int(last_text)
        except ValueError as exc:
            raise BmapError(f"invalid <Range> value '{text}'") from exc

        if first > last:
            raise BmapError(f"invalid <Range> value '{text}'")

        ranges.append(BmapRange(first, last, checksum))

    if not ranges:
        raise BmapError("no <Range> entries found")

    ranges.sort(key=lambda r: r.first)

    return Bmap(
        image_size=image_size,
        block_size=block_size,
        checksum_type=checksum_type,
        ranges=ranges,
    )
