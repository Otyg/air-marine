from __future__ import annotations

from app.ingest_ais_inproc import AISInprocReader
from app.models import NormalizedObservation, ScanBand, Source, TargetKind


def test_ais_inproc_reader_uses_dsp_decoder_lines(monkeypatch) -> None:
    line = "!AIVDM,1,1,,A,15N?;P001oG?VtN@E>4QwvN20<0H,0*14"
    expected = NormalizedObservation(
        target_id="ais:265123456",
        source=Source.AIS,
        kind=TargetKind.VESSEL,
        observed_at=NormalizedObservation.from_dict(
            {
                "target_id": "ais:265123456",
                "source": "ais",
                "kind": "vessel",
                "observed_at": "2026-04-08T00:00:00+00:00",
                "last_scan_band": "ais",
            }
        ).observed_at,
        last_scan_band=ScanBand.AIS,
        mmsi="265123456",
    )

    class _Client:
        def connect(self) -> bool:
            return True

        def read_samples(self, num_samples: int) -> bytes:  # noqa: ARG002
            return b"\x80\x80\x80\x80"

        def close(self) -> None:
            return None

        def retune(self, frequency_hz: int) -> None:  # noqa: ARG002
            return None

        def set_gain(self, gain_db: int) -> None:  # noqa: ARG002
            return None

    class _DSP:
        def decode_ais_nmea_lines(self, iq_data: bytes, sample_rate: int):  # noqa: ANN001, ARG002
            return [line]

    monkeypatch.setattr("app.ingest_ais_inproc.parse_ais_nmea_lines", lambda lines: [expected])  # noqa: ARG005

    reader = AISInprocReader(client=_Client(), dsp_backend=_DSP())
    observations = reader.read_observations(timeout_seconds=0.01, num_samples=4)

    assert len(observations) == 1
    assert observations[0].target_id == "ais:265123456"


def test_ais_inproc_reader_returns_empty_without_decoder_support() -> None:
    class _Client:
        def connect(self) -> bool:
            return True

        def read_samples(self, num_samples: int) -> bytes:  # noqa: ARG002
            return b"\x80\x80\x80\x80"

        def close(self) -> None:
            return None

        def retune(self, frequency_hz: int) -> None:  # noqa: ARG002
            return None

        def set_gain(self, gain_db: int) -> None:  # noqa: ARG002
            return None

    class _DSP:
        pass

    reader = AISInprocReader(client=_Client(), dsp_backend=_DSP())
    observations = reader.read_observations(timeout_seconds=0.01, num_samples=4)

    assert observations == []


def test_ais_inproc_reader_retune_and_gain_forward_to_client() -> None:
    class _Client:
        def __init__(self) -> None:
            self.retunes: list[int] = []
            self.gains: list[int] = []

        def connect(self) -> bool:
            return False

        def read_samples(self, num_samples: int) -> bytes:  # noqa: ARG002
            return b""

        def close(self) -> None:
            return None

        def retune(self, frequency_hz: int) -> None:
            self.retunes.append(frequency_hz)

        def set_gain(self, gain_db: int) -> None:
            self.gains.append(gain_db)

    class _DSP:
        def decode_ais_nmea_lines(self, iq_data: bytes, sample_rate: int):  # noqa: ANN001, ARG002
            return []

    client = _Client()
    reader = AISInprocReader(client=client, dsp_backend=_DSP())
    reader.retune(162_025_000)
    reader.set_gain(25)

    assert client.retunes == [162_025_000]
    assert client.gains == [25]
