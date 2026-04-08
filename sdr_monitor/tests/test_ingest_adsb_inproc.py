from __future__ import annotations

from datetime import timezone

from app.ingest_adsb_inproc import ADSBInprocReader, parse_modes_message_to_observation


VALID_MSG = "8D4840D6202CC371C32CE0576098"


def test_parse_modes_message_to_observation_extracts_adsb_fields() -> None:
    observation = parse_modes_message_to_observation(VALID_MSG)

    assert observation is not None
    assert observation.target_id == "adsb:4840d6"
    assert observation.icao24 == "4840d6"
    assert observation.source.value == "adsb"
    assert observation.kind.value == "aircraft"
    assert observation.last_scan_band.value == "adsb"
    assert observation.payload_json["df"] == 17
    assert observation.payload_json["raw_hex"] == VALID_MSG
    assert observation.observed_at.tzinfo == timezone.utc


def test_parse_modes_message_to_observation_rejects_non_extended_squitter() -> None:
    assert parse_modes_message_to_observation("0000000000000000000000000000") is None
    assert parse_modes_message_to_observation("8D4840D6202CC371C32CE05760") is None


def test_adsb_inproc_reader_deduplicates_observations() -> None:
    reader = ADSBInprocReader()

    class _Client:
        def connect(self) -> bool:
            return True

        def read_samples(self, num_samples: int) -> bytes:  # noqa: ARG002
            return b"\x80\x80\x80\x80"

        def close(self) -> None:
            return None

    class _Decoder:
        def feed_iq(self, iq_data: bytes) -> list[str]:  # noqa: ARG002
            return [VALID_MSG, VALID_MSG, "not-a-message"]

    reader._client = _Client()
    reader._decoder = _Decoder()

    observations = reader.read_observations(timeout_seconds=0.01, num_samples=4)

    assert len(observations) == 1
    assert observations[0].target_id == "adsb:4840d6"


def test_adsb_inproc_reader_returns_empty_when_connect_fails() -> None:
    reader = ADSBInprocReader()

    class _Client:
        def connect(self) -> bool:
            return False

        def close(self) -> None:
            return None

    reader._client = _Client()
    observations = reader.read_observations(timeout_seconds=0.01)
    assert observations == []


def test_adsb_inproc_reader_exposes_retune_and_gain_to_client() -> None:
    reader = ADSBInprocReader()

    class _Client:
        def __init__(self) -> None:
            self.retunes: list[int] = []
            self.gains: list[int] = []

        def retune(self, frequency_hz: int) -> None:
            self.retunes.append(frequency_hz)

        def set_gain(self, gain_db: int) -> None:
            self.gains.append(gain_db)

    client = _Client()
    reader._client = client

    reader.retune(1_090_000_000)
    reader.set_gain(28)

    assert client.retunes == [1_090_000_000]
    assert client.gains == [28]
