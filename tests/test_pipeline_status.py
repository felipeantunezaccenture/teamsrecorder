"""El fichero .pipeline_status.json: el contrato entre el daemon y la web.

Es el UNICO canal por el que el proceso del daemon le cuenta a la ventana web
(`web/app.js`) que hay una grabacion o un trabajo en curso. Si su forma cambia
sin avisar, el panel deja de pintar el progreso y el usuario cree que la app
esta colgada, exactamente lo que paso el 17/09 con la barra clavada en el 13%.

CONSUMIDOR: app_window.get_pipeline_status() -> web/app.js
"""
import json

import pytest

import config
import tray_app as ta

pytestmark = pytest.mark.unit

STEM = '2026-09-17_16-16_manual'


@pytest.fixture
def leer_estado(tr_dirs):
    """Lee el fichero de estado. Ojo: _write_status re-importa
    config.PROJECT_DIR dentro del cuerpo, asi que la costura es config, no
    tray_app (ver el docstring del conftest)."""
    def leer():
        p = config.PROJECT_DIR / '.pipeline_status.json'
        return json.loads(p.read_text(encoding='utf-8'))
    return leer


# ── Sin actividad ─────────────────────────────────────────────────────────

def test_sin_actividad_el_fichero_es_una_lista_vacia(tray, leer_estado):
    """La web lo interpreta como 'Sin actividad'. Si en vez de {'jobs': []}
    saliera otra cosa, pintaria un panel roto."""
    tray._write_status()
    assert leer_estado() == {'jobs': []}


def test_write_status_nunca_lanza_aunque_el_directorio_no_exista(tray, monkeypatch,
                                                                 tr_dirs):
    """Escribir el estado no puede tumbar una grabacion en curso."""
    monkeypatch.setattr(config, 'PROJECT_DIR', tr_dirs / 'no' / 'existe')
    tray._write_status()          # no debe lanzar


# ── Trabajo en grabacion ──────────────────────────────────────────────────

@pytest.fixture
def grabando(tray, tr_dirs):
    import time
    tray._recording_path = ta.RECORDINGS_DIR / f'{STEM}.wav'
    tray._recording_start = time.time() - 75         # 1:15 grabando
    tray._write_status()
    return tray


def test_la_grabacion_aparece_con_stage_recording(grabando, leer_estado):
    jobs = leer_estado()['jobs']
    assert len(jobs) == 1
    assert jobs[0]['stage'] == 'recording'


def test_la_grabacion_NO_lleva_stem(grabando, leer_estado):
    """Es lo que impide que la web pinte el boton de descartar sobre una
    grabacion en curso: para eso esta el boton de parar."""
    assert 'stem' not in leer_estado()['jobs'][0]


def test_la_grabacion_lleva_titulo_y_hora_derivados_del_stem(grabando, leer_estado):
    job = leer_estado()['jobs'][0]
    assert job['title'] == 'Manual'
    assert job['time'] == '16:16'


def test_el_titulo_se_capitaliza_destruyendo_los_acronimos(tray, leer_estado, tr_dirs):
    """Se fija el comportamiento actual: .title() convierte 'daily_scrum_ABC'
    en 'Daily Scrum Abc'. Cambiarlo es una decision, no un descuido."""
    import time
    tray._recording_path = ta.RECORDINGS_DIR / '2026-09-17_16-16_daily_scrum_ABC.wav'
    tray._recording_start = time.time()
    tray._write_status()
    assert leer_estado()['jobs'][0]['title'] == 'Daily Scrum Abc'


def test_un_stem_sin_titulo_deja_title_en_none(tray, leer_estado, tr_dirs):
    import time
    tray._recording_path = ta.RECORDINGS_DIR / '2026-09-17_16-16.wav'
    tray._recording_start = time.time()
    tray._write_status()
    job = leer_estado()['jobs'][0]
    assert job['title'] is None
    assert job['time'] == '16:16'


def test_un_stem_no_conforme_deja_titulo_y_hora_en_none(tray, leer_estado, tr_dirs):
    import time
    tray._recording_path = ta.RECORDINGS_DIR / 'grabacion_suelta.wav'
    tray._recording_start = time.time()
    tray._write_status()
    job = leer_estado()['jobs'][0]
    assert job['title'] is None and job['time'] is None


def test_la_etiqueta_de_grabacion_lleva_el_tiempo_en_mm_ss(grabando, leer_estado):
    job = leer_estado()['jobs'][0]
    assert job['label'] == 'Recording 01:15'
    assert job['elapsed'] == 75


def test_la_grabacion_es_el_paso_cero_de_tres(grabando, leer_estado):
    """web/app.js pinta total_steps + 1 pildoras. Si alguien cambia el numero
    de pasos sin tocar la web, el panel queda desalineado."""
    job = leer_estado()['jobs'][0]
    assert job['step'] == 0
    assert job['total_steps'] == 3


# ── Trabajo en proceso ────────────────────────────────────────────────────

@pytest.mark.parametrize('mensaje, pct', [
    ('Transcribiendo 0%...', 0),
    ('Transcribiendo 95%...', 95),
    ('Generando minutas...', None),          # la web pinta shimmer
    ('Generando acciones...', None),
])
def test_el_porcentaje_se_extrae_del_mensaje(tray, leer_estado, mensaje, pct):
    tray._processing_msg = mensaje
    tray._write_status()
    assert leer_estado()['jobs'][0]['pct'] == pct


def test_el_porcentaje_exige_el_signo_y_no_pilla_cualquier_numero(tray, leer_estado):
    r"""El regex es (\d+)% y exige el signo, asi que un mensaje con numeros
    que no son porcentajes no produce una barra falsa."""
    tray._processing_msg = 'Paso 2 de 3 al 40%'
    tray._write_status()
    assert leer_estado()['jobs'][0]['pct'] == 40, "deberia coger el 40%, no el 2"

    tray._processing_msg = 'Paso 2 de 3'
    tray._write_status()
    assert leer_estado()['jobs'][0]['pct'] is None, "sin % no hay porcentaje"


def test_el_trabajo_en_proceso_arrastra_los_datos_de_current_job(tray, leer_estado):
    tray._processing_msg = 'Transcribiendo 50%...'
    tray._current_job = {'stem': STEM, 'title': 'Manual', 'time': '16:16',
                         'step': 1, 'total_steps': 3, 'step_label': 'Transcribiendo'}
    tray._write_status()

    job = leer_estado()['jobs'][0]
    assert job['stem'] == STEM
    assert job['step_label'] == 'Transcribiendo'


def test_el_trabajo_en_proceso_SI_lleva_stem(tray, leer_estado):
    """Es lo que habilita el boton de descartar en la web."""
    tray._processing_msg = 'Transcribiendo 50%...'
    tray._current_job = {'stem': STEM}
    tray._write_status()
    assert leer_estado()['jobs'][0]['stem'] == STEM


# ── Trabajos en cola ──────────────────────────────────────────────────────

def test_cada_trabajo_en_cola_aparece_con_su_stem(tray, leer_estado):
    tray._pipeline_queued = ['a', 'b', 'c']
    tray._write_status()

    jobs = leer_estado()['jobs']
    assert [j['stem'] for j in jobs] == ['a', 'b', 'c']
    assert all(j['stage'] == 'queued' and j['pct'] == 0 for j in jobs)


def test_el_orden_es_grabacion_proceso_y_luego_la_cola(tray, leer_estado, tr_dirs):
    """La web los renderiza en el orden en que llegan."""
    import time
    tray._recording_path = ta.RECORDINGS_DIR / f'{STEM}.wav'
    tray._recording_start = time.time()
    tray._processing_msg = 'Transcribiendo 10%...'
    tray._current_job = {'stem': 'en-proceso'}
    tray._pipeline_queued = ['en-cola']
    tray._write_status()

    assert [j['stage'] for j in leer_estado()['jobs']] == ['recording', 'processing', 'queued']


# ── set_recording / set_processing como maquina de estados ────────────────

def test_set_recording_activo_guarda_la_ruta_y_arranca_el_reloj(tray, tr_dirs, monkeypatch):
    monkeypatch.setattr(tray, '_set_icon', lambda *a: None)
    monkeypatch.setattr(ta.threading, 'Thread',
                        lambda *a, **k: type('T', (), {'start': lambda self: None})())
    path = ta.RECORDINGS_DIR / f'{STEM}.wav'

    tray.set_recording(True, path)

    assert tray._recording_path == path
    assert tray._recording_start is not None
    assert not tray._ticker_stop.is_set()


def test_set_recording_inactivo_limpia_y_para_el_reloj(tray, tr_dirs, monkeypatch):
    monkeypatch.setattr(tray, '_set_icon', lambda *a: None)
    tray._recording_start = 1.0
    tray._recording_path = ta.RECORDINGS_DIR / 'x.wav'

    tray.set_recording(False)

    assert tray._recording_path is None
    assert tray._recording_start is None
    assert tray._ticker_stop.is_set()


def test_al_parar_de_grabar_con_un_proceso_en_curso_el_icono_no_vuelve_a_idle(tray,
                                                                             monkeypatch):
    """Invariante bonito: si paras de grabar pero el pipeline sigue, el icono
    se queda en 'procesando', no en reposo."""
    iconos = []
    monkeypatch.setattr(tray, '_set_icon', lambda img, tip: iconos.append(img))
    tray._processing_msg = 'Transcribiendo 10%...'

    tray.set_recording(False)

    assert iconos == [ta._ICON_PROCESSING]


def test_set_processing_no_cambia_el_icono_si_hay_grabacion_activa(tray, monkeypatch):
    iconos = []
    monkeypatch.setattr(tray, '_set_icon', lambda img, tip: iconos.append(img))
    tray._recording_start = 1.0

    tray.set_processing('Transcribiendo 10%...')

    assert iconos == [], "el icono rojo de grabando no debe pisarse"


def test_set_processing_siempre_escribe_el_estado(tray, leer_estado, monkeypatch):
    monkeypatch.setattr(tray, '_set_icon', lambda *a: None)
    tray._recording_start = 1.0

    tray.set_processing('Transcribiendo 10%...')

    # La grabacion va primera en la lista, asi que el trabajo en proceso no es
    # jobs[0]: lo que se comprueba es que APARECE.
    etiquetas = [j['label'] for j in leer_estado()['jobs']]
    assert 'Transcribiendo 10%...' in etiquetas


def test_set_processing_vacio_vuelve_a_reposo(tray, monkeypatch):
    iconos = []
    monkeypatch.setattr(tray, '_set_icon', lambda img, tip: iconos.append(img))
    tray.set_processing('')
    assert iconos == [ta._ICON_IDLE]
