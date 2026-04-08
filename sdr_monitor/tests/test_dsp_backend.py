from __future__ import annotations

from app.dsp_backend import DSPBackend


def test_dsp_backend_falls_back_to_python_when_no_extension() -> None:
    backend = DSPBackend()
    info = backend.info()

    assert info.name in {"python", "cpp"}
    if info.name == "python":
        assert info.accelerated is False


def test_dsp_backend_demodulate_is_callable() -> None:
    backend = DSPBackend()
    data = bytes([1, 2, 3, 4])
    output = backend.demodulate(data)

    assert isinstance(output, bytes)
    assert len(output) == len(data)
