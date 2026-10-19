"""Transcripcion en vivo contra Windows de verdad: ficheros y procesos reales.

Los dos modos de fallo que importan, y por que no se pueden probar con mocks:
1. Que el lector en vivo IMPIDA al grabador escribir o recrear su .part. Eso
   depende de los modos de comparticion de Windows (CreateFile), no de Python.
   Si pasara, se perderia el audio de la reunion.
2. Que al parar quede vivo el interprete real. El lanzador pythonw del venv
   arranca el interprete como hijo; matar solo el Popen podria dejar un proceso
   de ~1 GB con las pistas abiertas, y entonces el borrado de .mic.wav al
   transcribir (WinError 32) tumbaria la transcripcion.
"""
import subprocess
import sys
import time

import numpy as np
import psutil
import pytest
import soundfile as sf

import live_transcriber as lt

pytestmark = pytest.mark.integration

SR = lt.SR


_READER = r'''
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import live_transcriber as lt
path, stop = Path(sys.argv[2]), Path(sys.argv[3])
reads, pos = 0, 0
(stop.parent / 'ready').write_text('x')
while not stop.exists():
    audio, total = lt.read_part(path, pos, 16000)
    pos = pos + len(audio) if total >= pos else 0
    reads += 1
print(reads)
'''


def test_el_lector_en_vivo_nunca_impide_escribir_ni_recrear_el_part(tmp_path):
    """Un escritor como el del grabador (SoundFile + flush por bloque) contra el
    lector en vivo en OTRO proceso, como en produccion, leyendo sin parar. 40
    veces se crea, se escribe, se cierra y se borra el fichero, como un reintento
    del loopback. Abrir para escribir no puede fallar nunca. Un borrado que
    coincida con una lectura si puede fallar (el grabador ya lo tolera), y la
    siguiente apertura reutiliza el fichero.
    """
    from pathlib import Path
    repo = str(Path(lt.__file__).resolve().parent)
    path, stop = tmp_path / 'x.loop.part', tmp_path / 'stop'
    reader = subprocess.Popen([sys.executable, '-c', _READER, repo, str(path), str(stop)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    borrados_fallidos = 0
    try:
        # Arrancar un proceso aqui tarda segundos (imports y antivirus): se
        # espera a que el lector diga que ya esta leyendo.
        deadline = time.monotonic() + 30
        while not (tmp_path / 'ready').exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        for _ in range(40):
            with sf.SoundFile(str(path), mode='w', samplerate=SR, channels=1,
                              format='WAV', subtype='FLOAT') as f:
                for _ in range(20):
                    f.write(np.full(1600, 0.1, dtype=np.float32))
                    f.flush()
                    time.sleep(0.002)
            try:
                path.unlink()
            except PermissionError:
                borrados_fallidos += 1
    finally:
        stop.write_text('x')
        out, err = reader.communicate(timeout=10)
    assert reader.returncode == 0, err
    reads = int(out.strip())
    # Abrir un fichero aqui cuesta milisegundos (antivirus) y el numero de
    # lecturas varia mucho (15-205 medidas). La garantia determinista esta en
    # test_con_el_fichero_abierto_por_el_lector_el_grabador_puede_recrearlo.
    assert reads >= 1, "el lector no llego a leer"
    print(f"lecturas: {reads}, borrados que coincidieron con una lectura: {borrados_fallidos}/40")


def test_con_el_fichero_abierto_por_el_lector_el_grabador_puede_recrearlo(tmp_path):
    """La garantia, sin depender de tiempos: con el fichero abierto exactamente
    como lo abre read_part, el grabador puede recrearlo (lo hace si falla un
    intento de loopback) y seguir escribiendo. Lo unico que no puede es borrarlo
    en ese instante, y eso el grabador ya lo tolera."""
    path = tmp_path / 'x.loop.part'
    sf.write(str(path), np.zeros(SR, dtype=np.float32), SR, format='WAV', subtype='FLOAT')
    with open(path, 'rb'):                   # el handle de read_part
        with sf.SoundFile(str(path), mode='w', samplerate=SR, channels=1,
                          format='WAV', subtype='FLOAT') as f:
            f.write(np.full(SR, 0.2, dtype=np.float32))
            f.flush()
        with pytest.raises(PermissionError):
            path.unlink()
    audio, total = lt.read_part(path, 0, 2 * SR)
    assert total == SR and np.allclose(audio, 0.2)
    path.unlink()                            # sin el handle, el borrado funciona


def test_lo_leido_en_vivo_es_exactamente_lo_escrito(tmp_path):
    path = tmp_path / 'x.mic.part'
    data = np.random.default_rng(0).uniform(-0.5, 0.5, 5 * SR).astype(np.float32)
    got = []
    with sf.SoundFile(str(path), mode='w', samplerate=SR, channels=1, format='WAV', subtype='FLOAT') as f:
        pos = 0
        for i in range(0, len(data), 1600):
            f.write(data[i:i + 1600])
            f.flush()
            audio, _ = lt.read_part(path, pos, SR)
            got.append(audio)
            pos += len(audio)
    audio, _ = lt.read_part(path, pos, 10 * SR)
    got.append(audio)
    assert np.array_equal(np.concatenate(got), data)


def _sleeper():
    return subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def _family(proc):
    # En un venv de Windows, python.exe es un lanzador: el interprete real es
    # su hijo. Se espera a que exista para probar que tambien muere.
    # Solo los python: en una consola, Windows cuelga tambien un conhost.exe.
    in_venv = sys.prefix != sys.base_prefix
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        p = psutil.Process(proc.pid)
        kids = [k for k in p.children(recursive=True) if k.name().lower().startswith('python')]
        if kids or not in_venv:
            return [p] + kids
        time.sleep(0.1)
    return [psutil.Process(proc.pid)]


def _running(family, timeout=10):
    """Espera a que el interprete real este de verdad en marcha.

    Medido el 18/10/2026: recien nacido, Windows deniega el acceso al proceso
    unos segundos y aparece suspendido. Por eso spawn() no lanza nada hasta
    START_DELAY_S; aqui se reproduce ese mismo punto de partida.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if all(p.status() == psutil.STATUS_RUNNING for p in family) \
                    and time.time() - family[-1].create_time() > 3:
                return family
        except psutil.Error:
            pass
        time.sleep(0.2)
    return family


def test_kill_mata_tambien_el_interprete_real():
    proc = _sleeper()
    family = _running(_family(proc))
    lt.kill(proc, None)
    _gone, alive = psutil.wait_procs(family, timeout=5)
    assert not alive, f"siguen vivos: {[p.pid for p in alive]}"


def test_kill_por_el_pid_que_dejo_el_proceso(tmp_path):
    """Tras un reinicio del daemon no hay Popen: se mata por el pid de .live.json."""
    proc = _sleeper()
    real = _running(_family(proc))[-1]
    wav = tmp_path / 'r.wav'
    lt._write_status(wav, pid=real.pid, create_time=real.create_time())
    lt.kill(None, wav)
    # El lanzador termina cuando muere su hijo: es la señal fiable. psutil, en
    # esta maquina, a veces cree que el pid se reutilizo porque la hora de
    # creacion que lee varia unas decimas entre llamadas.
    try:
        proc.wait(timeout=5)
    finally:
        lt.kill(proc, None)
    assert proc.returncode is not None


def test_kill_no_mata_a_otro_proceso_con_el_mismo_pid(tmp_path):
    """Si el pid se reutilizo, la hora de creacion no coincide: no se toca."""
    proc = _sleeper()
    real = _running(_family(proc))[-1]
    wav = tmp_path / 'r.wav'
    lt._write_status(wav, pid=real.pid, create_time=real.create_time() - 3600)
    try:
        lt.kill(None, wav)
        time.sleep(0.5)
        assert proc.poll() is None, "mato a un proceso que no era el suyo"
    finally:
        lt.kill(proc, None)
