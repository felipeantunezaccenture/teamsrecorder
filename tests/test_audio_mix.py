"""Audio sin dispositivo: filtro DC, alineacion de la mezcla y temporales.

INCIDENTES QUE ORIGINARON ESTOS TESTS
- 10/09/2026: la mezcla se cortaba cuando el loopback se quedaba corto (WASAPI
  no entrega nada en silencio). Con un `zip` la grabacion terminaba en el
  primer stream que se agotaba: se perdieron 9,6 minutos de una reunion real.
- 16/09/2026: un reintento del loopback dejaba dos writers sobre el mismo
  fichero temporal. El primero se quedaba bloqueado para siempre en q.get()
  con el fichero abierto y en Windows eso impide borrarlo: 99 MB huerfanos.
"""
import queue
import threading

import numpy as np
import pytest
import soundfile as sf

import audio_recorder as ar
from audio_recorder import AudioRecorder
from config import SAMPLE_RATE

pytestmark = pytest.mark.unit


def rms(x):
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


# ── Filtro paso-alto (DC blocker) del microfono ────────────────────────────

@pytest.fixture
def audio_sucio():
    """Voz de 300 Hz contaminada con offset DC y rumble de 12 Hz."""
    t = np.arange(SAMPLE_RATE * 4) / SAMPLE_RATE
    voice = (0.3 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    rumble = (0.4 * np.sin(2 * np.pi * 12 * t)).astype(np.float32)
    return voice, (voice + 0.25 + rumble).astype(np.float32)


@pytest.fixture
def filtrado_por_bloques(audio_sucio):
    """Filtrado en bloques de 100 ms, igual que en produccion."""
    _, dirty = audio_sucio
    rec = AudioRecorder()
    rec._reset_dc_filter()
    block = int(SAMPLE_RATE * 0.1)
    out = np.concatenate([rec._dc_block(dirty[i:i + block])
                          for i in range(0, len(dirty), block)])
    return out


def test_elimina_el_offset_dc(filtrado_por_bloques):
    settled = filtrado_por_bloques[SAMPLE_RATE:]        # sin el transitorio inicial
    assert abs(float(settled.mean())) < 0.005


def test_reduce_el_pico_y_devuelve_margen_dinamico(audio_sucio, filtrado_por_bloques):
    _, dirty = audio_sucio
    settled = filtrado_por_bloques[SAMPLE_RATE:]
    assert float(np.abs(settled).max()) < float(np.abs(dirty).max()) * 0.75


def test_la_voz_sigue_siendo_la_frecuencia_dominante(filtrado_por_bloques):
    settled = filtrado_por_bloques[SAMPLE_RATE:]
    spec = np.abs(np.fft.rfft(settled * np.hanning(len(settled))))
    freqs = np.fft.rfftfreq(len(settled), 1 / SAMPLE_RATE)
    assert abs(float(freqs[spec.argmax()]) - 300) < 10


def test_conserva_mas_del_95_por_ciento_de_la_energia_de_la_voz(audio_sucio,
                                                                filtrado_por_bloques):
    voice, _ = audio_sucio
    settled = filtrado_por_bloques[SAMPLE_RATE:]
    win = np.hanning(len(settled))
    freqs = np.fft.rfftfreq(len(settled), 1 / SAMPLE_RATE)
    banda = (freqs > 290) & (freqs < 310)
    spec = np.abs(np.fft.rfft(settled * win))
    ref = np.abs(np.fft.rfft(voice[SAMPLE_RATE:] * win))
    assert float(spec[banda].sum() / ref[banda].sum()) > 0.95


def test_atenua_el_rumble_por_debajo_de_40_hz(audio_sucio, filtrado_por_bloques):
    _, dirty = audio_sucio
    settled = filtrado_por_bloques[SAMPLE_RATE:]
    win = np.hanning(len(settled))
    freqs = np.fft.rfftfreq(len(settled), 1 / SAMPLE_RATE)
    bajas = freqs < 40
    spec = np.abs(np.fft.rfft(settled * win))
    ref = np.abs(np.fft.rfft(dirty[SAMPLE_RATE:] * win))
    assert float(spec[bajas].sum() / ref[bajas].sum()) < 0.10


def test_filtrar_por_bloques_da_lo_mismo_que_de_una_vez(audio_sucio, filtrado_por_bloques):
    """El filtro arrastra estado entre bloques; si se reiniciara en cada uno
    habria un chasquido cada 100 ms."""
    _, dirty = audio_sucio
    rec = AudioRecorder()
    rec._reset_dc_filter()
    de_una_vez = rec._dc_block(dirty)
    assert float(np.abs(de_una_vez - filtrado_por_bloques).max()) < 1e-6


def test_el_corte_del_filtro_esta_por_debajo_de_la_voz():
    """Un corte demasiado alto se comeria las voces graves."""
    assert 0 < ar._HIGHPASS_HZ < 120


# ── Alineacion de la mezcla ────────────────────────────────────────────────

@pytest.fixture
def escribir_wav(tr_dirs):
    """Escribe un temporal .part como lo hace produccion.

    El formato va EXPLICITO: soundfile no puede inferirlo de la extension
    .part, que es la que usa el pipeline a proposito para que el buscador de
    *.wav no recoja un temporal a medio escribir.
    """
    def make(name, data, samplerate=SAMPLE_RATE, subtype='FLOAT'):
        p = tr_dirs / name
        sf.write(str(p), np.asarray(data, dtype=np.float32), samplerate,
                 format='WAV', subtype=subtype)
        return p
    return make


def test_la_mezcla_dura_lo_que_el_stream_mas_largo(escribir_wav):
    """El loopback se queda corto siempre que nadie habla: WASAPI no entrega
    nada en silencio. Con un zip la grabacion se cortaba ahi."""
    mic = escribir_wav('mic.part', np.full(1000, 0.1))
    loop = escribir_wav('loop.part', np.full(300, 0.2))

    total = 1000
    bloques = list(AudioRecorder._aligned_blocks(mic, loop, total))
    assert sum(len(m) for m, _ in bloques) == total
    assert sum(len(lp) for _, lp in bloques) == total


def test_el_stream_corto_se_rellena_con_silencio(escribir_wav):
    mic = escribir_wav('mic.part', np.full(1000, 0.1))
    loop = escribir_wav('loop.part', np.full(300, 0.2))

    loop_all = np.concatenate([lp for _, lp in
                               AudioRecorder._aligned_blocks(mic, loop, 1000)])
    assert np.allclose(loop_all[:300], 0.2)
    assert np.allclose(loop_all[300:], 0.0), "relleno con basura en vez de silencio"


def test_sin_loopback_la_mezcla_es_solo_microfono(escribir_wav):
    """Caso real: WASAPI falla y la grabacion sale solo con tu voz. Debe
    guardarse igual, no perderse."""
    mic = escribir_wav('mic.part', np.full(500, 0.1))
    bloques = list(AudioRecorder._aligned_blocks(mic, None, 500))
    assert sum(len(m) for m, _ in bloques) == 500
    assert all(np.allclose(lp, 0.0) for _, lp in bloques)


def test_write_mix_produce_un_wav_de_la_duracion_pedida(escribir_wav, tr_dirs):
    mic = escribir_wav('mic.part', np.full(2000, 0.1))
    loop = escribir_wav('loop.part', np.full(800, 0.1))
    out = tr_dirs / 'final.wav'

    AudioRecorder._write_mix(out, mic, loop, 2000, mic_gain=1.0, gain=1.0)

    info = sf.info(str(out))
    assert info.frames == 2000
    assert info.samplerate == SAMPLE_RATE
    assert info.channels == 1


def test_write_mix_suma_los_dos_streams(escribir_wav, tr_dirs):
    mic = escribir_wav('mic.part', np.full(1000, 0.10))
    loop = escribir_wav('loop.part', np.full(1000, 0.20))
    out = tr_dirs / 'final.wav'

    AudioRecorder._write_mix(out, mic, loop, 1000, mic_gain=1.0, gain=1.0)

    data, _ = sf.read(str(out), dtype='float32')
    assert abs(rms(data) - 0.30) < 0.01, "no sumo los dos streams"


def test_blocks_respeta_el_limite(escribir_wav):
    p = escribir_wav('x.part', np.arange(5000, dtype=np.float32) / 5000)
    leido = sum(len(b) for b in AudioRecorder._blocks(p, limit=1234))
    assert leido == 1234


def test_blocks_sin_limite_lee_el_fichero_entero(escribir_wav):
    p = escribir_wav('x.part', np.zeros(3333, dtype=np.float32))
    assert sum(len(b) for b in AudioRecorder._blocks(p)) == 3333


# ── El writer del loopback no se queda colgado al reintentar ───────────────

def test_abrir_el_writer_dos_veces_deja_solo_uno_vivo(tr_dirs):
    """El 16/09 Stereo Mix abrio el fichero y fallo despues; el reintento de
    WASAPI abrio un segundo writer sobre el mismo .part. El primero quedo
    esperando en q.get() con el fichero abierto y el temporal, 99 MB, no se
    pudo borrar."""
    rec = AudioRecorder()
    rec._tmp_loop = tr_dirs / 'x.loop.part'
    rec._mic_t0 = None
    rec._loop_sr, rec._loop_ch = SAMPLE_RATE, 1

    rec._open_loop_writer()
    primero = rec._loop_writer
    rec._open_loop_writer()
    segundo = rec._loop_writer

    assert primero is not segundo
    primero.join(timeout=5)
    assert not primero.is_alive(), "el primer writer se quedo colgado"

    vivos = [t for t in threading.enumerate() if t.name == 'LoopWriter' and t.is_alive()]
    assert len(vivos) == 1, f"{len(vivos)} writers vivos"

    rec._loop_q.put(None)
    segundo.join(timeout=5)


def test_tras_cerrar_el_writer_el_temporal_se_puede_borrar(tr_dirs):
    """Es el sintoma que veia el usuario: un .part de 99 MB imposible de
    borrar mientras el proceso siguiera vivo."""
    rec = AudioRecorder()
    rec._tmp_loop = tr_dirs / 'y.loop.part'
    rec._tmp_mic = None
    rec._mic_t0 = None
    rec._loop_sr, rec._loop_ch = SAMPLE_RATE, 1

    rec._open_loop_writer()
    rec._open_loop_writer()
    rec._loop_q.put(None)
    rec._loop_writer.join(timeout=5)

    rec._discard_temp()
    assert not rec._tmp_loop.exists()


def test_cerrar_un_writer_inexistente_no_lanza(tr_dirs):
    rec = AudioRecorder()
    rec._loop_q = None
    rec._loop_writer = None
    rec._close_stale_loop_writer()          # no debe lanzar


def test_el_writer_padea_silencio_por_el_retraso_del_loopback(tr_dirs):
    """El loopback arranca despues del microfono (hasta 13 s el 17/09). Sin
    compensar, las voces de los demas quedarian adelantadas."""
    import time

    rec = AudioRecorder()
    rec._tmp_loop = tr_dirs / 'z.loop.part'
    rec._loop_sr, rec._loop_ch = SAMPLE_RATE, 1
    rec._mic_t0 = time.monotonic() - 2.0        # el micro arranco 2 s antes

    rec._open_loop_writer()
    rec._loop_q.put(None)
    rec._loop_writer.join(timeout=10)

    frames = sf.info(str(rec._tmp_loop)).frames
    assert frames >= SAMPLE_RATE * 1.5, f"solo padeo {frames / SAMPLE_RATE:.2f} s"


def test_el_writer_escribe_lo_que_se_le_encola(tr_dirs):
    rec = AudioRecorder()
    path = tr_dirs / 'w.part'
    q = queue.Queue()
    th = threading.Thread(target=rec._writer_loop,
                          args=(q, path, 1, SAMPLE_RATE, 'test'), daemon=True)
    th.start()
    q.put(np.full(800, 0.5, dtype=np.float32))
    q.put(None)
    th.join(timeout=10)

    data, _ = sf.read(str(path), dtype='float32')
    assert len(data) == 800
    assert abs(rms(data) - 0.5) < 0.01
