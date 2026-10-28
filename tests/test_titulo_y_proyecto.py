"""Titulo y proyecto siempre, aunque Claude falle, y las minutas que fallan se
rehacen solas (28/10/2026).

Ese dia caduco la sesion de Claude. Dos reuniones grabadas con "Grabar ahora"
quedaron como "Reunion", sin proyecto, sin minutas y sin acciones: no habia
forma de saber cual era cual sin abrir el transcript. Y el log decia solo
"claude -p error (rc=1): ", porque el CLI escribe su error en stdout.

LO QUE ESTOS TESTS PROTEGEN
1. El titulo sin Claude: el de Teams (del fichero o de su .title), el asunto de
   la cita de Outlook, o la hora. Nunca "Reunion".
2. El proyecto sin Claude: el que nombra el titulo, nunca el transcript, y
   ninguno si dos empatan.
3. Las minutas que fallan quedan 'pending' con su grabacion y se rehacen en el
   MISMO fichero cuando Claude vuelve: nunca dos minutas para una reunion.
4. El aviso dice el motivo real (sin sesion, sin CLI).
"""
import json
import queue
import time
from datetime import datetime
from pathlib import Path

import pytest

import actions_enricher as ae
import minutes_generator as mg
import project_context as pc
import tray_app as ta
from audio_recorder import AudioRecorder

pytestmark = pytest.mark.unit

# La funcion real: conftest la sustituye en todos los tests (sin_claude_de_verdad).
CLAUDE_PROBLEM = mg.claude_problem

STEM = '2026-10-28_10-33_manual'
TRANSCRIPT = '[00:05] [Felipe]: Repasamos la sesion de formacion.\n[00:20] [Otros]: Vale.'
PROJECTS = [
    {'id': 'sales-training', 'name': 'Sales Training Presentation'},
    {'id': 'data-brain', 'name': 'Intelligent Data Brain'},
    {'id': 'knowledge-brain', 'name': 'Knowledge Brain'},
    {'id': 'atlas-project', 'name': 'Atlas Project'},
]
ES = ta._STR['es']


def ajustes(tr_dirs, **valores):
    (tr_dirs / 'settings.json').write_text(json.dumps(valores), encoding='utf-8')


# ── 1. El titulo sin Claude ──────────────────────────────────────────────────

@pytest.mark.parametrize('stem, nombre', [
    ('2026-10-28_10-34_GTM_Sales_Training', 'GTM Sales Training'),
    ('2026-10-28_10-34_Comite_Semanal__2', 'Comite Semanal'),     # sin el sufijo de colision
    ('2026-10-28_10-33_manual', None),
    ('2026-10-28_10-33_reunion', None),
    ('2026-10-28_14-32_Unión_a_la_reunión', None),                # titulo pasajero de Teams
    ('sin_fecha', None),
])
def test_el_nombre_del_fichero_solo_cuenta_si_es_una_reunion(tray, stem, nombre):
    assert tray._stem_meeting_name(stem) == nombre


class Detector:
    def __init__(self, nombre):
        self.current_meeting_name = nombre


def grabando(tray, stem, nombre):
    tray._recording_path = ta.RECORDINGS_DIR / f'{stem}.wav'
    tray._detector = Detector(nombre)
    return tray._recording_path.with_suffix('.title')


def test_una_grabacion_manual_apunta_el_titulo_de_teams(tray):
    """EL CASO: a las 14:33 Teams ya sabia el nombre de la reunion y la grabacion
    manual no lo guardo en ningun sitio."""
    titulo = grabando(tray, STEM, 'IFC Roadmapping on steroids')
    tray._note_teams_title()
    assert titulo.read_text(encoding='utf-8') == 'IFC Roadmapping on steroids'


def test_vale_el_primer_titulo(tray):
    titulo = grabando(tray, STEM, 'Kickoff Orion')
    tray._note_teams_title()
    tray._detector.current_meeting_name = 'Otra ventana de Teams'
    tray._note_teams_title()
    assert titulo.read_text(encoding='utf-8') == 'Kickoff Orion'


@pytest.mark.parametrize('stem, nombre', [
    (STEM, None),                                       # sin llamada
    (STEM, 'Unión a la reunión'),                       # titulo pasajero
    ('2026-10-28_10-34_Kickoff_Orion', 'Otra'),         # el fichero ya lo lleva
])
def test_no_apunta_lo_que_no_es_un_titulo_nuevo(tray, stem, nombre):
    titulo = grabando(tray, stem, nombre)
    tray._note_teams_title()
    assert not titulo.exists()


def test_sin_grabacion_no_apunta_nada(tray):
    tray._recording_path = None
    tray._detector = Detector('Kickoff Orion')
    tray._note_teams_title()
    assert not list(ta.RECORDINGS_DIR.glob('*.title'))


def test_el_ticker_lo_apunta_y_un_error_no_lo_para(tray, monkeypatch):
    llamadas = []
    tray._recording_start = time.time()
    tray._disk_next_check = float('inf')
    monkeypatch.setattr(ta.TrayApp, '_write_status', lambda self: llamadas.append('estado'))
    monkeypatch.setattr(ta.TrayApp, '_check_pending_notification', lambda self: None)

    def apunta(self):
        llamadas.append('titulo')
        self._ticker_stop.set()
        raise OSError('disco lleno')

    monkeypatch.setattr(ta.TrayApp, '_note_teams_title', apunta)
    tray._ticker()
    assert llamadas == ['titulo', 'estado']


def test_el_titulo_de_respaldo_va_por_orden(tray, monkeypatch):
    import outlook_sender
    manual = ta.RECORDINGS_DIR / f'{STEM}.wav'
    teams = ta.RECORDINGS_DIR / '2026-10-28_11-02_Kickoff_Orion.wav'
    preguntas = []
    monkeypatch.setattr(outlook_sender, 'find_meeting_subject',
                        lambda t, *a: preguntas.append(t) or 'Asunto de Outlook')

    # 1. El nombre de Teams de cualquier grabacion de la reunion (reconexiones).
    assert tray._fallback_title([manual, teams], [], ES) == 'Kickoff Orion'
    # 2. El .title de una grabacion manual, tambien con el WAV ya en processed/.
    titulo = ta.RECORDINGS_DIR / 'processed' / f'{STEM}.title'
    titulo.parent.mkdir(exist_ok=True)
    titulo.write_text('IFC Roadmapping', encoding='utf-8')
    assert tray._fallback_title([manual], [], ES) == 'IFC Roadmapping'
    titulo.unlink()
    # 3. El asunto de la cita: el que ya trajeron los participantes, sin preguntar...
    con_cita = [{'name': 'Ana', 'meeting_subject': 'Revision trimestral'}]
    assert tray._fallback_title([manual], con_cita, ES) == 'Revision trimestral'
    assert preguntas == []
    #    ...y si no, a Outlook, con la hora de la grabacion.
    assert tray._fallback_title([manual], [], ES) == 'Asunto de Outlook'
    assert preguntas == [datetime(2026, 10, 28, 10, 33)]


@pytest.mark.parametrize('lang, esperado', [('es', 'Reunión 10:33'), ('en', 'Meeting 10:33')])
def test_sin_nada_mas_queda_la_hora(tray, lang, esperado):
    """conftest: no hay Outlook."""
    assert tray._fallback_title([ta.RECORDINGS_DIR / f'{STEM}.wav'], [], ta._STR[lang]) == esperado
    assert tray._fallback_title([], [], ta._STR[lang]).startswith(esperado[:-5])


# ── 2. El proyecto sin Claude ────────────────────────────────────────────────

@pytest.mark.parametrize('titulo, proyecto', [
    ('GTM Sales Training ConsultantsAnalysts Session Prep', 'sales-training'),
    ('20261028_1033_GTM_Sales_Training_Session_Prep', 'sales-training'),   # un nombre de fichero
    ('Revision Slides Deck Brain', None),        # dos proyectos con "brain": empate, ninguno
    ('Project kickoff', None),                   # "project" no distingue un proyecto
    ('IFC Roadmapping on steroids', None),
    ('', None),
])
def test_el_proyecto_sale_del_titulo(titulo, proyecto):
    p = pc.project_from_title(titulo, PROJECTS)
    assert (p['id'] if p else None) == proyecto


def test_sin_lista_lee_los_proyectos(tr_dirs):
    (tr_dirs / 'projects.json').write_text(json.dumps({'projects': PROJECTS}), encoding='utf-8')
    assert pc.project_from_title('Atlas weekly')['id'] == 'atlas-project'


def minutas_sin_proyecto(tr_dirs, nombre='20261028_1033_GTM_Sales_Training.md'):
    md = tr_dirs / 'minutes' / nombre
    md.write_text('<!-- noted:minutes=pending stem=x -->\n\nNada.\n', encoding='utf-8')
    ae._save_json(md, [], {})
    (tr_dirs / 'projects.json').write_text(json.dumps({'projects': PROJECTS}), encoding='utf-8')
    return md


def proyecto_de(md):
    return json.loads(ae.get_actions_path(md).read_text(encoding='utf-8')).get('project_id', 'SIN')


def test_sin_sesion_de_claude_el_proyecto_sale_del_titulo(tr_dirs, monkeypatch, caplog):
    md = minutas_sin_proyecto(tr_dirs)
    monkeypatch.setattr(ae, '_get_claude_bin', lambda: 'claude')
    monkeypatch.setattr(ae.subprocess, 'run', lambda *a, **k: type('R', (), {
        'returncode': 1, 'stdout': 'Failed to authenticate: OAuth session expired', 'stderr': ''})())
    ae._detect_and_save_project(md, tr_dirs)
    assert proyecto_de(md) == 'sales-training'
    assert 'OAuth session expired' in caplog.text, "el motivo real, que va en stdout"


def test_sin_claude_instalado_tambien(tr_dirs, monkeypatch):
    md = minutas_sin_proyecto(tr_dirs)
    monkeypatch.setattr(ae, '_get_claude_bin', lambda: None)
    ae._detect_and_save_project(md, tr_dirs)
    assert proyecto_de(md) == 'sales-training'


def test_si_claude_revienta_tambien(tr_dirs, monkeypatch):
    md = minutas_sin_proyecto(tr_dirs)
    monkeypatch.setattr(ae, '_get_claude_bin', lambda: 'claude')

    def revienta(*_a, **_k):
        raise OSError('timeout')

    monkeypatch.setattr(ae.subprocess, 'run', revienta)
    ae._detect_and_save_project(md, tr_dirs)
    assert proyecto_de(md) == 'sales-training'


def test_si_el_titulo_no_nombra_ninguno_queda_sin_proyecto(tr_dirs, monkeypatch):
    """Sin project_id, y no con 'none': asi detect_projects_for_all_meetings lo
    vuelve a intentar cuando Claude responda."""
    md = minutas_sin_proyecto(tr_dirs, '20261028_1433_IFC_Roadmapping.md')
    monkeypatch.setattr(ae, '_get_claude_bin', lambda: None)
    ae._detect_and_save_project(md, tr_dirs)
    assert proyecto_de(md) == 'SIN'


def test_un_fallo_buscando_el_proyecto_no_rompe_nada(tr_dirs, monkeypatch, caplog):
    md = minutas_sin_proyecto(tr_dirs)
    monkeypatch.setattr(ae, '_get_claude_bin', lambda: None)

    def revienta(*_a):
        raise ValueError('boom')

    monkeypatch.setattr(pc, 'project_from_title', revienta)
    ae._detect_and_save_project(md, tr_dirs)
    assert proyecto_de(md) == 'SIN' and 'boom' in caplog.text


# ── El motivo del fallo ──────────────────────────────────────────────────────

@pytest.fixture
def claude_problem_real(monkeypatch):
    monkeypatch.setattr(mg, 'claude_problem', CLAUDE_PROBLEM)


def test_sin_cli_el_motivo_es_que_falta(claude_problem_real, monkeypatch):
    monkeypatch.setattr(mg, '_get_claude_bin', lambda: None)
    assert mg.claude_problem() == 'missing'


@pytest.mark.parametrize('salida, motivo', [
    ('{"loggedIn": false, "authMethod": "none"}', 'no_session'),
    ('{"loggedIn": true}', None),
    ('error: unknown command auth', None),          # un CLI antiguo: no se sabe, se intenta
])
def test_el_motivo_lo_dice_claude_auth_status(claude_problem_real, monkeypatch, salida, motivo):
    pedidos = []
    monkeypatch.setattr(mg, '_get_claude_bin', lambda: 'claude')
    monkeypatch.setattr(mg.subprocess, 'run', lambda cmd, **k: pedidos.append(cmd) or type('R', (), {
        'stdout': salida, 'returncode': 1})())
    assert mg.claude_problem() == motivo
    assert pedidos == [['claude', 'auth', 'status']]


class ClaudeSinSesion:
    returncode = 1

    def __init__(self, *_a, **_k):
        pass

    def communicate(self, input=None, timeout=None):
        return 'Failed to authenticate: OAuth session expired\n', ''


@pytest.mark.parametrize('llamada', ['minutas', 'suelta'])
def test_el_log_trae_el_error_que_el_cli_escribe_en_stdout(monkeypatch, caplog, llamada):
    monkeypatch.setattr(mg, '_get_claude_bin', lambda: 'claude')
    monkeypatch.setattr(mg.subprocess, 'Popen', ClaudeSinSesion)
    if llamada == 'minutas':
        assert mg._generate_via_cli(TRANSCRIPT, ta.RECORDINGS_DIR / f'{STEM}.wav') is None
    else:
        assert mg.run_claude('hola') is None
    assert 'OAuth session expired' in caplog.text


def test_el_error_del_cli_prefiere_stderr():
    assert mg._cli_error('salida', 'el error') == 'el error'
    assert mg._cli_error(None, None) == ''


def test_las_minutas_pendientes_llevan_su_grabacion_y_el_motivo():
    raw = mg.brief_minutes('Kickoff Orion', 'pending', language='en', reason='no_session', stem=STEM)
    title, content = mg.extract_title_from_minutes(raw)
    assert title == 'Kickoff Orion' and mg.brief_mode_of(content) == 'pending'
    assert mg.pending_stem_of(content) == STEM and 'claude auth login' in content
    # Sin stem tambien son 'pending', pero no se pueden reintentar: no se sabe de que grabacion.
    assert mg.pending_stem_of(mg.brief_minutes('X', 'pending')) is None
    assert mg.pending_stem_of(mg.brief_minutes('X', 'off')) is None


# ── 3. El cierre: minutas pendientes y su reintento ──────────────────────────

@pytest.fixture
def cierre(monkeypatch, tr_dirs):
    """_finalize_session sin Claude, sin Outlook y sin archivar."""
    reg = {'avisos': [], 'completas': None, 'guardadas': []}

    def save(minutes, output_path):
        reg['guardadas'].append(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(minutes, encoding='utf-8')
        return True

    import html_exporter
    monkeypatch.setattr(mg, 'generate_minutes', lambda *a, **k: reg['completas'])
    monkeypatch.setattr(mg, 'save_minutes', save)
    monkeypatch.setattr(html_exporter, 'export_to_html', lambda *a, **k: None)
    monkeypatch.setattr(pc, 'prepare_context', lambda *a, **k: (None, None))
    monkeypatch.setattr(ae, 'enrich_and_save', lambda *a, **k: None)
    monkeypatch.setattr(ae, 'save_without_actions', lambda *a, **k: None)
    monkeypatch.setattr(ta.TrayApp, '_notify', lambda self, t, m: reg['avisos'].append(m))
    monkeypatch.setattr(ta.TrayApp, 'set_processing', lambda self, m='': None)
    monkeypatch.setattr(ta.TrayApp, '_write_status', lambda self: None)
    return reg


def cerrar(tray, wav_factory, stem=STEM):
    wav = wav_factory(ta.RECORDINGS_DIR / f'{stem}.wav', seconds=0.1)
    tray._finalize_session({'base_wav': wav, 'parts': [wav], 'transcripts': {wav.stem: TRANSCRIPT},
                            'lang': 'es', 'key': '', 'awaiting': False, 'finalized': False,
                            'timer': None, 'last_at': time.time()})


def test_si_claude_falla_la_reunion_lleva_el_titulo_de_teams(tray, cierre, wav_factory, monkeypatch):
    """EL CASO del 28/10/2026: "Grabar ahora" durante una llamada de Teams."""
    monkeypatch.setattr(mg, 'claude_problem', lambda: 'no_session')
    (ta.RECORDINGS_DIR / f'{STEM}.title').write_text('IFC Roadmapping on steroids', encoding='utf-8')
    cerrar(tray, wav_factory)
    [md] = cierre['guardadas']
    assert md.name == '20261028_1033_IFC_Roadmapping_on_steroids.md'
    texto = md.read_text(encoding='utf-8')
    assert mg.brief_mode_of(texto) == 'pending' and mg.pending_stem_of(texto) == STEM
    assert 'claude auth login' in texto
    assert cierre['avisos'] == [ES['minutes_pending_session']]
    assert not (ta.RECORDINGS_DIR / f'{STEM}.title').exists(), "se va con el WAV a processed/"


@pytest.mark.parametrize('motivo, clave', [('missing', 'minutes_pending_missing'), (None, 'minutes_pending')])
def test_el_aviso_dice_el_motivo(tray, cierre, wav_factory, monkeypatch, motivo, clave):
    monkeypatch.setattr(mg, 'claude_problem', lambda: motivo)
    cerrar(tray, wav_factory)
    assert cierre['avisos'] == [ES[clave]]
    assert cierre['guardadas'][0].name == '20261028_1033_Reunión_1033.md'


def test_el_reintento_que_funciona_escribe_en_las_mismas_minutas(tray, cierre, wav_factory):
    """Las minimas ya se archivaron y exportaron con su nombre: unas segundas
    minutas para la misma reunion dejarian dos en el panel y en el proyecto."""
    cerrar(tray, wav_factory)
    [pendiente] = cierre['guardadas']
    cierre['completas'] = 'TITULO: Revision Del Roadmap\n\n## Resumen\n\nSe reviso el roadmap.'
    cierre['avisos'].clear()

    cerrar(tray, wav_factory)

    assert cierre['guardadas'] == [pendiente, pendiente]
    texto = pendiente.read_text(encoding='utf-8')
    assert texto.startswith('TITULO: Revision Del Roadmap\n'), "el panel muestra el titulo de Claude"
    assert mg.brief_mode_of(texto) is None and 'Se reviso el roadmap.' in texto
    assert [p.name for p in ta.MINUTES_DIR.glob('*.md')] == [pendiente.name]
    assert cierre['avisos'] == [ES['action_items']], "el camino de siempre, sin aviso de fallo"


def test_un_reintento_que_vuelve_a_fallar_no_avisa_otra_vez(tray, cierre, wav_factory):
    cerrar(tray, wav_factory)
    cerrar(tray, wav_factory)
    assert len(cierre['avisos']) == 1
    assert cierre['guardadas'][0] == cierre['guardadas'][1]
    assert mg.brief_mode_of(cierre['guardadas'][0].read_text(encoding='utf-8')) == 'pending'


def test_otra_grabacion_del_mismo_minuto_no_pisa_las_pendientes(tray, cierre, wav_factory):
    """Dos grabaciones en el mismo minuto comparten la hora del nombre: lo que
    identifica las minutas pendientes es el stem de su grabacion."""
    cerrar(tray, wav_factory)
    [pendiente] = cierre['guardadas']
    cierre['completas'] = 'TITULO: Otra Reunion\n\n## Resumen\n\nOtra cosa.'

    cerrar(tray, wav_factory, stem=f'{STEM}__2')

    assert cierre['guardadas'][1] != pendiente
    assert mg.pending_stem_of(pendiente.read_text(encoding='utf-8')) == STEM


# ── El reintento automatico ──────────────────────────────────────────────────

def pendiente(stem=STEM, nombre='20261028_1033_IFC_Roadmapping.md', transcript=TRANSCRIPT):
    md = ta.MINUTES_DIR / nombre
    _t, content = mg.extract_title_from_minutes(mg.brief_minutes('IFC Roadmapping', 'pending', stem=stem))
    md.write_text(content, encoding='utf-8')
    if transcript is not None:
        md.with_name(md.stem + '_transcript.txt').write_text(transcript, encoding='utf-8')
    return md


class Grabadora:
    is_recording = False
    is_saving = False


@pytest.fixture
def ocioso(tray, monkeypatch):
    tray._recorder = Grabadora()
    monkeypatch.setattr(ta.TrayApp, '_write_status', lambda self: None)
    return tray


def test_cuando_claude_vuelve_la_reunion_entra_en_el_pipeline(ocioso):
    pendiente()
    ocioso._retry_pending_minutes()
    assert [w.name for w in ocioso._pipeline_queue.queue] == [f'{STEM}.wav']
    assert ocioso._pipeline_queued == [STEM]
    # Donde el pipeline busca un transcript hecho: no vuelve a pasar por Whisper.
    assert (ta.RECORDINGS_DIR / f'{STEM}_transcript.txt').read_text(encoding='utf-8') == TRANSCRIPT


def test_sin_sesion_ni_se_intenta(ocioso, monkeypatch):
    pendiente()
    monkeypatch.setattr(mg, 'claude_problem', lambda: 'no_session')
    ocioso._retry_pending_minutes()
    assert ocioso._pipeline_queue.empty() and ocioso._pending_retries == {}


def test_como_mucho_tres_intentos_por_arranque(ocioso):
    pendiente()
    for _ in range(5):
        ocioso._retry_pending_minutes()
        ocioso._pipeline_queue = queue.Queue()          # el pipeline lo saca
        ocioso._pipeline_queued = []
    assert ocioso._pending_retries[STEM] == ta.RETRY_PENDING_MAX


@pytest.mark.parametrize('ocupado', ['grabando', 'guardando', 'cola', 'sesion', 'procesando', 'minutas_breves'])
def test_solo_con_noted_ocioso_y_las_minutas_completas(ocioso, tr_dirs, ocupado):
    pendiente()
    if ocupado == 'grabando':
        ocioso._recorder.is_recording = True
    elif ocupado == 'guardando':
        ocioso._recorder.is_saving = True
    elif ocupado == 'cola':
        ocioso._pipeline_queue.put(ta.RECORDINGS_DIR / 'otra.wav')
    elif ocupado == 'sesion':
        ocioso._session = {'finalized': False}
    elif ocupado == 'procesando':
        ocioso._current_job = {'stem': 'otra'}
    else:
        ajustes(tr_dirs, minutes_mode='summary')
    ocioso._retry_pending_minutes()
    assert ocioso._pending_retries == {}


def test_sin_transcript_no_se_puede_reintentar_y_no_se_insiste(ocioso, caplog):
    pendiente(transcript=None)
    ocioso._retry_pending_minutes()
    assert ocioso._pipeline_queue.empty()
    assert ocioso._pending_retries[STEM] == ta.RETRY_PENDING_MAX and 'no se pueden reintentar' in caplog.text


def test_las_completas_y_las_breves_no_se_reintentan(ocioso):
    (ta.MINUTES_DIR / '20261028_1033_Hecha.md').write_text('## Resumen\n\nHecho.\n', encoding='utf-8')
    _t, breve = mg.extract_title_from_minutes(mg.brief_minutes('Breve', 'off'))
    (ta.MINUTES_DIR / '20261028_1034_Breve.md').write_text(breve, encoding='utf-8')
    ocioso._retry_pending_minutes()
    assert ocioso._pipeline_queue.empty()


def test_de_punta_a_punta_sin_el_wav(ocioso, cierre, monkeypatch):
    """El WAV ya esta en processed/ (o lo borro la retencion): para rehacer las
    minutas basta el transcript."""
    md = pendiente()
    cierre['completas'] = 'TITULO: Roadmap Q1\n\n## Resumen\n\nSe reviso.'
    monkeypatch.setattr(ta.TrayApp, '_schedule_finalize', lambda self, sess: self._finalize_session(sess))

    ocioso._retry_pending_minutes()
    ocioso._run_pipeline_sync(ocioso._pipeline_queue.get())

    assert cierre['guardadas'] == [md]
    assert md.read_text(encoding='utf-8').startswith('TITULO: Roadmap Q1')
    assert not (ta.RECORDINGS_DIR / f'{STEM}_transcript.txt').exists()


def test_el_bucle_sigue_aunque_un_intento_falle(tray, monkeypatch):
    intentos, esperas = [], []

    class Basta(Exception):
        pass

    def espera(s):
        esperas.append(s)
        if len(esperas) == 3:
            raise Basta

    def falla(self):
        intentos.append(1)
        raise RuntimeError('boom')

    monkeypatch.setattr(ta.TrayApp, '_retry_pending_minutes', falla)
    monkeypatch.setattr(ta.time, 'sleep', espera)
    with pytest.raises(Basta):
        tray._retry_pending_loop()
    assert esperas == [ta.RETRY_PENDING_FIRST_S, ta.RETRY_PENDING_EVERY_S, ta.RETRY_PENDING_EVERY_S]
    assert len(intentos) == 2


# ── 4. El aviso de Teams aceptado con una grabacion en marcha ────────────────

def test_start_con_una_grabacion_en_marcha_devuelve_false():
    """main.py lo usa para no tratar la ruta del aviso como la grabacion en
    curso: el 28/10/2026 eso lanzo un segundo transcript en vivo de un audio que
    no existia."""
    rec = AudioRecorder()
    rec._recording = True
    assert rec.start(Path('otra.wav')) is False


def test_start_sin_grabacion_devuelve_true(monkeypatch):
    rec = AudioRecorder()
    monkeypatch.setattr(rec, '_begin', lambda path: None)
    assert rec.start(Path('una.wav')) is True and rec.is_recording


def test_el_detector_solo_da_nombre_durante_una_llamada():
    from teams_detector import TeamsCallDetector
    d = TeamsCallDetector()
    d._current_meeting_name = 'Kickoff Orion'
    assert d.current_meeting_name is None
    d._in_call = True
    assert d.current_meeting_name == 'Kickoff Orion'
