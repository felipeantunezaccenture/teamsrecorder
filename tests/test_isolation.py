"""Comprueba que el aislamiento del conftest realmente aisla.

Es el test que valida la red antes de confiar en ella.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parent.parent


def test_las_rutas_apuntan_a_tmp_no_al_repo(tr_dirs):
    import config
    import storage
    import tray_app

    for mod, attr in ((config, 'PROJECT_DIR'), (storage, 'RECORDINGS_DIR'),
                      (storage, 'MINUTES_DIR'), (tray_app, 'MINUTES_DIR'),
                      (tray_app, 'RECORDINGS_DIR'), (tray_app, 'PROJECT_DIR')):
        value = Path(getattr(mod, attr))
        assert tr_dirs in value.parents or value == tr_dirs, f"{attr} = {value}"


def test_write_status_escribe_en_tmp_no_en_el_repo(tray, tr_dirs):
    """_write_status re-importa config.PROJECT_DIR dentro del cuerpo: es la
    costura asimetrica que haria fallar al primer test ingenuo."""
    real = REPO / '.pipeline_status.json'
    antes = real.read_bytes() if real.exists() else None

    tray._write_status()

    assert (tr_dirs / '.pipeline_status.json').exists()
    # El fichero real puede existir (lo escribe el daemon en marcha); lo que no
    # puede es haberlo escrito este test.
    despues = real.read_bytes() if real.exists() else None
    if antes is None:
        assert despues is None, "el test creo el fichero de estado real"
    else:
        assert despues is not None


def test_construir_tray_no_arranca_hilos(tray):
    """El constructor real arranca 3 hilos, uno capaz de mandar un email."""
    import threading

    nombres = {t.name for t in threading.enumerate()}
    for prohibido in ('PipelineWorker', 'PipelineRecovery', 'NotificationPoller'):
        assert prohibido not in nombres


def test_tasks_store_no_escribe_en_el_tablero_real(tr_dirs):
    """El tablero real esta en .gitignore, asi que en un clon limpio (CI) no
    existe y en la maquina del usuario si. Se comprueban los dos casos: que no
    se cree, y que si ya estaba no se toque."""
    import tasks_store

    real = REPO / 'tasks.json'
    antes = real.read_bytes() if real.exists() else None

    tasks_store.create_task(project_id='none', title='tarea de prueba')

    assert (tr_dirs / 'tasks.json').exists(), "la tarea deberia ir al tablero temporal"
    if antes is None:
        assert not real.exists(), "el test creo el tablero real"
    else:
        assert real.read_bytes() == antes, "el test modifico el tablero real"


def test_no_queda_handler_escribiendo_en_el_log_real():
    """main.py y cli.py adjuntan un FileHandler sobre noted.log al
    importarse. El conftest lo quita: la suite no debe ensuciar el log que se
    usa para diagnosticar crashes en produccion."""
    import logging

    real_log = REPO / 'noted.log'
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler):
            assert Path(handler.baseFilename) != real_log


def test_todos_los_modulos_de_la_app_importan():
    """Si un modulo no importa, ningun test sobre el correria y el fallo
    pasaria desapercibido."""
    from conftest import IMPORT_ERRORS

    assert not IMPORT_ERRORS, {k: repr(v) for k, v in IMPORT_ERRORS.items()}


def test_wav_factory_genera_audio_real(wav_factory, tr_dirs):
    import soundfile as sf

    p = wav_factory(tr_dirs / 'recordings' / 'x.wav', seconds=0.5, samplerate=8000)
    info = sf.info(str(p))
    assert info.samplerate == 8000
    assert abs(info.duration - 0.5) < 0.01
