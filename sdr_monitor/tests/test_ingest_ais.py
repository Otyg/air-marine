from __future__ import annotations

from io import StringIO
from datetime import datetime, timezone

from app.ingest_ais import parse_ais_nmea_lines, parse_ais_sentence, read_ais_lines_from_tcp
from app.models import ScanBand, Source, TargetKind

AIS_TEXT_ALPHABET = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"
AIS_TEXT_INDEX = {char: idx for idx, char in enumerate(AIS_TEXT_ALPHABET)}


def test_parse_ais_nmea_lines_decodes_type1_position_report() -> None:
    payload, fill_bits = _build_type1_payload(
        mmsi=265123456,
        lat=59.334,
        lon=18.063,
        sog_knots=12.3,
        cog_degrees=90.0,
        nav_status=5,
    )
    line = _make_sentence(payload=payload, fill_bits=fill_bits)
    observed_at = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)

    observations = parse_ais_nmea_lines([line], observed_at=observed_at)
    assert len(observations) == 1

    obs = observations[0]
    assert obs.target_id == "ais:265123456"
    assert obs.source == Source.AIS
    assert obs.kind == TargetKind.VESSEL
    assert obs.last_scan_band == ScanBand.AIS
    assert obs.mmsi == "265123456"
    assert obs.nav_status == "moored"
    assert obs.speed == 12.3
    assert obs.course == 90.0
    assert round(obs.lat or 0, 3) == 59.334
    assert round(obs.lon or 0, 3) == 18.063
    assert obs.observed_at == observed_at


def test_parse_ais_nmea_lines_assembles_multipart_type5() -> None:
    payload, fill_bits = _build_type5_payload(
        mmsi=265777333,
        shipname="AMALIA",
    )
    split_index = len(payload) // 2
    part1 = payload[:split_index]
    part2 = payload[split_index:]
    line1 = _make_sentence(
        payload=part1,
        fill_bits=0,
        total_fragments=2,
        fragment_number=1,
        sequence_id="42",
    )
    line2 = _make_sentence(
        payload=part2,
        fill_bits=fill_bits,
        total_fragments=2,
        fragment_number=2,
        sequence_id="42",
    )

    observations = parse_ais_nmea_lines([line1, line2])
    assert len(observations) == 1
    obs = observations[0]
    assert obs.target_id == "ais:265777333"
    assert obs.shipname == "AMALIA"
    assert obs.label == "AMALIA"
    assert obs.lat is None
    assert obs.lon is None


def test_parse_ais_nmea_lines_handles_incomplete_multipart() -> None:
    payload, _fill_bits = _build_type5_payload(mmsi=265555111, shipname="NORDIC")
    line1 = _make_sentence(
        payload=payload[: len(payload) // 2],
        fill_bits=0,
        total_fragments=2,
        fragment_number=1,
        sequence_id="77",
    )

    observations = parse_ais_nmea_lines([line1])
    assert observations == []


def test_parse_ais_sentence_rejects_invalid_checksum() -> None:
    payload, fill_bits = _build_type1_payload(
        mmsi=123456789,
        lat=10.0,
        lon=20.0,
        sog_knots=1.0,
        cog_degrees=45.0,
        nav_status=0,
    )
    line = _make_sentence(payload=payload, fill_bits=fill_bits)
    tampered = f"{line[:-2]}00"
    assert parse_ais_sentence(tampered) is None


def test_read_ais_lines_from_tcp_reads_lines(monkeypatch) -> None:
    payload, fill_bits = _build_type1_payload(
        mmsi=265123456,
        lat=59.334,
        lon=18.063,
        sog_knots=12.3,
        cog_degrees=90.0,
        nav_status=0,
    )
    line1 = _make_sentence(payload=payload, fill_bits=fill_bits)
    line2 = _make_sentence(payload=payload, fill_bits=fill_bits)

    class FakeSocket:
        def __init__(self, content: str) -> None:
            self._stream = StringIO(content)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            self._stream.close()

        def settimeout(self, timeout: float) -> None:  # noqa: ARG002
            return None

        def makefile(self, mode: str, encoding: str, errors: str):  # noqa: ARG002
            return self._stream

    def _fake_create_connection(addr, timeout):  # noqa: ARG001
        return FakeSocket(f"{line1}\n{line2}\n")

    monkeypatch.setattr("app.ingest_ais.socket.create_connection", _fake_create_connection)
    lines = read_ais_lines_from_tcp(host="127.0.0.1", port=10110, timeout_seconds=1.0, max_lines=10)
    assert lines == [line1, line2]


def _build_type1_payload(
    *,
    mmsi: int,
    lat: float,
    lon: float,
    sog_knots: float,
    cog_degrees: float,
    nav_status: int,
) -> tuple[str, int]:
    bits = ["0"] * 168
    _set_u(bits, 0, 6, 1)
    _set_u(bits, 8, 30, mmsi)
    _set_u(bits, 38, 4, nav_status)
    _set_u(bits, 50, 10, int(round(sog_knots * 10)))
    _set_s(bits, 61, 28, int(round(lon * 600000)))
    _set_s(bits, 89, 27, int(round(lat * 600000)))
    _set_u(bits, 116, 12, int(round(cog_degrees * 10)))
    _set_u(bits, 128, 9, int(round(cog_degrees)))
    return _encode_bits_to_payload("".join(bits))


def _build_type5_payload(*, mmsi: int, shipname: str) -> tuple[str, int]:
    bits = ["0"] * 424
    _set_u(bits, 0, 6, 5)
    _set_u(bits, 8, 30, mmsi)
    _set_text(bits, 112, 20, shipname)
    return _encode_bits_to_payload("".join(bits))


def _set_u(bits: list[str], offset: int, length: int, value: int) -> None:
    encoded = f"{value:0{length}b}"[-length:]
    bits[offset : offset + length] = list(encoded)


def _set_s(bits: list[str], offset: int, length: int, value: int) -> None:
    if value < 0:
        value = (1 << length) + value
    _set_u(bits, offset, length, value)


def _set_text(bits: list[str], offset: int, char_count: int, text: str) -> None:
    normalized = (text.upper()[:char_count]).ljust(char_count, "@")
    for idx, char in enumerate(normalized):
        value = AIS_TEXT_INDEX.get(char, AIS_TEXT_INDEX["@"])
        _set_u(bits, offset + (idx * 6), 6, value)


def _encode_bits_to_payload(bits: str) -> tuple[str, int]:
    fill_bits = (6 - (len(bits) % 6)) % 6
    padded = bits + ("0" * fill_bits)
    payload_chars: list[str] = []
    for idx in range(0, len(padded), 6):
        value = int(padded[idx : idx + 6], 2)
        payload_chars.append(_sixbit_to_char(value))
    return "".join(payload_chars), fill_bits


def _sixbit_to_char(value: int) -> str:
    if value < 40:
        return chr(value + 48)
    return chr(value + 56)


def _make_sentence(
    *,
    payload: str,
    fill_bits: int,
    total_fragments: int = 1,
    fragment_number: int = 1,
    sequence_id: str = "",
    channel: str = "A",
    talker: str = "AIVDM",
) -> str:
    body = (
        f"!{talker},{total_fragments},{fragment_number},"
        f"{sequence_id},{channel},{payload},{fill_bits}"
    )
    checksum = 0
    for char in body[1:]:
        checksum ^= ord(char)
    return f"{body}*{checksum:02X}"
