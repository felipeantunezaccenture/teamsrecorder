"""Disco lleno a mitad de una reunion (26/10/2026).

La reunion de las 16:06 se quedo con 8,8 de ~19,5 minutos. El pagefile de
Windows lleno el disco y, a las 16:15, libsndfile no pudo escribir el audio.
Tres fallos, uno por test o grupo de tests:

1. El writer solo avisaba con OSError, y libsndfile lanza LibsndfileError (un
   RuntimeError): nadie se entero.
2. El writer moria al primer fallo; el resto de la reunion se quedo en la cola,
   en memoria, y se tiro al colgar. Ahora espera en memoria y reintenta.
3. Si el WAV final no se podia escribir, el finally borraba igual los .part: la
   reunion entera perdida. Ahora se conservan y se reintenta.

Y lo que lo rodea: rutas por grabacion (dos reuniones seguidas), .part
huerfanos que nadie convertia, la vigilancia del disco y su reserva, y el modo
de fondo, que no debe empujar el pagefile mientras se graba.
"""
import queue
import threading
import time

import numpy as np
import pytest
import soundfile as sf

import audio_recorder as ar
import disk_guard
import live_transcriber
from audio_recorder import AudioRecorder
from config import SAMPLE_RATE

pytestmark = pytest.mark.unit


def _disco_lleno():
    """El error real del 26/10: 'System error.' de libsndfile (SF_ERR_SYSTEM)."""
    return sf.LibsndfileError(2, prefix='Error writing: ')


def _esperar(cond, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _part(path, data):
    with sf.SoundFile(str(path), mode='w', samplerate=SAMPLE_RATE, channels=1,
                      format='WAV', subtype='FLOAT') as f:
        f.write(np.asarray(data, dtype=np.float32))


# ── 1 y 2: el writer no muere ni tira audio ─────────────────────────────────

@pytest.fixture
def escritura_que_falla(monkeypatch):
    """SoundFile.write falla las veces pedidas, como con el disco lleno, y
    despues escribe de verdad."""
    real = sf.SoundFile.write
    estado = {'fallos': 0}

    def write(self, data):
        if estado['fallos'] > 0:
            estado['fallos'] -= 1
            raise _disco_lleno()
        return real(self, data)

    monkeypatch.setattr(sf.SoundFile, 'write', write)
    monkeypatch.setattr(ar, '_WRITE_RETRY_S', 0.05, raising=False)
    return estado


def test_el_writer_guarda_en_memoria_y_reintenta_sin_perder_bloques(tr_dirs, escritura_que_falla):
    rec = AudioRecorder()
    avisos, recuperado = [], []
    rec.on_write_error = avisos.append
    rec.on_write_recovered = lambda: recuperado.append(1)
    path = tr_dirs / 'm.mic.part'
    q = queue.Queue()
    th = threading.Thread(target=rec._writer_loop, args=(q, path, 1, SAMPLE_RATE, 'mic'), daemon=True)

    bloques = [np.full(1600, i / 100, dtype=np.float32) for i in range(30)]
    q.put(bloques[0])
    th.start()
    assert _esperar(lambda: path.exists() and sf.info(str(path)).frames == 1600)
    escritura_que_falla['fallos'] = 5           # el disco se llena
    for b in bloques[1:]:
        q.put(b)
    q.put(None)
    th.join(timeout=10)

    assert not th.is_alive()
    data, _ = sf.read(str(path), dtype='float32')
    assert np.array_equal(data, np.concatenate(bloques)), "faltan bloques o estan desordenados"
    assert _esperar(lambda: avisos and recuperado)
    assert len(avisos) == 1 and 'System error' in avisos[0]
    assert len(recuperado) == 1


def test_el_aviso_salta_con_el_error_de_libsndfile_no_solo_con_oserror(tr_dirs, escritura_que_falla):
    """Fallo 1: LibsndfileError no es OSError y el aviso nunca salia."""
    assert not issubclass(sf.LibsndfileError, OSError)
    rec = AudioRecorder()
    avisos = []
    rec.on_write_error = avisos.append
    q = queue.Queue()
    escritura_que_falla['fallos'] = 1
    th = threading.Thread(target=rec._writer_loop, args=(q, tr_dirs / 'x.part', 1, SAMPLE_RATE, 'mic'),
                          daemon=True)
    th.start()
    q.put(np.zeros(800, dtype=np.float32))
    q.put(None)
    th.join(timeout=10)
    assert _esperar(lambda: avisos)


def test_cancelar_suelta_un_writer_que_reintenta(tr_dirs, escritura_que_falla):
    """Una grabacion descartada con el disco lleno no deja un hilo reintentando
    para siempre (ni recrea el .part que cancel() borra)."""
    rec = AudioRecorder()
    abort = threading.Event()
    q = queue.Queue()
    escritura_que_falla['fallos'] = 10 ** 6
    th = threading.Thread(target=rec._writer_loop, args=(q, tr_dirs / 'c.part', 1, SAMPLE_RATE, 'mic'),
                          kwargs={'abort': abort}, daemon=True)
    th.start()
    q.put(np.zeros(800, dtype=np.float32))
    q.put(None)
    time.sleep(0.2)
    abort.set()
    th.join(timeout=5)
    assert not th.is_alive()


def test_reabrir_tras_una_escritura_a_medias_con_libsndfile_real(tr_dirs):
    """La reapertura 'r+' contra libsndfile de verdad: la basura que deja una
    escritura a medias se quita y el audio sigue en orden."""
    path = tr_dirs / 'r.mic.part'
    a = np.linspace(-0.5, 0.5, 1600, dtype=np.float32)
    b = np.full(1600, 0.25, dtype=np.float32)
    _part(path, a)
    with open(path, 'ab') as raw:
        raw.write(b'\x01\x02\x03' * 333)

    f = AudioRecorder._open_part(path, 1, SAMPLE_RATE, frames=1600)
    f.write(b)
    f.close()

    data, _ = sf.read(str(path), dtype='float32')
    assert np.array_equal(data, np.concatenate([a, b]))


def test_tras_reabrir_un_cierre_brusco_no_corta_la_reunion(tr_dirs):
    """En 'r+' libsndfile solo reescribe el header al cerrar: si el daemon muere
    despues, el .part diria que solo tiene lo de antes de reabrir, y la
    recuperacion cortaria ahi (medido el 26/10, revision del cambio)."""
    import subprocess
    import sys
    path = tr_dirs / 'k.mic.part'
    _part(path, np.full(4800, 0.1, dtype=np.float32))
    hijo = (
        "import os, sys, numpy as np, audio_recorder as ar\n"
        "f = ar.AudioRecorder._open_part(ar.Path(sys.argv[1]), 1, ar.SAMPLE_RATE, frames=4800)\n"
        "for _ in range(5):\n"
        "    f.write(np.full(1600, 0.2, dtype=np.float32)); f.flush(); ar._update_header_now(f)\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, '-c', hijo, str(path)], check=True,
                   cwd=str(ar.Path(ar.__file__).parent))
    assert sf.info(str(path)).frames == 4800 + 5 * 1600


def test_sin_audio_confirmado_se_empieza_de_nuevo(tr_dirs):
    path = tr_dirs / 'h.mic.part'
    path.write_bytes(b'RIFF\x00\x00')            # un header a medias: nada que perder
    f = AudioRecorder._open_part(path, 1, SAMPLE_RATE, frames=0)
    f.write(np.zeros(160, dtype=np.float32))
    f.close()
    assert sf.info(str(path)).frames == 160


def test_una_escritura_a_medias_no_duplica_audio(tr_dirs, monkeypatch):
    """Si libsndfile llega a escribir parte del bloque antes de fallar, el
    reintento no debe repetir esa parte: desfasaria el micro del sistema."""
    monkeypatch.setattr(ar, '_WRITE_RETRY_S', 0.05, raising=False)
    real = sf.SoundFile.write
    estado = {'n': 0}

    def write(self, data):
        estado['n'] += 1
        if estado['n'] == 2:                      # el segundo bloque, a medias
            real(self, data[:len(data) // 2])
            raise _disco_lleno()
        return real(self, data)

    monkeypatch.setattr(sf.SoundFile, 'write', write)
    rec = AudioRecorder()
    path, q = tr_dirs / 'd.mic.part', queue.Queue()
    bloques = [np.full(1600, i / 10, dtype=np.float32) for i in range(1, 5)]
    for b in bloques:
        q.put(b)
    q.put(None)
    rec._writer_loop(q, path, 1, SAMPLE_RATE, 'mic')

    data, _ = sf.read(str(path), dtype='float32')
    assert np.array_equal(data, np.concatenate(bloques))


def test_cancelar_corta_tambien_mientras_vacia_lo_pendiente_y_borra_el_part(tr_dirs, monkeypatch):
    """Con un atasco largo, cancel() agotaba su espera con el .part abierto: no
    se podia borrar y la reunion descartada volvia al arrancar."""
    real = sf.SoundFile.write

    def write_lento(self, data):
        time.sleep(0.01)
        return real(self, data)

    monkeypatch.setattr(sf.SoundFile, 'write', write_lento)
    rec = AudioRecorder()
    abort, q = threading.Event(), queue.Queue()
    path = tr_dirs / 'atasco.mic.part'
    for _ in range(2000):                         # ~20 s de escritura
        q.put(np.zeros(160, dtype=np.float32))
    q.put(None)
    th = threading.Thread(target=rec._writer_loop, args=(q, path, 1, SAMPLE_RATE, 'mic'),
                          kwargs={'abort': abort}, daemon=True)
    th.start()
    time.sleep(0.3)
    t0 = time.monotonic()
    abort.set()
    th.join(timeout=5)
    assert not th.is_alive() and time.monotonic() - t0 < 2
    assert not path.exists()


def test_si_borran_el_part_entre_reintentos_sigue_con_lo_pendiente(tr_dirs, escritura_que_falla):
    """El aviso pide liberar espacio: si alguien borra el .part (cerrado entre
    reintentos), 'r+' fallaria para siempre y el audio pendiente con el."""
    rec = AudioRecorder()
    path, q = tr_dirs / 'b.mic.part', queue.Queue()
    th = threading.Thread(target=rec._writer_loop, args=(q, path, 1, SAMPLE_RATE, 'mic'), daemon=True)
    q.put(np.full(1600, 0.1, dtype=np.float32))
    th.start()
    assert _esperar(lambda: path.exists() and sf.info(str(path)).frames == 1600)
    escritura_que_falla['fallos'] = 10 ** 6
    q.put(np.full(1600, 0.2, dtype=np.float32))
    assert _esperar(lambda: rec._failing)
    path.unlink()
    escritura_que_falla['fallos'] = 0
    q.put(np.full(1600, 0.3, dtype=np.float32))
    q.put(None)
    th.join(timeout=10)
    assert not th.is_alive()
    data, _ = sf.read(str(path), dtype='float32')
    assert np.array_equal(data, np.concatenate([np.full(1600, 0.2), np.full(1600, 0.3)]).astype(np.float32))


def test_start_y_cancel_usan_bien_el_lock(tr_dirs):
    """_begin entero bajo el lock (un stop() durante el InputStream dejaba un
    stream sin cerrar), y los joins de cancel fuera (no frenan el start)."""
    rec = AudioRecorder()
    visto = {}

    def begin(path):
        visto['begin'] = rec._start_lock.locked()
        rec._output_path, rec._tmp_mic, rec._tmp_loop = path, None, None

    class Hilo:
        def join(self, timeout=None):
            visto['join'] = rec._start_lock.locked()

    rec._begin = begin
    rec._close_inputs = lambda: None
    rec._detach_writers = lambda: [Hilo()]
    rec.start(tr_dirs / 'l.wav')
    rec.cancel()
    assert visto == {'begin': True, 'join': False}


def test_dos_writers_fallando_dan_un_aviso_y_uno_de_vuelta(tr_dirs, escritura_que_falla):
    """Que se recupere el micro con el sistema aun fallando no es 'todo bien'."""
    rec = AudioRecorder()
    avisos, recuperado = [], []
    rec.on_write_error = avisos.append
    rec.on_write_recovered = lambda: recuperado.append(1)
    rec._set_failing('mic:x', 'lleno')
    rec._set_failing('loop:x', 'lleno')
    rec._set_failing('mic:x', None)
    assert _esperar(lambda: avisos) and not recuperado
    rec._set_failing('loop:x', None)
    assert _esperar(lambda: recuperado)
    assert len(avisos) == 1 and len(recuperado) == 1


def test_el_filtro_de_cada_writer_es_suyo():
    """Con stop() sin esperar, el writer de una reunion y el de la siguiente
    pueden convivir: compartir el estado del paso-alto mete clics."""
    rec = AudioRecorder()
    rec._reset_dc_filter()
    x = np.full(1600, 0.5, dtype=np.float32)
    a, b = [0.0, 0.0], [0.0, 0.0]
    uno = rec._dc_block(x, a)
    rec._dc_block(np.full(1600, -0.9, dtype=np.float32), b)
    dos = rec._dc_block(x, a)
    solo = AudioRecorder()
    solo._reset_dc_filter()
    assert np.allclose(np.concatenate([uno, dos]), np.concatenate([solo._dc_block(x), solo._dc_block(x)]))
    assert (rec._dc_x1, rec._dc_y1) == (0.0, 0.0), "toco el estado de la instancia"


def test_stop_suelta_los_writers_con_el_lock_de_start(tr_dirs):
    """Sin el lock, un start() de la reunion siguiente podia colarse entre bajar
    el flag y soltar los writers, y esta se quedaba con los de aquella."""
    rec = AudioRecorder()
    visto = []
    rec._close_inputs = lambda: visto.append(rec._start_lock.locked())
    rec._recording = True
    rec._output_path = tr_dirs / 's.wav'
    rec._tmp_mic, rec._tmp_loop = tr_dirs / 's.mic.part', None
    rec.stop()
    assert visto == [True]
    rec.wait_for_save(5)


def test_el_centinela_no_se_quita_con_otro_guardado_pendiente(tr_dirs, monkeypatch):
    """.recording_active es la puerta de reinicio: con audio de otra reunion aun
    en memoria, reiniciar el daemon lo perderia."""
    # Su ruta se fija al importar: sin esto el test escribiria en la carpeta real.
    monkeypatch.setattr(ar, '_RECORDING_SENTINEL', tr_dirs / '.recording_active')
    ar._write_sentinel()
    rec = AudioRecorder()
    rec._saving_stems.add('otra')
    rec._remove_sentinel_if_idle()
    assert ar._RECORDING_SENTINEL.exists()
    rec._saving_stems.clear()
    rec._remove_sentinel_if_idle()
    assert not ar._RECORDING_SENTINEL.exists()


# ── 3: el guardado no borra los .part si el WAV no se pudo escribir ─────────

def test_el_guardado_conserva_los_part_y_reintenta_hasta_escribir_el_wav(tr_dirs, monkeypatch):
    monkeypatch.setattr(ar, '_SAVE_RETRY_S', 0.01, raising=False)
    rec = AudioRecorder()
    guardados = []
    rec.on_recording_stopped = guardados.append
    mic, out = tr_dirs / 'g.mic.part', tr_dirs / 'g.wav'
    _part(mic, np.full(SAMPLE_RATE, 0.3, dtype=np.float32))

    real = AudioRecorder._write_mix
    intentos = []

    def write_mix(out_path, *a):
        intentos.append(mic.exists())
        if len(intentos) < 3:
            out_path.write_bytes(b'RIFF a medias')
            raise _disco_lleno()
        return real(out_path, *a)

    monkeypatch.setattr(AudioRecorder, '_write_mix', staticmethod(write_mix))
    rec._process_and_save(mic, None, out, [])

    assert intentos == [True, True, True], "los .part se borraron antes de tener el WAV"
    assert sf.info(str(out)).frames == SAMPLE_RATE
    assert not mic.exists()
    assert _esperar(lambda: guardados == [out])


def test_un_error_que_no_se_arregla_esperando_no_se_reintenta_para_siempre(tr_dirs):
    """Un .part ilegible no es disco lleno: reintentarlo cada 30 s dejaba la
    puerta de reinicio cerrada. Se avisa, se conserva y se suelta."""
    rec = AudioRecorder()
    fallos = []
    rec.on_save_failed = fallos.append
    mic, out = tr_dirs / 'roto.mic.part', tr_dirs / 'roto.wav'
    mic.write_bytes(b'esto no es un wav' * 400)
    rec._saving_stems.add('roto')
    rec._save_event.clear()

    rec._process_and_save(mic, None, out, [])

    assert mic.exists(), "se borro un .part que nunca se guardo"
    assert not out.exists()
    assert rec._save_event.is_set() and not rec._saving_stems
    assert _esperar(lambda: fallos)


def test_que_errores_se_reintentan():
    assert ar._is_retryable(_disco_lleno())
    assert ar._is_retryable(OSError(28, 'No space left on device'))
    assert not ar._is_retryable(sf.LibsndfileError(1))           # formato no reconocido
    assert not ar._is_retryable(ValueError('fallo del codigo'))


def test_un_guardado_fallido_no_deja_un_wav_a_medias(tr_dirs):
    out = tr_dirs / 'p.wav'
    for p in (out, out.with_suffix('.mic.wav'), out.with_suffix('.loop.wav')):
        p.write_bytes(b'a medias')
    AudioRecorder._remove_outputs(out)
    assert not list(tr_dirs.glob('p.*'))


def test_el_guardado_de_una_reunion_no_toca_los_part_de_la_siguiente(tr_dirs):
    """Dos reuniones seguidas, como las de las 15:33 y 16:06 del 26/10: cuando
    termina el guardado de la primera, self._tmp_* ya son los de la segunda."""
    rec = AudioRecorder()
    viejo, nuevo = tr_dirs / 'a.mic.part', tr_dirs / 'b.mic.part'
    _part(viejo, np.full(800, 0.3, dtype=np.float32))
    _part(nuevo, np.full(800, 0.3, dtype=np.float32))
    rec._tmp_mic, rec._tmp_loop = nuevo, None

    rec._process_and_save(viejo, None, tr_dirs / 'a.wav', [])

    assert (tr_dirs / 'a.wav').exists()
    assert nuevo.exists(), "se borro el audio de la reunion que se estaba grabando"


def test_la_siguiente_grabacion_no_espera_al_guardado_anterior(tr_dirs):
    """Antes _begin esperaba hasta 30 s el guardado anterior antes de abrir el
    micro: con el disco lleno, esos 30 s se perdian de la reunion nueva."""
    rec = AudioRecorder()
    rec._saving_stems.add('anterior')
    rec._save_event.clear()

    class Parar(Exception):
        pass

    def no_abrir_micro(*a, **k):
        raise Parar()

    t0 = time.monotonic()
    import sounddevice as sd
    real = sd.InputStream
    sd.InputStream = no_abrir_micro
    try:
        with pytest.raises(Parar):
            rec._begin(tr_dirs / 'siguiente.wav')
    finally:
        sd.InputStream = real
        rec._close_writers()
    assert time.monotonic() - t0 < 5


# ── .part huerfanos al arrancar ─────────────────────────────────────────────

def test_al_arrancar_los_part_huerfanos_se_convierten_en_wav(tr_dirs):
    rec_dir = tr_dirs / 'recordings'
    mic, loop = rec_dir / '2026-10-26_16-06_manual.mic.part', rec_dir / '2026-10-26_16-06_manual.loop.part'
    _part(mic, np.full(SAMPLE_RATE, 0.3, dtype=np.float32))
    _part(loop, np.full(SAMPLE_RATE, 0.2, dtype=np.float32))
    sin_audio = rec_dir / '2026-10-16_09-41_Daily.mic.part'
    _part(sin_audio, np.zeros(0, dtype=np.float32))          # 80 bytes, como el del 16/10

    rec = AudioRecorder()
    recuperados = rec.recover_orphan_parts(rec_dir)

    out = rec_dir / '2026-10-26_16-06_manual.wav'
    assert recuperados == [out]
    assert sf.info(str(out)).frames == SAMPLE_RATE
    assert out.with_suffix('.mic.wav').exists() and out.with_suffix('.loop.wav').exists()
    assert not mic.exists() and not loop.exists()
    assert sin_audio.exists(), "un .part sin audio no es una reunion"


def test_los_part_de_la_grabacion_en_curso_no_se_tocan(tr_dirs):
    rec_dir = tr_dirs / 'recordings'
    mic = rec_dir / 'en_curso.mic.part'
    _part(mic, np.full(800, 0.3, dtype=np.float32))
    rec = AudioRecorder()
    rec._recording, rec._output_path = True, rec_dir / 'en_curso.wav'
    try:
        assert rec.recover_orphan_parts(rec_dir) == []
    finally:
        rec._recording = False
    assert mic.exists()


def test_si_ya_se_proceso_no_se_rehace(tr_dirs):
    rec_dir = tr_dirs / 'recordings'
    mic = rec_dir / 'hecha.mic.part'
    _part(mic, np.full(800, 0.3, dtype=np.float32))
    (rec_dir / 'processed').mkdir(exist_ok=True)
    (rec_dir / 'processed' / 'hecha.wav').write_bytes(b'x')
    assert AudioRecorder().recover_orphan_parts(rec_dir) == []
    assert not (rec_dir / 'hecha.wav').exists()


# ── Vigilancia del disco y reserva ──────────────────────────────────────────

def test_cada_nivel_avisa_una_vez_por_bajada():
    lv = disk_guard.Levels()
    seguidos = [lv.update(x) for x in (10, 2.5, 2.0, 0.8, 0.5, 3.5, 0.9, 4.5, 2.9, None)]
    assert seguidos == [None, 'warn', None, 'critical', None, None, None, None, 'warn', None]


@pytest.fixture
def reserva(tr_dirs, monkeypatch):
    """Ajusta disk_reserve_gb. El disco libre se simula holgado: con el de la
    maquina (a veces < 5 GB el 26/10) la reserva no se crearia y el test
    dependeria del dia. El fichero se crea de verdad en el disco."""
    monkeypatch.setattr(disk_guard, 'free_gb', lambda path=None: 50.0)

    def ajustar(gb):
        (tr_dirs / 'settings.json').write_text(f'{{"disk_reserve_gb": {gb}}}', encoding='utf-8')
    return ajustar


def test_la_reserva_ocupa_sitio_de_verdad(tr_dirs, reserva):
    """En el disco real: un fichero disperso no reservaria nada."""
    import ctypes
    reserva(0.01)                                    # ~10 MB
    assert disk_guard.ensure_reserve()
    path = disk_guard.reserve_file()
    assert path.stat().st_size == int(0.01 * disk_guard.GB)
    if hasattr(ctypes, 'windll'):
        assert not ctypes.windll.kernel32.GetFileAttributesW(str(path)) & 0x200, "es dispersa"
    assert disk_guard.release_reserve() == pytest.approx(0.01, rel=0.01)
    assert not path.exists()
    assert disk_guard.release_reserve() == 0.0


def test_la_reserva_no_se_crea_si_dejaria_poco_disco(tr_dirs, reserva, monkeypatch):
    reserva(2)
    monkeypatch.setattr(disk_guard, 'free_gb', lambda path=None: 6.5)
    assert not disk_guard.ensure_reserve()
    assert not disk_guard.reserve_file().exists()


def test_la_reserva_se_quita_si_se_apaga(tr_dirs, reserva):
    reserva(0.01)
    assert disk_guard.ensure_reserve()
    reserva(0)
    assert not disk_guard.ensure_reserve()
    assert not disk_guard.reserve_file().exists()


def test_sin_ajuste_no_hay_reserva(tr_dirs):
    assert disk_guard.reserve_gb() == 0.0
    assert not disk_guard.ensure_reserve()


def test_en_critico_la_bandeja_libera_la_reserva_y_avisa(tr_dirs, reserva, monkeypatch):
    import tray_app
    reserva(0.01)
    assert disk_guard.ensure_reserve()
    avisos = []
    tray = object.__new__(tray_app.TrayApp)
    tray._disk_levels = disk_guard.Levels()
    tray._notify = lambda title, msg: avisos.append(msg)
    libres = iter([2.5, 0.7])
    monkeypatch.setattr(disk_guard, 'free_gb', lambda path=None: next(libres))

    tray._check_disk()
    tray._check_disk()

    assert len(avisos) == 2
    assert '2.5' in avisos[0] or '2,5' in avisos[0]
    assert not disk_guard.reserve_file().exists()
    assert 'reserv' in avisos[1].lower()


def test_el_aviso_de_disco_lleno_no_se_repite_en_rafaga():
    import tray_app
    avisos = []
    tray = object.__new__(tray_app.TrayApp)
    tray._notify = lambda title, msg: avisos.append(msg)
    tray.warn_disk_full('mic')
    tray.warn_disk_full('mic')
    assert len(avisos) == 1


def test_tras_recuperarse_un_nuevo_lleno_se_avisa_enseguida():
    """El ultimo aviso visible no puede ser 'hay espacio' con el disco otra vez lleno."""
    import tray_app
    avisos = []
    tray = object.__new__(tray_app.TrayApp)
    tray._notify = lambda title, msg: avisos.append(msg)
    tray.warn_disk_full('mic')
    tray.warn_disk_recovered()
    tray.warn_disk_full('mic')
    assert len(avisos) == 3


def test_el_primer_fallo_de_escritura_libera_la_reserva(tr_dirs, reserva, monkeypatch):
    """Tambien fuera de una grabacion: un guardado final esperando sitio."""
    import tray_app
    reserva(0.01)
    assert disk_guard.ensure_reserve()
    monkeypatch.setattr(disk_guard, 'free_gb', lambda path=None: 0.4)
    avisos = []
    tray = object.__new__(tray_app.TrayApp)
    tray._notify = lambda title, msg: avisos.append(msg)
    tray.warn_disk_full('save')
    assert not disk_guard.reserve_file().exists()
    assert 'reserv' in avisos[0].lower()


def test_un_fichero_bloqueado_con_disco_de_sobra_no_gasta_la_reserva(tr_dirs, reserva, monkeypatch):
    """Un antivirus que bloquea el .part no es disco lleno (segunda revision)."""
    import tray_app
    reserva(0.01)
    assert disk_guard.ensure_reserve()
    monkeypatch.setattr(disk_guard, 'free_gb', lambda path=None: 40.0)
    avisos = []
    tray = object.__new__(tray_app.TrayApp)
    tray._notify = lambda title, msg: avisos.append(msg)
    tray.warn_disk_full('PermissionError')
    assert disk_guard.reserve_file().exists()
    assert avisos == [tray_app._STR[tray_app.get_ui_language()]['write_failed']]


def test_reponer_la_reserva_sin_grabador_no_rompe_el_cierre(tr_dirs):
    """El fixture de la bandeja crea la app sin grabador (_recorder=None)."""
    import tray_app
    tray = object.__new__(tray_app.TrayApp)
    tray._recorder = None
    tray._refresh_disk_reserve()


# ── El modo de fondo no empuja el pagefile ──────────────────────────────────

def test_el_modo_de_fondo_no_carga_medium_con_poco_disco(monkeypatch):
    monkeypatch.setattr(disk_guard, 'system_free_gb', lambda: 5.0)
    assert not live_transcriber._disk_ok('medium')
    monkeypatch.setattr(disk_guard, 'system_free_gb', lambda: 6.5)
    assert live_transcriber._disk_ok('medium')


def test_el_modo_de_fondo_suelta_el_modelo_bajo_2_gb(monkeypatch):
    monkeypatch.setattr(disk_guard, 'system_free_gb', lambda: 1.5)
    assert not live_transcriber._disk_floor_ok()
    monkeypatch.setattr(disk_guard, 'system_free_gb', lambda: None)
    assert live_transcriber._disk_floor_ok(), "sin poder medir, no se para"


def test_uno_que_espera_disco_no_deja_al_pipeline_en_pausa(tr_dirs):
    """Pausar el pipeline con 'medium' ya cargado toda la reunion no libera nada
    y nadie transcribiria (segunda revision del cambio)."""
    wav = tr_dirs / 'recordings' / 'w.wav'
    live_transcriber._write_status(wav, state='waiting_disk')
    assert not live_transcriber.child_usable(wav)


def test_un_modelo_pequeno_no_se_carga_y_suelta_en_bucle(monkeypatch):
    _free, need = live_transcriber._disk_free_and_need('tiny')
    assert need > live_transcriber.DISK_FLOOR_GB
