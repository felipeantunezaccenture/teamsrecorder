"""Grabacion con el MICROFONO REAL de la maquina.

Marcados `integration`: abren un InputStream de verdad, asi que no corren en
CI. Se ejecutan en local antes de tocar nada del audio:

    pytest -m integration

INCIDENTES QUE ORIGINARON ESTOS TESTS
- 07/09/2026: dos `start()` a la vez arrancaron dos grabaciones sobre el mismo
  fichero. El log mostraba dos "Recording started" separados por 13 s.
- 10/09/2026: el loopback fallo y la reunion se grabo solo con el microfono,
  SIN ningun aviso. Con auriculares eso significa perder todas las demas
  voces, y nadie se entero hasta escuchar el audio.
"""
import threading
import time

import pytest

import audio_recorder as ar
from audio_recorder import AudioRecorder

pytestmark = pytest.mark.integration


@pytest.fixture
def sin_loopback():
    """Recorder cuyo loopback siempre falla, para probar el aviso y el
    fallback a solo microfono sin depender del hardware de audio."""
    class SinLoopback(AudioRecorder):
        def __init__(self):
            super().__init__()
            self.wasapi_calls = 0
            self.stereo_calls = 0

        def _start_wasapi_loopback(self):
            self.wasapi_calls += 1
            self._loopback_error = 'simulado: no hay dispositivo loopback'
            return False

        def _start_stereo_mix_loopback(self):
            self.stereo_calls += 1
            return False

    return SinLoopback


# ── Condicion de carrera en start() ───────────────────────────────────────

def test_dos_start_simultaneos_arrancan_una_sola_grabacion(tr_dirs):
    """El bug del 07/09. _begin se hace lento a proposito para reproducir la
    ventana que antes dejaba entrar a los dos hilos."""
    class ArranqueLento(AudioRecorder):
        def __init__(self):
            super().__init__()
            self.begun = []

        def _begin(self, output_path):
            self.begun.append(output_path)
            time.sleep(0.6)

    rec = ArranqueLento()
    hilos = [threading.Thread(target=rec.start, args=(tr_dirs / f'race_{i}.wav',))
             for i in range(2)]
    for t in hilos:
        t.start()
    for t in hilos:
        t.join()

    assert len(rec.begun) == 1, f"arrancaron {len(rec.begun)}: {[p.name for p in rec.begun]}"
    assert rec.is_recording
    rec._recording = False


def test_start_reserva_el_flag_bajo_lock(tr_dirs):
    rec = AudioRecorder()
    assert isinstance(rec._start_lock, type(threading.Lock()))


# ── Sin loopback: reintentos y aviso al usuario ───────────────────────────

@pytest.fixture
def grabacion_sin_loopback(sin_loopback, tr_dirs):
    avisos = []
    rec = sin_loopback()
    rec.on_loopback_unavailable = avisos.append
    out = tr_dirs / 'no_loopback.wav'

    t0 = time.time()
    rec.start(out)
    elapsed = time.time() - t0

    # Esperar a que el hilo del loopback agote los reintentos
    time.sleep(ar._LOOPBACK_ATTEMPTS * ar._LOOPBACK_RETRY_WAIT + 1.5)
    rec.stop()
    guardado = rec.wait_for_save(timeout=60)
    return {'rec': rec, 'out': out, 'avisos': avisos, 'elapsed': elapsed,
            'guardado': guardado}


def test_start_no_bloquea_esperando_el_loopback(grabacion_sin_loopback):
    """El loopback se abre en segundo plano: start() debe volver de inmediato
    aunque queden 3 reintentos de 1,5 s por delante. El umbral es holgado
    porque abrir el InputStream del microfono real ronda el segundo."""
    espera_reintentos = ar._LOOPBACK_ATTEMPTS * ar._LOOPBACK_RETRY_WAIT
    assert grabacion_sin_loopback['elapsed'] < espera_reintentos - 1.0


def test_reintenta_el_loopback_el_numero_de_veces_configurado(grabacion_sin_loopback):
    rec = grabacion_sin_loopback['rec']
    assert rec.wasapi_calls == ar._LOOPBACK_ATTEMPTS
    assert rec.stereo_calls == ar._LOOPBACK_ATTEMPTS, "no probo el fallback Stereo Mix"


def test_avisa_al_usuario_de_que_solo_hay_microfono(grabacion_sin_loopback):
    """Sin este aviso, el 10/09 se grabo una reunion entera sin las voces de
    los demas y nadie se entero hasta escucharla."""
    avisos = grabacion_sin_loopback['avisos']
    assert len(avisos) == 1
    assert 'simulado' in avisos[0], "el aviso no dice el motivo"


def test_aun_sin_loopback_guarda_la_grabacion(grabacion_sin_loopback):
    """Solo microfono es peor que nada, pero mucho mejor que perder la
    reunion."""
    assert grabacion_sin_loopback['guardado']
    assert grabacion_sin_loopback['out'].exists()
    assert grabacion_sin_loopback['rec'].loopback_active is False


def test_no_deja_temporales_part_huerfanos(grabacion_sin_loopback, tr_dirs):
    time.sleep(0.5)
    huerfanos = list(tr_dirs.glob('no_loopback.*.part'))
    assert not huerfanos, [p.name for p in huerfanos]


# ── cancel() descarta sin guardar ─────────────────────────────────────────

@pytest.fixture
def grabacion_cancelada(sin_loopback, tr_dirs):
    rec = sin_loopback()
    rec.on_loopback_unavailable = lambda _r: None
    out = tr_dirs / 'cancelada.wav'
    rec.start(out)
    time.sleep(1.0)
    rec.cancel()
    time.sleep(0.5)
    return {'rec': rec, 'out': out}


def test_cancelar_deja_de_grabar(grabacion_cancelada):
    assert grabacion_cancelada['rec'].is_recording is False


def test_cancelar_no_genera_el_wav_final(grabacion_cancelada):
    assert not grabacion_cancelada['out'].exists()


def test_cancelar_borra_los_temporales(grabacion_cancelada, tr_dirs):
    assert not list(tr_dirs.glob('cancelada.*.part'))


def test_cancelar_libera_el_evento_de_guardado(grabacion_cancelada):
    """Si no se liberara, un start() posterior se quedaria esperando."""
    assert grabacion_cancelada['rec'].wait_for_save(timeout=1)


# ── Llamadas fuera de orden ───────────────────────────────────────────────

def test_stop_sin_grabar_devuelve_none(sin_loopback):
    assert sin_loopback().stop() is None


def test_cancel_sin_grabar_no_lanza(sin_loopback):
    assert sin_loopback().cancel() is None


def test_el_segundo_stop_no_reprocesa(sin_loopback, tr_dirs):
    rec = sin_loopback()
    rec.on_loopback_unavailable = lambda _r: None
    out = tr_dirs / 'doble_stop.wav'
    rec.start(out)
    time.sleep(1.0)

    primero = rec.stop()
    segundo = rec.stop()
    rec.wait_for_save(timeout=60)

    assert primero == out
    assert segundo is None


# ── El reintento del loopback no deja temporal huerfano ───────────────────

def test_el_reintento_del_loopback_no_deja_part_huerfano(tr_dirs):
    """Reproduce la secuencia real del 16/09 con la reunion de las 16:40:
    Stereo Mix abre el fichero del loopback y falla despues, y el reintento de
    WASAPI abre un segundo writer sobre el mismo .part."""
    class ReintentoLoopback(AudioRecorder):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def _start_wasapi_loopback(self):
            self.attempts += 1
            if self.attempts == 1:
                self._loopback_error = 'simulado: worker muerto'
                return False
            self._loop_sr, self._loop_ch = ar.SAMPLE_RATE, 1
            self._open_loop_writer()
            return True

        def _start_stereo_mix_loopback(self):
            # Abre el fichero ANTES de saber si el InputStream funciona.
            self._loop_sr, self._loop_ch = ar.SAMPLE_RATE, 1
            self._open_loop_writer()
            self._loopback_error = 'simulado: Invalid device'
            return False

    rec = ReintentoLoopback()
    rec.on_loopback_unavailable = lambda _r: None
    out = tr_dirs / 'retry_loop.wav'
    rec.start(out)
    time.sleep(ar._LOOPBACK_RETRY_WAIT + 2.5)

    assert rec.loopback_active is True
    vivos = [t for t in threading.enumerate() if t.name == 'LoopWriter' and t.is_alive()]
    assert len(vivos) == 1, f"{len(vivos)} writers de loopback vivos"

    rec.stop()
    assert rec.wait_for_save(timeout=60)
    time.sleep(0.5)

    assert out.exists()
    huerfanos = list(tr_dirs.glob('retry_loop.*.part'))
    assert not huerfanos, [p.name for p in huerfanos]
    assert not [t for t in threading.enumerate()
                if t.name == 'LoopWriter' and t.is_alive()]
