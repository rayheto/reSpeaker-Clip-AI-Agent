"""Project-local raw Opus -> Ogg Opus re-container for reSpeaker Clip.

Downloaded ``NNNN.opus`` files from a Clip session contain a flat stream of
``[uint16 little-endian length][raw Opus frame]`` packets, **not** Ogg.  Before
sending audio to Groq STT we join every packet of a session, in file order,
into one valid Ogg Opus container using the session metadata
(``sample_rate_hz`` / ``channels``) that the SDK writes to ``session.json``.

This module is intentionally dependency-free and self-contained; it never
imports the legacy ``applications/clip`` utilities.
"""

from __future__ import annotations

import io
import json
import struct
from typing import Any
from collections.abc import Iterator
from pathlib import Path

# -- Opus frame bounds ------------------------------------------------------
# A real Opus packet is at least 1 byte (empty TOC-without-payload is invalid)
# and at most a few KB.  Anything larger is corrupt or not packetized data.
MIN_FRAME_LEN = 1
MAX_FRAME_LEN = 4096
# Legacy recordings sometimes carry a tiny opaque header before the first
# packet; scan this many bytes for a plausible packet start.
SCAN_LIMIT = 256

_DEFAULT_PRE_SKIP = 312  # 6.5ms at 48 kHz, the canonical Opus pre-roll


class OpusFormatError(ValueError):
    """The raw Opus payload is corrupt, truncated, or not packetized."""


# -- Ogg CRC (RFC 3533 polynomial 0x04C11DB7) --------------------------------

def _build_ogg_crc_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
        table.append(crc)
    return table


_OGG_CRC_TABLE = _build_ogg_crc_table()


def ogg_crc32(data: bytes) -> int:
    """Ogg page CRC32 (different polynomial from zlib's)."""
    crc = 0
    for byte in data:
        index = ((crc >> 24) ^ byte) & 0xFF
        crc = ((crc << 8) ^ _OGG_CRC_TABLE[index]) & 0xFFFFFFFF
    return crc


# -- Raw Opus frame parsing ---------------------------------------------------

def _frame_length(raw: bytes, offset: int) -> int:
    if offset + 2 > len(raw):
        raise OpusFormatError("truncated Opus length prefix")
    return struct.unpack_from("<H", raw, offset)[0]


def _scan_frame_start(raw: bytes) -> int:
    """Find the first plausible packet start (legacy header tolerance)."""
    limit = min(SCAN_LIMIT, max(0, len(raw) - 2))
    for offset in range(0, limit + 1):
        flen = _frame_length(raw, offset)
        if MIN_FRAME_LEN <= flen <= MAX_FRAME_LEN and offset + 2 + flen <= len(raw):
            return offset
    return -1


def parse_raw_opus_frames(raw: bytes, *, strict: bool = True) -> list[bytes]:
    """Parse ``[u16le length][frame]`` packets into a list of Opus frames.

    ``strict=True`` requires the very first length prefix to be a plausible
    packet.  ``strict=False`` additionally tolerates a small opaque legacy
    header by scanning for the first plausible packet start.
    """
    if not raw:
        return []
    offset = 0
    first_len = _frame_length(raw, 0)
    if not (MIN_FRAME_LEN <= first_len <= MAX_FRAME_LEN):
        if strict:
            raise OpusFormatError(
                f"invalid leading Opus frame length {first_len}"
            )
        offset = _scan_frame_start(raw)
        if offset < 0:
            raise OpusFormatError("no valid Opus frame start found")

    frames: list[bytes] = []
    while offset < len(raw):
        flen = _frame_length(raw, offset)
        offset += 2
        if flen < MIN_FRAME_LEN or flen > MAX_FRAME_LEN:
            raise OpusFormatError(f"invalid Opus frame length {flen}")
        if offset + flen > len(raw):
            raise OpusFormatError("truncated Opus frame payload")
        frames.append(raw[offset : offset + flen])
        offset += flen
    return frames


def iter_raw_opus_frames(path: str | Path) -> Iterator[bytes]:
    """Stream device packets from disk without buffering a whole recording."""
    with Path(path).open("rb") as handle:
        while True:
            prefix = handle.read(2)
            if not prefix:
                return
            if len(prefix) != 2:
                raise OpusFormatError("truncated Opus length prefix")
            frame_len = struct.unpack("<H", prefix)[0]
            if frame_len < MIN_FRAME_LEN or frame_len > MAX_FRAME_LEN:
                raise OpusFormatError(f"invalid Opus frame length {frame_len}")
            frame = handle.read(frame_len)
            if len(frame) != frame_len:
                raise OpusFormatError("truncated Opus frame payload")
            yield frame


# -- Opus packet duration -------------------------------------------------------

def opus_packet_duration_ms(toc: int) -> float:
    """Duration of one Opus frame from its TOC config (RFC 6716 table 2)."""
    config = (toc >> 3) & 0x1F
    if config < 12:      # SILK: NB/MB/WB, 10/20/40/60 ms
        return (10.0, 20.0, 40.0, 60.0)[config & 0x03]
    if config < 16:      # Hybrid: SWB/FB, 10/20 ms
        return (10.0, 20.0)[config & 0x01]
    # CELT: NB/WB/SWB/FB, 2.5/5/10/20 ms
    return (2.5, 5.0, 10.0, 20.0)[config & 0x03]


_OPUS_INTERNAL_RATE = 48000


def granule_delta_samples(frame: bytes) -> int:
    if not frame:
        return int(round(20.0 * _OPUS_INTERNAL_RATE / 1000))
    toc = frame[0]
    frame_code = toc & 0x03
    if frame_code == 0:
        frame_count = 1
    elif frame_code in (1, 2):
        frame_count = 2
    else:
        if len(frame) < 2:
            raise OpusFormatError("truncated Opus frame-count byte")
        frame_count = frame[1] & 0x3F
        if frame_count == 0:
            raise OpusFormatError("invalid Opus frame count 0")
    duration_ms = opus_packet_duration_ms(toc) * frame_count
    if duration_ms > 120:
        raise OpusFormatError("Opus packet duration exceeds 120 ms")
    return max(1, int(round(duration_ms * _OPUS_INTERNAL_RATE / 1000)))


# -- Ogg Opus writer -----------------------------------------------------------

class OggOpusWriter:
    """Writes a valid Ogg Opus file from Opus packets, page per packet.

    OpusHead and OpusTags are written on separate pages as required by the Ogg
    Opus mapping; the final audio page is patched with EOS on ``close``.
    """

    def __init__(
        self,
        path: str | Path | None,
        *,
        stream: Any | None = None,
        sample_rate: int = 16000,
        channels: int = 1,
        serial: int = 0x12345678,
        vendor: str = "reSpeaker Clip AI Agent",
    ) -> None:
        """Write to ``path`` or, when ``stream`` is given, to a binary stream.

        The stream variant is used for in-memory RTC utterance snapshots
        (``convert_frames_to_ogg_bytes``) so rolling partials never touch disk.
        """
        self.path = Path(path) if path is not None else None
        self._owns_file = stream is None
        if stream is not None:
            self._file = stream
        else:
            assert self.path is not None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("wb")
        self.sample_rate = sample_rate
        self.channels = channels
        self.serial = serial
        self.vendor = vendor
        self._page_seq = 0
        self._granule = 0
        self._page_count = 0
        self._last_page_start = 0
        self._last_page_data = b""

    # -- packet helpers ---------------------------------------------------

    @staticmethod
    def _lace(packets: list[bytes]) -> tuple[bytes, bytes]:
        segments: list[int] = []
        payload = bytearray()
        for packet in packets:
            size = len(packet)
            if size == 0:
                raise OpusFormatError("cannot write an empty Opus packet")
            while size >= 255:
                segments.append(255)
                size -= 255
            segments.append(size)
            payload.extend(packet)
        if not segments:
            segments.append(0)
        return bytes(segments), bytes(payload)

    # -- page writing ------------------------------------------------------

    def _write_page(
        self,
        packets: list[bytes],
        *,
        header_type: int,
        granule: int,
    ) -> None:
        segments, payload = self._lace(packets)
        header = bytearray()
        header.extend(b"OggS")
        header.append(0)                      # stream structure version
        header.append(header_type)            # BOS / continuation / EOS
        header.extend(struct.pack("<Q", granule))
        header.extend(struct.pack("<I", self.serial))
        header.extend(struct.pack("<I", self._page_seq))
        header.extend(struct.pack("<I", 0))   # CRC placeholder
        header.append(len(segments))
        header.extend(segments)

        page = bytes(header) + payload
        crc = ogg_crc32(page)
        struct.pack_into("<I", header, 22, crc)
        page = bytes(header) + payload

        self._last_page_start = self._file.tell()
        self._file.write(page)
        self._page_seq += 1
        self._page_count += 1
        self._last_page_data = page

    def _patch_last_page_eos(self) -> None:
        """Re-write the last page with the EOS flag and corrected CRC."""
        page = self._last_page_data
        if not page or page[0:4] != b"OggS":
            return
        header = bytearray(page[:27])
        header[5] = header[5] | 0x04  # set EOS
        struct.pack_into("<I", header, 22, 0)
        rebuilt = bytes(header) + page[27:]
        crc = ogg_crc32(rebuilt)
        struct.pack_into("<I", header, 22, crc)
        rebuilt = bytes(header) + page[27:]
        self._file.seek(self._last_page_start)
        self._file.write(rebuilt)
        self._file.flush()

    # -- stream ------------------------------------------------------------

    def write_header(self) -> None:
        opus_head = bytearray()
        opus_head.extend(b"OpusHead")
        opus_head.append(1)                                   # version
        opus_head.append(self.channels & 0xFF)                # channels
        opus_head.extend(struct.pack("<H", _DEFAULT_PRE_SKIP))
        opus_head.extend(struct.pack("<I", self.sample_rate))
        opus_head.extend(struct.pack("<h", 0))                # output gain
        opus_head.append(0)                                   # mapping family

        vendor = self.vendor.encode("utf-8")
        opus_tags = bytearray()
        opus_tags.extend(b"OpusTags")
        opus_tags.extend(struct.pack("<I", len(vendor)))
        opus_tags.extend(vendor)
        opus_tags.extend(struct.pack("<I", 0))                # no comments

        self._write_page([bytes(opus_head)], header_type=0x02, granule=0)
        self._write_page([bytes(opus_tags)], header_type=0x00, granule=0)

    def write_packet(self, frame: bytes, granule_delta: int | None = None) -> None:
        if not frame:
            raise OpusFormatError("cannot write an empty Opus packet")
        if granule_delta is None:
            granule_delta = granule_delta_samples(frame)
        self._granule += max(1, int(granule_delta))
        self._write_page([frame], header_type=0x00, granule=self._granule)

    def close(self) -> None:
        if self._page_count == 0:
            # Header-only stream: still mark the BOS page EOS.
            raise OpusFormatError("no pages were written")
        self._patch_last_page_eos()
        if self._owns_file:
            self._file.close()

    def __enter__(self) -> "OggOpusWriter":
        return self

    def __exit__(self, exc_type: object, *_exc: object) -> None:
        if exc_type is None:
            self.close()
        elif self._owns_file:
            self._file.close()


# -- Orchestration ------------------------------------------------------------

def convert_opus_to_ogg(
    input_path: str | Path,
    output_path: str | Path,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
) -> Path:
    """Convert a single ``[u16le len][opus frame]`` file into Ogg Opus."""
    out = Path(output_path)
    frame_count = 0
    with OggOpusWriter(out, sample_rate=sample_rate, channels=channels) as writer:
        writer.write_header()
        for frame in iter_raw_opus_frames(input_path):
            writer.write_packet(frame)
            frame_count += 1
        if frame_count == 0:
            raise OpusFormatError(f"no Opus frames in {input_path}")
    return out


def convert_frames_to_ogg_bytes(
    frames: list[bytes] | tuple[bytes, ...],
    *,
    sample_rate: int = 16000,
    channels: int = 1,
) -> bytes:
    """Re-container an in-memory list of raw Opus packets into Ogg Opus bytes.

    Used for bounded RTC utterance snapshots (partial + final STT). Raises
    :class:`OpusFormatError` when no usable frame is present.
    """
    if not frames:
        raise OpusFormatError("no Opus frames for RTC snapshot")
    buffer = io.BytesIO()
    frame_count = 0
    with OggOpusWriter(
        None, stream=buffer, sample_rate=sample_rate, channels=channels
    ) as writer:
        writer.write_header()
        for frame in frames:
            if not frame:
                continue
            writer.write_packet(frame)
            frame_count += 1
        if frame_count == 0:
            raise OpusFormatError("no Opus frames for RTC snapshot")
    return buffer.getvalue()


def convert_session_to_ogg(session_dir: str | Path) -> Path:
    """Re-container every ``NNNN.opus`` file of a downloaded session in order.

    Session metadata (``sample_rate_hz``, ``channels``) is read from the
    ``session.json`` the SDK writes before a transfer starts.  The output file
    is written next to the session directory as ``<session_id>.ogg``.
    """
    session_dir = Path(session_dir)
    if not session_dir.is_dir():
        raise OpusFormatError(f"session directory not found: {session_dir}")

    meta_path = session_dir / "session.json"
    if not meta_path.exists():
        raise OpusFormatError(f"missing session metadata: {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    try:
        sample_rate = int(meta.get("sample_rate_hz") or 16000)
        channels = int(meta.get("channels") or 1)
    except (TypeError, ValueError) as exc:
        raise OpusFormatError(f"invalid session metadata: {exc}") from exc
    if channels not in (1, 2):
        raise OpusFormatError(f"unsupported channel count {channels}")

    opus_files = sorted(
        (p for p in session_dir.glob("*.opus")),
        key=lambda p: int(p.stem) if p.stem.isdigit() else p.name,
    )
    if not opus_files:
        raise OpusFormatError(f"no .opus files in {session_dir}")

    output_path = session_dir.parent / f"{session_dir.name}.ogg"
    frame_count = 0
    with OggOpusWriter(
        output_path, sample_rate=sample_rate, channels=channels
    ) as writer:
        writer.write_header()
        for path in opus_files:
            for frame in iter_raw_opus_frames(path):
                writer.write_packet(frame)
                frame_count += 1
        if frame_count == 0:
            raise OpusFormatError(f"no Opus frames found for {session_dir}")
    return output_path
