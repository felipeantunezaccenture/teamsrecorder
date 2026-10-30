"""El audio de los demas ya no depende de que UN worker de loopback arranque rapido.

INCIDENTE (29/10/2026, 16:02)
Cuatro workers seguidos tardaron mas de 15 s en dar la cabecera (arrancar Python
con la CPU saturada: `python -c pass` tarda 1,5-2 s aqui, y mas con Teams
entrando en la llamada). A cada uno se le mataba justo antes de terminar y se
empezaba otro de cero: 94 s sin las voces de los demas, y el aviso "solo grabara
tu parte". El quinto, que arranco en 4 s, funciono.

Ahora los workers lentos NO se matan: se lanza otro al lado cada pocos segundos y
gana el primero que mande la cabecera. Ademas hay un worker "armado" con Python
ya cargado, y si el worker muere con la reunion en marcha se relanza.

Sin dispositivos ni procesos: los workers son dobles.
"""
import struct
import threading
import time

import numpy as np
import pytest

import audio_recorder as ar
from audio_recorder import AudioRecorder

pytestmark = pytest.mark.unit

HEADER = struct.pack('<II', 48000, 1)


class Worker:
    """Worker doble: manda su cabecera tras `delay` s (o nunca, o falla)."""

    def __init__(self, delay=0.0, never=False, error=None, audio=b''):
        self.delay, self.never, self.error = delay, never, error
        self.audio = audio
        self.terminado = False
        self.stdin = _Sink()
        self.stdout = self
        self._muerto = threading.Event()
        self._t0 = time.monotonic()
        self._pos = 0

    def read(self, n):
        if self.never:
            self._muerto.wait()
            return b''
        if self.error:
            return b''
        if n == 8:
            resta = self.delay - (time.monotonic() - self._t0)
            if resta > 0 and self._muerto.wait(resta):
                return b''
            return HEADER
        # Audio: lo que quede y luego EOF
        if self._pos < len(self.audio):
            out = self.audio[self._pos:self._pos + n]
            self._pos += n
            return out
        self._muerto.wait(0.05)
        return b''

    def poll(self):
        return 1 if (self.error or self._muerto.is_set()) else None

    def terminate(self):
        self.terminado = True
        self._muerto.set()

    kill = terminate

    def communicate(self, timeout=None):
        return b'', (self.error or '').encode()


class _Sink:
    def __init__(self):
        self.escrito = b''
        self.cerrado = False

    def write(self, b):
        self.escrito += b

    def flush(self):
        pass

    def close(self):
        self.cerrado = True


@pytest.fixture
def grabador(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, '_LOOPBACK_RETRY_WAIT', 0.01)
    monkeypatch.setattr(ar, '_LOOPBACK_LATE_WAIT', 0.01)
    monkeypatch.setattr(ar, '_LOOPBACK_STAGGER', 0.2)
    monkeypatch.setattr(ar, '_LOOPBACK_STANDBY_GRACE', 0.3)
    monkeypatch.setattr(ar, '_LOOPBACK_HEADER_TIMEOUT', 3.0)
    rec = AudioRecorder()
    rec._tmp_loop = tmp_path / 'a.loop.part'
    rec._tmp_mic = tmp_path / 'a.mic.part'
    rec._recording = True
    rec._mic_t0 = time.monotonic()
    rec._writer_abort = threading.Event()
    monkeypatch.setattr(rec, '_wasapi_reader', lambda *a: None)
    monkeypatch.setattr(rec, '_open_loop_writer', lambda: None)
    return rec


def lanzar(monkeypatch, rec, workers):
    """Los workers 'normales' que se van lanzando, en orden."""
    cola = list(workers)
    lanzados = []

    def popen(standby=False):
        w = cola.pop(0)
        lanzados.append(w)
        return w

    monkeypatch.setattr(rec, '_popen_loopback_worker', popen)
    return lanzados


def test_un_worker_lento_no_se_mata_para_empezar_otro_de_cero(grabador, monkeypatch):
    """EL CASO DEL 29/10: el worker tarda mas que el tiempo de apuesta pero
    termina. Antes se mataba y se empezaba otro; ahora se le espera."""
    lento = Worker(delay=0.7)
    extra = Worker(never=True)
    lanzar(monkeypatch, grabador, [lento, extra, Worker(never=True)])

    proc, header, motivo = grabador._race_loopback_workers(lambda: True)

    assert proc is lento and header == HEADER and motivo is None
    assert not lento.terminado, "se mato al worker que iba a ganar"


def test_si_el_primero_se_cuelga_gana_el_que_se_lanza_al_lado(grabador, monkeypatch):
    colgado = Worker(never=True)
    bueno = Worker(delay=0.0)
    lanzar(monkeypatch, grabador, [colgado, bueno])

    t = time.monotonic()
    proc, _h, _m = grabador._race_loopback_workers(lambda: True)

    assert proc is bueno
    assert time.monotonic() - t < 1.5, "tardo como si hubiera esperado el timeout entero"
    time.sleep(0.2)
    assert colgado.terminado, "el worker colgado quedo vivo (huerfano)"


def test_todos_fallan_rapido_devuelve_el_motivo_sin_esperar_el_timeout(grabador, monkeypatch):
    lanzar(monkeypatch, grabador, [Worker(error='ningun dispositivo loopback se pudo abrir')])

    t = time.monotonic()
    proc, header, motivo = grabador._race_loopback_workers(lambda: True)

    assert proc is None and header is None
    assert 'ningun dispositivo' in motivo
    assert time.monotonic() - t < 1.0


def test_ninguno_responde_acaba_en_el_timeout_y_los_cierra(grabador, monkeypatch):
    monkeypatch.setattr(ar, '_LOOPBACK_HEADER_TIMEOUT', 0.8)
    ws = [Worker(never=True) for _ in range(3)]
    lanzar(monkeypatch, grabador, ws)

    proc, _h, motivo = grabador._race_loopback_workers(lambda: True)

    assert proc is None and 'ningún worker' in motivo
    time.sleep(0.3)
    assert all(w.terminado for w in ws)


def test_si_la_grabacion_termina_durante_la_espera_se_cierra_todo(grabador, monkeypatch):
    ws = [Worker(never=True) for _ in range(3)]
    lanzar(monkeypatch, grabador, ws)
    estado = {'vigente': True}
    threading.Timer(0.3, lambda: estado.update(vigente=False)).start()

    t = time.monotonic()
    proc, _h, _m = grabador._race_loopback_workers(lambda: estado['vigente'])

    assert proc is None
    assert time.monotonic() - t < 1.5
    time.sleep(0.3)
    assert all(w.terminado for w in ws[:2])


def test_el_worker_armado_se_usa_primero_y_no_se_lanza_otro_si_responde(grabador, monkeypatch):
    armado = Worker(delay=0.0)
    grabador._standby = armado
    grabador._standby_enabled = False          # sin rearmado en este test
    lanzados = lanzar(monkeypatch, grabador, [Worker(never=True)])

    proc, _h, _m = grabador._race_loopback_workers(lambda: True)

    assert proc is armado
    assert armado.stdin.escrito == b'go\n' and armado.stdin.cerrado
    assert lanzados == [], "lanzo un worker normal pese a que el armado respondio"


def test_si_el_armado_no_responde_entra_un_worker_normal(grabador, monkeypatch):
    armado = Worker(never=True)
    normal = Worker(delay=0.0)
    grabador._standby = armado
    lanzar(monkeypatch, grabador, [normal])

    proc, _h, _m = grabador._race_loopback_workers(lambda: True)

    assert proc is normal


def test_un_armado_muerto_se_ignora(grabador, monkeypatch):
    armado = Worker(error='murio')
    grabador._standby = armado
    normal = Worker(delay=0.0)
    lanzar(monkeypatch, grabador, [normal])

    proc, _h, _m = grabador._race_loopback_workers(lambda: True)

    assert proc is normal


def test_arm_y_disarm_no_dejan_procesos(grabador, monkeypatch):
    creados = []

    def popen(standby=False):
        w = Worker(never=True)
        creados.append((w, standby))
        return w

    monkeypatch.setattr(grabador, '_popen_loopback_worker', popen)
    grabador.arm_loopback()
    grabador.arm_loopback()                    # idempotente: no duplica
    assert len(creados) == 1 and creados[0][1] is True

    grabador.disarm_loopback()
    time.sleep(0.2)
    assert creados[0][0].terminado


def test_despues_de_usar_el_armado_se_arma_otro(grabador, monkeypatch):
    monkeypatch.setattr(ar, '_LOOPBACK_REARM_DELAY', 0.05)
    creados = []
    monkeypatch.setattr(grabador, '_popen_loopback_worker',
                        lambda standby=False: creados.append(standby) or Worker(delay=0.0))
    grabador.arm_loopback()
    assert grabador._take_standby() is not None

    time.sleep(0.4)

    assert creados == [True, True]


# ── el worker muere en mitad de la reunion ─────────────────────────────────

def test_si_el_worker_muere_con_la_grabacion_en_marcha_se_relanza(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, '_LOOPBACK_RETRY_WAIT', 0.01)
    rec = AudioRecorder()
    rec._tmp_loop = tmp_path / 'a.loop.part'
    rec._tmp_mic = tmp_path / 'a.mic.part'
    rec._recording = True
    rec._mic_t0 = time.monotonic()
    abort = rec._writer_abort = threading.Event()
    relanzado = threading.Event()

    def mine():
        return rec._recording and rec._writer_abort is abort
    mine.abort = abort

    muerto = Worker(delay=0.0)
    muerto._muerto.set()                       # EOF inmediato
    rec._loopback_proc = muerto
    monkeypatch.setattr(rec, '_start_loopback_async',
                        lambda ab, resume=False: relanzado.set() if resume else None)

    rec._wasapi_reader(muerto, 48000, 1, mine)

    assert relanzado.wait(1.0), "el worker murio y nadie lo relanzo"


def test_el_worker_que_termina_porque_se_para_la_grabacion_no_se_relanza(tmp_path, monkeypatch):
    rec = AudioRecorder()
    rec._recording = False                     # stop() ya bajo el flag
    abort = rec._writer_abort = threading.Event()

    def mine():
        return rec._recording and rec._writer_abort is abort
    mine.abort = abort
    llamadas = []
    monkeypatch.setattr(rec, '_start_loopback_async', lambda *a, **k: llamadas.append(1))
    w = Worker(delay=0.0)
    w._muerto.set()
    rec._loopback_proc = w

    rec._wasapi_reader(w, 48000, 1, mine)
    time.sleep(0.2)

    assert llamadas == []


def test_al_relanzar_el_hueco_se_rellena_con_silencio_en_el_mismo_fichero(grabador):
    import queue
    grabador._loop_q = queue.Queue()
    grabador._loop_t_last = time.monotonic() - 3.0     # 3 s sin audio

    grabador._resume_loop_writer()

    bloque = grabador._loop_q.get_nowait()
    assert not bloque.any()
    assert 2.5 * ar.SAMPLE_RATE < len(bloque) < 3.6 * ar.SAMPLE_RATE


def test_reanudar_no_abre_un_fichero_nuevo(grabador, monkeypatch):
    """Abrir otro writer machacaria lo grabado antes de la caida."""
    import queue
    abiertos = []
    monkeypatch.setattr(grabador, '_open_loop_writer', lambda: abiertos.append(1))
    grabador._loop_q = queue.Queue()
    grabador._loop_t_last = time.monotonic()
    lanzar(monkeypatch, grabador, [Worker(delay=0.0)])

    assert grabador._start_wasapi_loopback(lambda: True, resume=True) is True
    assert abiertos == [], "reanudar abrio un writer nuevo"
    grabador._loop_q = None


def test_un_primer_arranque_si_abre_el_writer(grabador, monkeypatch):
    abiertos = []
    monkeypatch.setattr(grabador, '_open_loop_writer', lambda: abiertos.append(1))
    lanzar(monkeypatch, grabador, [Worker(delay=0.0)])

    assert grabador._start_wasapi_loopback(lambda: True) is True
    assert abiertos == [1]


def test_el_audio_del_worker_llega_a_la_cola_de_verdad(tmp_path, monkeypatch):
    """Control del camino bueno: sin esto, 'se relanza' seria trivial."""
    import queue
    rec = AudioRecorder()
    rec._recording = True
    rec._loop_q = queue.Queue()
    audio = (np.full(4800, 0.3, dtype=np.float32)).tobytes()
    w = Worker(delay=0.0, audio=audio)
    rec._loopback_proc = w

    t = threading.Thread(target=rec._wasapi_reader, args=(w, 48000, 1, None), daemon=True)
    t.start()
    time.sleep(0.3)
    rec._recording = False
    w.terminate()
    t.join(2)

    bloques = []
    while not rec._loop_q.empty():
        bloques.append(rec._loop_q.get_nowait())
    assert sum(len(b) for b in bloques) > 0
    assert max(float(np.max(b)) for b in bloques if len(b)) == pytest.approx(0.3, abs=0.01)


# ── el aviso no espera a agotar los intentos ───────────────────────────────

def test_el_aviso_de_sin_audio_sale_pronto_aunque_el_worker_siga_intentando(grabador, monkeypatch):
    """29/10: el aviso salio a los 54 s y el audio volvio a los 94 s. Con
    workers lentos, agotar los intentos puede tardar minutos."""
    monkeypatch.setattr(ar, '_LOOPBACK_WARN_AFTER', 0.2)
    avisos = []
    grabador.on_loopback_unavailable = avisos.append
    liberar = threading.Event()

    def lento(still=None, resume=False):
        liberar.wait(3)
        return False

    monkeypatch.setattr(grabador, '_start_wasapi_loopback', lento)
    monkeypatch.setattr(grabador, '_start_stereo_mix_loopback', lambda still=None: False)
    t = threading.Thread(target=grabador._start_loopback_async, daemon=True)
    t.start()

    time.sleep(0.6)
    assert len(avisos) == 1, "no aviso al usuario a tiempo"
    liberar.set()
    grabador._recording = False
    t.join(3)
    assert len(avisos) == 1, "aviso duplicado"


def test_sin_problemas_no_hay_aviso(grabador, monkeypatch):
    monkeypatch.setattr(ar, '_LOOPBACK_WARN_AFTER', 0.2)
    avisos = []
    grabador.on_loopback_unavailable = avisos.append
    monkeypatch.setattr(grabador, '_start_wasapi_loopback', lambda still=None: True)

    grabador._start_loopback_async()
    time.sleep(0.5)

    assert avisos == [] and grabador.loopback_active is True


def test_worker_que_tarda_mas_que_el_viejo_limite_de_15s_acaba_funcionando(grabador, monkeypatch):
    """El limite viejo (15 s) mataba a un worker que solo estaba lento. Aqui,
    proporcionalmente: tarda mas que antes el limite y mucho menos que el nuevo."""
    monkeypatch.setattr(ar, '_LOOPBACK_HEADER_TIMEOUT', 2.0)
    monkeypatch.setattr(ar, '_LOOPBACK_STAGGER', 0.5)
    lento = Worker(delay=1.2)
    lanzar(monkeypatch, grabador, [lento, Worker(never=True), Worker(never=True)])

    assert grabador._start_wasapi_loopback(lambda: True) is True
    assert grabador._loopback_proc is lento
