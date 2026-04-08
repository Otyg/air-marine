from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models import NormalizedObservation, ScanBand
from app.radio_v2 import InprocBackend, LegacyBackend, MockBackend, ObservationEvent, ReaderBandSource


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "mock_radio"


class _FixtureReader:
    def __init__(self, observations: list[NormalizedObservation]) -> None:
        self._observations = list(observations)

    def read_observations(self, **kwargs):  # noqa: ANN003
        return list(self._observations)


def _load_fixture_observations(fixture_path: Path) -> dict[ScanBand, list[NormalizedObservation]]:
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    by_band: dict[ScanBand, list[NormalizedObservation]] = {}
    for row in payload.get("timeline", []):
        if row.get("event_type") != "observation":
            continue
        obs_payload = row.get("payload", {}).get("observation")
        if not isinstance(obs_payload, dict):
            continue
        band = ScanBand(str(row.get("band")))
        by_band.setdefault(band, []).append(NormalizedObservation.from_dict(obs_payload))
    return by_band


def _build_legacy_and_inproc(
    by_band: dict[ScanBand, list[NormalizedObservation]],
) -> tuple[LegacyBackend, InprocBackend]:
    readers = {band: _FixtureReader(observations) for band, observations in by_band.items()}
    legacy = LegacyBackend(readers)
    inproc = InprocBackend(readers, sources={band: ReaderBandSource(reader) for band, reader in readers.items()})
    legacy.start()
    inproc.start()
    return legacy, inproc


@pytest.mark.parametrize(
    "fixture_name,bands",
    [
        ("nominal_ais.json", [ScanBand.AIS]),
        ("nominal_adsb.json", [ScanBand.ADSB]),
        ("nominal_ogn.json", [ScanBand.OGN]),
        ("nominal_dsc.json", [ScanBand.DSC]),
        ("mixed_cycle.json", [ScanBand.AIS, ScanBand.ADSB, ScanBand.OGN, ScanBand.DSC]),
    ],
)
def test_parity_matrix_legacy_vs_inproc_vs_mock_deterministic(
    fixture_name: str,
    bands: list[ScanBand],
) -> None:
    fixture_path = FIXTURE_DIR / fixture_name
    by_band = _load_fixture_observations(fixture_path)
    legacy, inproc = _build_legacy_and_inproc(by_band)
    mock = MockBackend(fixture_path=fixture_path, enable_timing_mode=False)
    mock.start()

    for band in bands:
        legacy_events = legacy.read(0.2, band=band)
        inproc_events = inproc.read(0.2, band=band)
        mock_events = mock.read(0.2, band=band)

        assert [type(event).__name__ for event in legacy_events] == [type(event).__name__ for event in inproc_events]
        assert [type(event).__name__ for event in legacy_events] == [type(event).__name__ for event in mock_events]

        legacy_dicts = [event.observation.to_dict() for event in legacy_events if isinstance(event, ObservationEvent)]
        inproc_dicts = [event.observation.to_dict() for event in inproc_events if isinstance(event, ObservationEvent)]
        mock_dicts = [event.observation.to_dict() for event in mock_events if isinstance(event, ObservationEvent)]

        assert legacy_dicts == inproc_dicts
        assert legacy_dicts == mock_dicts


def test_parity_matrix_mock_timing_mode_preserves_key_fields() -> None:
    fixture_path = FIXTURE_DIR / "retune_mid_window.json"
    by_band = _load_fixture_observations(fixture_path)
    legacy, inproc = _build_legacy_and_inproc(by_band)
    mock = MockBackend(fixture_path=fixture_path, enable_timing_mode=True)
    mock.start()

    legacy_events = legacy.read(0.2, band=ScanBand.AIS)
    inproc_events = inproc.read(0.2, band=ScanBand.AIS)
    mock_events = mock.read(0.2, band=ScanBand.AIS)

    assert len(legacy_events) == 1
    assert len(inproc_events) == 1
    assert len(mock_events) == 1

    legacy_obs = legacy_events[0].observation  # type: ignore[attr-defined]
    inproc_obs = inproc_events[0].observation  # type: ignore[attr-defined]
    mock_obs = mock_events[0].observation  # type: ignore[attr-defined]

    # Timing mode may change delivery timing, but observation identity/content must hold.
    assert legacy_obs.target_id == inproc_obs.target_id == mock_obs.target_id
    assert legacy_obs.source == inproc_obs.source == mock_obs.source
    assert legacy_obs.kind == inproc_obs.kind == mock_obs.kind
    assert legacy_obs.lat == inproc_obs.lat == mock_obs.lat
    assert legacy_obs.lon == inproc_obs.lon == mock_obs.lon
