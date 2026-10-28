"""El reintento del loopback es de la grabacion que lo lanzo, no de la que haya.

77140da hizo que el loopback se reintentara cada 10 s mientras se grabe, y
9199241 que start() ya no espere a que se guarde la reunion anterior. Juntos,
un reintento de la reunion A dormido 10 s se despertaba con la B ya grabando
(se cuelga y se vuelve a entrar, una reconexion) y le abria un segundo worker
encima de sus ficheros. Y la cabecera del worker puede tardar hasta 15 s: en
ese rato tambien puede cambiar la grabacion.

Sin dispositivos: los intentos y el worker son dobles.
"""
import struct
import threading

import pytest

import audio_recorder as ar
from audio_recorder import AudioRecorder

pytestmark = pytest.mark.unit


@pytest.fixture
def grabador(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, '_LOOPBACK_RETRY_WAIT', 0.01)
    monkeypatch.setattr(ar, '_LOOPBACK_LATE_WAIT', 0.01)
    rec = AudioRecorder()
    rec._tmp_loop = tmp_path / 'a.loop.part'
    rec._tmp_mic = tmp_path / 'a.mic.part'
    rec._recording = True
    rec._writer_abort = threading.Event()
    return rec


def test_un_reintento_de_la_reunion_anterior_no_toca_la_siguiente(grabador, monkeypatch):
    """EL CASO: el primer intento falla y, mientras se espera para reintentar,
    la reunion A termina y empieza la B. A no vuelve a intentar nada."""
    intentos = []

    def falla_y_cambia(still=None):
        intentos.append(still())
        grabador._writer_abort = threading.Event()     # empezo la reunion B
        return False

    monkeypatch.setattr(grabador, '_start_wasapi_loopback', falla_y_cambia)
    monkeypatch.setattr(grabador, '_start_stereo_mix_loopback', lambda still=None: False)

    grabador._start_loopback_async()

    assert intentos == [True], "A siguio reintentando sobre la grabacion de B"


def test_mientras_sea_su_grabacion_sigue_reintentando(grabador, monkeypatch):
    intentos = []

    def falla(still=None):
        intentos.append(1)
        if len(intentos) == 5:
            grabador._recording = False                  # se colgo
        return False

    monkeypatch.setattr(grabador, '_start_wasapi_loopback', falla)
    monkeypatch.setattr(grabador, '_start_stereo_mix_loopback', lambda still=None: False)

    grabador._start_loopback_async()

    assert len(intentos) == 5


class _Worker:
    """Worker de loopback que ya mando su cabecera."""

    def __init__(self):
        self.terminado = False
        self.stdout = self

    def read(self, n):
        return struct.pack('<II', 48000, 2)[:n]

    def poll(self):
        return None

    def terminate(self):
        self.terminado = True

    def kill(self):
        self.terminado = True

    def communicate(self, timeout=None):
        return b'', b''


def test_una_cabecera_que_llega_tarde_no_se_engancha_a_otra_grabacion(grabador, monkeypatch):
    """La cabecera llego cuando la grabacion que lo pidio ya no era la de ahora:
    el worker se cierra y no se abre ningun writer sobre los ficheros de nadie."""
    worker = _Worker()
    monkeypatch.setattr(ar.subprocess, 'Popen', lambda *a, **k: worker)
    abiertos = []
    monkeypatch.setattr(grabador, '_open_loop_writer', lambda: abiertos.append(1))

    assert grabador._start_wasapi_loopback(still=lambda: False) is False

    assert worker.terminado and abiertos == []
    assert grabador._loopback_proc is None


def test_con_su_grabacion_vigente_la_cabecera_engancha_el_worker(grabador, monkeypatch):
    worker = _Worker()
    monkeypatch.setattr(ar.subprocess, 'Popen', lambda *a, **k: worker)
    abiertos = []
    monkeypatch.setattr(grabador, '_open_loop_writer', lambda: abiertos.append(1))
    monkeypatch.setattr(grabador, '_wasapi_reader', lambda *a: None)

    assert grabador._start_wasapi_loopback(still=lambda: True) is True

    assert abiertos == [1] and grabador._loopback_proc is worker and not worker.terminado
