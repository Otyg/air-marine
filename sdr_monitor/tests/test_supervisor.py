from __future__ import annotations

from app.models import ScanBand
from app.supervisor import DecoderProcessConfig, DecoderSupervisor, ProcessSupervisor


class FakeProcess:
    _pid = 1000

    def __init__(self) -> None:
        FakeProcess._pid += 1
        self.pid = FakeProcess._pid
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):  # noqa: ARG002
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class FakePopenFactory:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.instances: list[FakeProcess] = []

    def __call__(self, command, **kwargs):  # noqa: ANN001, ARG002
        self.calls.append(list(command))
        proc = FakeProcess()
        self.instances.append(proc)
        return proc


def test_process_supervisor_start_and_stop() -> None:
    popen = FakePopenFactory()
    supervisor = ProcessSupervisor(popen_factory=popen)

    supervisor.start(name="adsb", command=("readsb", "--quiet"))
    assert supervisor.is_running()
    assert supervisor.active_name == "adsb"
    assert popen.calls == [["readsb", "--quiet"]]

    supervisor.stop()
    assert not supervisor.is_running()
    assert supervisor.active_name is None
    assert popen.instances[0].terminated


def test_decoder_supervisor_switches_between_bands() -> None:
    popen = FakePopenFactory()
    process_supervisor = ProcessSupervisor(popen_factory=popen)
    decoder_supervisor = DecoderSupervisor(
        config=DecoderProcessConfig(
            adsb_command=("readsb",),
            ais_command=("rtl_ais",),
        ),
        process_supervisor=process_supervisor,
    )

    decoder_supervisor.switch_to(ScanBand.ADSB)
    assert decoder_supervisor.active_band == ScanBand.ADSB
    decoder_supervisor.switch_to(ScanBand.AIS)
    assert decoder_supervisor.active_band == ScanBand.AIS
    assert popen.calls == [["readsb"], ["rtl_ais"]]
    assert popen.instances[0].terminated

    decoder_supervisor.stop_active()
    assert decoder_supervisor.active_band is None


def test_decoder_supervisor_allows_ogn_band_without_process_command() -> None:
    popen = FakePopenFactory()
    process_supervisor = ProcessSupervisor(popen_factory=popen)
    decoder_supervisor = DecoderSupervisor(
        config=DecoderProcessConfig(
            adsb_command=("readsb",),
            ogn_command=None,
            ais_command=("rtl_ais",),
        ),
        process_supervisor=process_supervisor,
    )

    decoder_supervisor.switch_to(ScanBand.ADSB)
    decoder_supervisor.switch_to(ScanBand.OGN)

    assert popen.calls == [["readsb"]]
    assert decoder_supervisor.active_band is None
