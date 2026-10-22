"""El worker de loopback mezcla todas las salidas, no solo la de por defecto.

Motivo: con Teams sonando por un altavoz distinto al predeterminado de Windows
(p. ej. un Jabra), el loopback de la salida por defecto grababa silencio y la
reunion se descartaba con "Loopback descartado (peak bajo)".
"""
import numpy as np
import pytest

import wasapi_loopback_worker as w

pytestmark = pytest.mark.unit


def test_mix_suma_pistas_y_rellena_las_cortas():
    a = np.full(4, 0.2, dtype=np.float32)
    b = np.full(2, 0.3, dtype=np.float32)
    out = w.mix_blocks([a, b], 4)
    assert out.tolist() == pytest.approx([0.5, 0.5, 0.2, 0.2])


def test_mix_limita_el_pico():
    a = np.full(3, 0.9, dtype=np.float32)
    out = w.mix_blocks([a, a], 3)
    assert out.max() <= 1.0


def test_mix_sin_bloques_es_silencio():
    assert not w.mix_blocks([], 5).any()


def test_dispositivo_en_silencio_no_tapa_al_activo():
    activo = np.full(4800, 0.4, dtype=np.float32)
    out = w.mix_blocks([np.zeros(0, dtype=np.float32), activo], 4800)
    assert out.max() == pytest.approx(0.4)


def test_resampler_mono_misma_frecuencia_no_cambia_nada():
    r = w.Resampler(48000, 1)
    x = np.linspace(-1, 1, 480, dtype=np.float32)
    assert np.array_equal(r.process(x), x)


def test_resampler_estereo_pasa_a_mono_promediando():
    r = w.Resampler(48000, 2)
    x = np.array([1.0, 0.0, 1.0, 0.0], dtype=np.float32)
    assert r.process(x).tolist() == [0.5, 0.5]


def test_resampler_44100_a_48000_encadenando_bloques_conserva_la_duracion():
    r = w.Resampler(44100, 1)
    total = 0
    for _ in range(10):
        total += len(r.process(np.zeros(4410, dtype=np.float32)))
    assert abs(total - 48000) <= 2


def test_buffer_descarta_lo_mas_antiguo_al_pasar_el_tope():
    buf = w.DeviceBuffer('x', 48000, 1)
    cap = int(w.MAX_BUFFER_S * w.MIX_SR)
    buf.push(np.zeros(cap, dtype=np.float32).tobytes())
    buf.push(np.ones(1000, dtype=np.float32).tobytes())
    out = buf.pop(cap)
    assert len(out) == cap
    assert out[-1] == 1.0
