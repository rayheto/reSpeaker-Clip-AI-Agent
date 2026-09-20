"""Raw Opus packet parsing and Ogg Opus re-container tests."""

import struct
from pathlib import Path

import pytest

from backend.clip.ogg import (
    MAX_FRAME_LEN,
    MIN_FRAME_LEN,
    OpusFormatError,
    OggOpusWriter,
    convert_opus_to_ogg,
    convert_session_to_ogg,
    granule_delta_samples,
    ogg_crc32,
    opus_packet_duration_ms,
    parse_raw_opus_frames,
)


def packet(toc: int = 0x48, payload: bytes = b"\x00\x01\x02") -> bytes:
    """A [u16le len][frame] packet; 0x48 = SILK WB, 20 ms, one frame."""
    return struct.pack("<H", len(payload) + 1) + bytes([toc]) + payload


def session_dir(tmp_path: Path, sample_rate: int = 16000, channels: int = 1) -> Path:
    """A downloaded-session layout: session.json + NNNN.opus files."""
    sid_dir = tmp_path / "20260101000000"
    sid_dir.mkdir()
    (sid_dir / "session.json").write_text(
        '{"session_id":"20260101000000","sample_rate_hz":%d,"channels":%d,"mode":"enhanced"}\n'
        % (sample_rate, channels),
        encoding="utf-8",
    )
    return sid_dir


def frames(*counts: int) -> list[bytes]:
    """counts = number of packets per file."""
    out = []
    for n in counts:
        out.append(b"".join(packet(toc=0x40 + i % 2, payload=bytes([i % 7])) for i in range(n)))
    return out


class TestParseRawOpusFrames:
    def test_parses_flat_stream(self):
        raw = packet() + packet() + packet()
        parsed = parse_raw_opus_frames(raw)
        assert len(parsed) == 3
        assert parsed[0] == b"\x48\x00\x01\x02"

    def test_empty_input(self):
        assert parse_raw_opus_frames(b"") == []

    def test_corrupt_length_prefix_raises(self):
        with pytest.raises(OpusFormatError):
            parse_raw_opus_frames(b"\xff\xff\x00\x00\x00")

    def test_truncated_frame_payload_raises(self):
        raw = struct.pack("<H", 10) + b"\x48\x00\x01"  # declares 10 bytes, has 3
        with pytest.raises(OpusFormatError):
            parse_raw_opus_frames(raw)

    def test_truncated_length_prefix_raises(self):
        with pytest.raises(OpusFormatError):
            parse_raw_opus_frames(b"\x05")

    def test_legacy_header_tolerated_in_scan_mode(self):
        raw = b"LEGACY-HDR!" + b"".join(packet() for _ in range(2))
        # strict rejects the leading junk length
        with pytest.raises(OpusFormatError):
            parse_raw_opus_frames(raw, strict=True)
        parsed = parse_raw_opus_frames(raw, strict=False)
        assert len(parsed) == 2

    def test_ordered_packets_preserved(self):
        raw = packet(toc=0x48, payload=b"a") + packet(toc=0x49, payload=b"b")
        parsed = parse_raw_opus_frames(raw)
        assert parsed[0][-1] == ord("a")
        assert parsed[1][-1] == ord("b")


class TestOpusDuration:
    def test_toc_durations(self):
        assert opus_packet_duration_ms(0x00) == 10.0
        assert opus_packet_duration_ms(0x08) == 20.0
        assert opus_packet_duration_ms(0x18) == 60.0
        assert opus_packet_duration_ms(0x60) == 10.0  # hybrid config 12
        assert opus_packet_duration_ms(0x68) == 20.0  # hybrid config 13
        assert opus_packet_duration_ms(0x80) == 2.5   # CELT config 16
        assert opus_packet_duration_ms(0x98) == 20.0  # CELT config 19

    def test_packet_frame_count_affects_granule(self):
        assert granule_delta_samples(b"\x48payload") == 960
        assert granule_delta_samples(b"\x49payload") == 1920
        assert granule_delta_samples(b"\x4b\x03payload") == 2880


class TestOggWriter:
    def _read_pages(self, path: Path) -> list[dict]:
        data = path.read_bytes()
        pages = []
        offset = 0
        while offset + 27 <= len(data):
            assert data[offset : offset + 4] == b"OggS"
            header_type = data[offset + 5]
            granule = struct.unpack_from("<Q", data, offset + 6)[0]
            serial = struct.unpack_from("<I", data, offset + 14)[0]
            seg_count = data[offset + 26]
            seg_table = data[offset + 27 : offset + 27 + seg_count]
            body_len = sum(seg_table)
            body = data[offset + 27 + seg_count : offset + 27 + seg_count + body_len]
            raw_page = data[offset : offset + 27 + seg_count + body_len]
            stored_crc = struct.unpack_from("<I", raw_page, 22)[0]
            crc_input = bytearray(raw_page)
            struct.pack_into("<I", crc_input, 22, 0)
            assert ogg_crc32(bytes(crc_input)) == stored_crc
            pages.append(
                {
                    "type": header_type,
                    "granule": granule,
                    "serial": serial,
                    "body": body,
                }
            )
            offset += 27 + seg_count + body_len
        return pages

    def test_writes_valid_container_with_metadata(self, tmp_path):
        out = tmp_path / "out.ogg"
        with OggOpusWriter(out, sample_rate=16000, channels=1, serial=0xABCD) as w:
            w.write_header()
            w.write_packet(packet(toc=0x48)[2:])
            w.write_packet(packet(toc=0x48)[2:])

        pages = self._read_pages(out)
        assert pages[0]["type"] == 0x02
        assert pages[0]["body"].startswith(b"OpusHead")
        assert pages[1]["body"].startswith(b"OpusTags")
        # packet() returns [len][frame]; the writer consumes bare frames,
        # so strip the 2-byte length prefix.  0x40 = CELT 20 ms = 960 samples.
        assert pages[2]["granule"] == 960
        assert pages[3]["granule"] == 1920
        assert pages[-1]["type"] & 0x04  # EOS set on final page
        assert pages[0]["serial"] == pages[-1]["serial"]

    def test_channel_and_rate_metadata(self, tmp_path):
        out = tmp_path / "stereo.ogg"
        with OggOpusWriter(out, sample_rate=48000, channels=2) as w:
            w.write_header()
            w.write_packet(packet())
        pages = self._read_pages(out)
        head = pages[0]["body"]
        assert head[9] == 2  # channels
        assert struct.unpack_from("<I", head, 12)[0] == 48000

    def test_close_without_pages_is_error(self, tmp_path):
        out = tmp_path / "empty.ogg"
        with pytest.raises(OpusFormatError):
            with OggOpusWriter(out):
                pass  # nothing written -> invalid stream on close

    def test_header_only_close_marks_eos(self, tmp_path):
        out = tmp_path / "quiet.ogg"
        with OggOpusWriter(out) as w:
            w.write_header()
        pages = self._read_pages(out)
        assert pages[0]["type"] & 0x02
        assert pages[-1]["type"] & 0x04

    def test_write_rejects_empty_packet(self, tmp_path):
        with pytest.raises(OpusFormatError):
            with OggOpusWriter(tmp_path / "x.ogg") as w:
                w.write_header()
                w.write_packet(b"")


class TestConversions:
    def test_convert_session_joins_files_in_order(self, tmp_path):
        d = session_dir(tmp_path)
        (d / "0000.opus").write_bytes(b"".join(packet(payload=bytes([1])) for _ in range(2)))
        (d / "0001.opus").write_bytes(b"".join(packet(payload=bytes([2])) for _ in range(2)))
        (d / "0002.opus").write_bytes(b"".join(packet(payload=bytes([3])) for _ in range(2)))

        out = convert_session_to_ogg(d)
        assert out.exists()
        assert out.name == "20260101000000.ogg"
        data = out.read_bytes()
        assert data.startswith(b"OggS")
        assert b"OpusHead" in data
        # 6 packets total; page count = 1 header + 6 audio pages
        assert data.count(b"OggS") == 8

    def test_convert_session_missing_metadata_raises(self, tmp_path):
        d = tmp_path / "20260101000001"
        d.mkdir()
        (d / "0000.opus").write_bytes(b"")
        with pytest.raises(OpusFormatError):
            convert_session_to_ogg(d)

    def test_convert_session_corrupt_raises_and_keeps_inputs(self, tmp_path):
        d = session_dir(tmp_path)
        (d / "0000.opus").write_bytes(struct.pack("<H", 200) + b"\x40" + b"\x00" * 5)
        with pytest.raises(OpusFormatError):
            convert_session_to_ogg(d)
        assert (d / "0000.opus").exists()  # artifacts retained for diagnosis

    def test_convert_opus_to_ogg_single_file(self, tmp_path):
        src = tmp_path / "0000.opus"
        src.write_bytes(b"".join(packet() for _ in range(3)))
        out = tmp_path / "single.ogg"
        result = convert_opus_to_ogg(src, out, sample_rate=16000, channels=1)
        assert result == out
        assert out.read_bytes().count(b"OggS") == 2 + 3

# ---------------------------------------------------------------------------
# In-memory RTC utterance snapshots
# ---------------------------------------------------------------------------


def test_convert_frames_to_ogg_bytes_builds_valid_ogg():
    from backend.clip.ogg import (
        OggOpusWriter,
        OpusFormatError,
        convert_frames_to_ogg_bytes,
    )

    frames = [b"\xf8\x01\x02\x03\x04"] * 4
    data = convert_frames_to_ogg_bytes(frames, sample_rate=16000, channels=1)
    assert data.startswith(b"OggS")
    assert data.count(b"OggS") >= 3  # BOS + tags + audio pages
    assert b"OpusHead" in data
    # The last page carries the EOS flag (header byte 5 bit 0x04).
    last_page = data.rfind(b"OggS")
    assert data[last_page + 5] & 0x04

    # Header-only metadata is stable across page serials (deterministic).
    again = convert_frames_to_ogg_bytes(frames, sample_rate=16000, channels=1)
    assert again == data


def test_convert_frames_to_ogg_bytes_rejects_empty():
    from backend.clip.ogg import OpusFormatError, convert_frames_to_ogg_bytes

    with pytest.raises(OpusFormatError):
        convert_frames_to_ogg_bytes([])
    with pytest.raises(OpusFormatError):
        convert_frames_to_ogg_bytes([b""])
