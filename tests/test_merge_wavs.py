"""Fusion manual de varias grabaciones en un solo WAV.

Se usa a mano cuando una reunion quedo partida en trozos que la fusion
automatica no junto (por ejemplo, tres reconexiones seguidas). Es la
herramienta de rescate, asi que su fallo llega en el peor momento: cuando ya
algo ha ido mal.

Lo que importa cubrir: que respeta el orden, que normaliza el sample rate al
que espera Whisper, y que no recorta el audio por saturacion.
"""
import numpy as np
import pytest
import soundfile as sf

from _merge_wavs import merge_wavs

pytestmark = pytest.mark.unit

SR = 16000          # el que espera el pipeline


@pytest.fixture
def tono(tr_dirs):
    """Fabrica un WAV con una amplitud constante, facil de identificar luego."""
    def make(nombre, seconds=1.0, samplerate=SR, amplitud=0.3, canales=1):
        p = tr_dirs / nombre
        n = int(seconds * samplerate)
        data = np.full(n, amplitud, dtype=np.float32)
        if canales == 2:
            data = np.column_stack([data, data])
        sf.write(str(p), data, samplerate, subtype='PCM_16')
        return p
    return make


def test_fusionar_dos_ficheros_suma_sus_duraciones(tono, tr_dirs):
    a = tono('a.wav', seconds=1.0)
    b = tono('b.wav', seconds=2.0)
    salida = tr_dirs / 'fusionado.wav'

    merge_wavs([a, b], salida)

    info = sf.info(str(salida))
    assert abs(info.duration - 3.0) < 0.05


def test_el_orden_de_los_ficheros_se_respeta(tono, tr_dirs):
    """Si se invirtiera, la reunion quedaria contada del final al principio."""
    a = tono('a.wav', seconds=0.5, amplitud=0.2)
    b = tono('b.wav', seconds=0.5, amplitud=0.8)
    salida = tr_dirs / 'fusionado.wav'

    merge_wavs([a, b], salida)

    data, _ = sf.read(str(salida), dtype='float32')
    primera_mitad = np.abs(data[:len(data) // 2]).mean()
    segunda_mitad = np.abs(data[len(data) // 2:]).mean()
    assert primera_mitad < segunda_mitad


def test_la_salida_esta_al_sample_rate_del_pipeline(tono, tr_dirs):
    """Whisper recibe 16 kHz: si la fusion dejara otro, la transcripcion sale
    mal o directamente falla."""
    a = tono('a.wav', seconds=0.5, samplerate=48000)
    salida = tr_dirs / 'fusionado.wav'

    merge_wavs([a], salida)

    assert sf.info(str(salida)).samplerate == SR


def test_un_fichero_a_otro_sample_rate_se_remuestrea_sin_perder_duracion(tono,
                                                                         tr_dirs):
    a = tono('a.wav', seconds=2.0, samplerate=44100)
    salida = tr_dirs / 'fusionado.wav'

    merge_wavs([a], salida)

    assert abs(sf.info(str(salida)).duration - 2.0) < 0.05


def test_se_pueden_fusionar_ficheros_de_sample_rates_distintos(tono, tr_dirs):
    """Caso real: una parte grabada por WASAPI a 48 kHz y otra por el micro a
    16 kHz."""
    a = tono('a.wav', seconds=1.0, samplerate=48000)
    b = tono('b.wav', seconds=1.0, samplerate=SR)
    salida = tr_dirs / 'fusionado.wav'

    merge_wavs([a, b], salida)

    info = sf.info(str(salida))
    assert info.samplerate == SR
    assert abs(info.duration - 2.0) < 0.05


def test_un_fichero_estereo_se_pasa_a_mono(tono, tr_dirs):
    a = tono('a.wav', seconds=1.0, canales=2)
    salida = tr_dirs / 'fusionado.wav'

    merge_wavs([a], salida)

    assert sf.info(str(salida)).channels == 1


def test_el_audio_se_normaliza_para_no_saturar(tono, tr_dirs):
    """Concatenar dos grabaciones con niveles muy distintos puede pasarse de
    fondo de escala: el pico se lleva a 0,9 para dejar margen."""
    a = tono('a.wav', seconds=0.5, amplitud=0.95)
    b = tono('b.wav', seconds=0.5, amplitud=0.1)
    salida = tr_dirs / 'fusionado.wav'

    merge_wavs([a, b], salida)

    data, _ = sf.read(str(salida), dtype='float32')
    pico = float(np.abs(data).max())
    assert 0.85 < pico < 0.95, f"pico {pico:.3f}: deberia normalizarse a 0,9"


def test_el_silencio_absoluto_no_provoca_division_por_cero(tono, tr_dirs):
    a = tono('a.wav', seconds=0.5, amplitud=0.0)
    salida = tr_dirs / 'fusionado.wav'

    merge_wavs([a], salida)          # no debe lanzar

    data, _ = sf.read(str(salida), dtype='float32')
    assert float(np.abs(data).max()) == 0.0


def test_fusionar_devuelve_la_ruta_de_salida(tono, tr_dirs):
    a = tono('a.wav', seconds=0.2)
    salida = tr_dirs / 'fusionado.wav'
    assert merge_wavs([a], salida) == salida


def test_la_salida_es_pcm16(tono, tr_dirs):
    """Es lo que espera el resto del pipeline; un float WAV pesaria el doble."""
    a = tono('a.wav', seconds=0.2)
    salida = tr_dirs / 'fusionado.wav'

    merge_wavs([a], salida)

    assert 'PCM_16' in sf.info(str(salida)).subtype


def test_fusionar_un_solo_fichero_lo_copia_normalizado(tono, tr_dirs):
    a = tono('a.wav', seconds=1.0, amplitud=0.3)
    salida = tr_dirs / 'fusionado.wav'

    merge_wavs([a], salida)

    assert abs(sf.info(str(salida)).duration - 1.0) < 0.05
