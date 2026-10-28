"""Niveles de minutas y acciones apagables (decision de Felipe, 19/10/2026).

Felipe no sigue las acciones y a veces solo lee la primera frase de las
minutas para saber de que fue la reunion. Por eso, en Ajustes: minutas
completas / solo titulo y resumen / ninguna, y acciones si o no.

LO QUE ESTOS TESTS PROTEGEN
1. Con minutas breves o sin minutas la reunion SIGUE guardandose y viendose:
   .md con TITULO y marcador, transcript copiado, WAV a processed. Sin .md el
   panel no la mostraba y _recover_pending la reprocesaba en cada arranque.
2. Con las acciones apagadas el proyecto se sigue detectando: de el dependen
   el archivado, la exportacion y scan_meetings.py.
3. Los ajustes se leen en cada reunion: se cambian desde el panel, otro proceso.
"""
import json
import threading
import time

import pytest

import actions_enricher as ae
import app_window as aw
import config
import minutes_generator as mg
import tray_app as ta

pytestmark = pytest.mark.unit

STEM = '2026-10-19_15-53_Comite_Semanal'
TRANSCRIPT = '[00:05] [Felipe]: Empezamos con el taller del proyecto Atlas.\n[00:20] [Otros]: Vale.'


def ajustes(tr_dirs, **valores):
    (tr_dirs / 'settings.json').write_text(json.dumps(valores), encoding='utf-8')


@pytest.fixture
def cierre(monkeypatch, tr_dirs):
    """_finalize_session sin Claude, sin Outlook y sin mover ficheros de verdad."""
    reg = {'completas': 0, 'resumen': 0, 'contexto': 0, 'enriquecer': 0,
           'sin_acciones': [], 'procesado': 0, 'guardadas': None}

    def generate(transcript, wav_path, **_k):
        reg['completas'] += 1
        return 'TITULO: Comite Semanal\n\n## Resumen\n\ncompletas'

    def save(minutes, output_path):
        reg['guardadas'] = output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(minutes, encoding='utf-8')
        return True

    def summarize(transcript, **_k):
        reg['resumen'] += 1
        return reg.get('resumen_devuelve', 'TITULO: Taller Atlas\n\nSe preparo el taller del proyecto Atlas.')

    import html_exporter
    import meeting_qa
    import project_context
    monkeypatch.setattr(mg, 'generate_minutes', generate)
    monkeypatch.setattr(mg, 'save_minutes', save)
    monkeypatch.setattr(meeting_qa, 'summarize', summarize)
    monkeypatch.setattr(html_exporter, 'export_to_html', lambda *a, **k: None)
    monkeypatch.setattr(project_context, 'prepare_context',
                        lambda *a, **k: reg.__setitem__('contexto', reg['contexto'] + 1) or (None, None))
    monkeypatch.setattr(ae, 'enrich_and_save',
                        lambda *a, **k: reg.__setitem__('enriquecer', reg['enriquecer'] + 1))
    monkeypatch.setattr(ae, 'save_without_actions',
                        lambda md, d, extra_text=None, on_done=None: reg['sin_acciones'].append(extra_text))
    monkeypatch.setattr(ta.TrayApp, '_notify', lambda self, t, m: None)
    monkeypatch.setattr(ta.TrayApp, 'set_processing', lambda self, m='': None)
    monkeypatch.setattr(ta.TrayApp, '_write_status', lambda self: None)
    monkeypatch.setattr(ta.TrayApp, '_move_to_processed',
                        lambda self, w, t: reg.__setitem__('procesado', reg['procesado'] + 1))
    return reg


def cerrar(tray, wav_factory):
    wav = wav_factory(ta.RECORDINGS_DIR / f'{STEM}.wav', seconds=0.1)
    tray._finalize_session({'base_wav': wav, 'parts': [wav], 'transcripts': {wav.stem: TRANSCRIPT},
                            'lang': 'es', 'key': 'comite', 'awaiting': False, 'finalized': False,
                            'timer': None, 'last_at': time.time()})


# ── Los tres niveles ─────────────────────────────────────────────────────────

def test_sin_ajustes_todo_es_como_siempre(tray, cierre, tr_dirs, wav_factory):
    cerrar(tray, wav_factory)
    assert cierre['completas'] == 1 and cierre['enriquecer'] == 1 and cierre['contexto'] == 1
    assert cierre['resumen'] == 0 and cierre['sin_acciones'] == []
    assert mg.brief_mode_of(cierre['guardadas'].read_text(encoding='utf-8')) is None


def test_solo_resumen_no_genera_minutas_completas(tray, cierre, tr_dirs, wav_factory):
    ajustes(tr_dirs, minutes_mode='summary')
    cerrar(tray, wav_factory)
    md = cierre['guardadas']
    texto = md.read_text(encoding='utf-8')
    assert cierre['completas'] == 0 and cierre['resumen'] == 1
    assert cierre['contexto'] == 0, "la memoria del proyecto solo alimenta las completas"
    assert 'Taller_Atlas' in md.name, "el titulo del resumen da nombre a la reunion"
    assert 'Se preparo el taller' in texto and mg.brief_mode_of(texto) == 'summary'
    # Sin acciones (no hay de donde sacarlas), pero el proyecto con el transcript.
    assert cierre['enriquecer'] == 0 and cierre['sin_acciones'] == [TRANSCRIPT[:4000]]


def test_si_el_resumen_falla_quedan_las_minimas_y_la_reunion_se_guarda(tray, cierre, tr_dirs, wav_factory):
    """Antes, sin minutas la reunion se quedaba sin procesar y se reintentaba en
    cada arranque. Un resumen fallido no puede llevar a eso."""
    ajustes(tr_dirs, minutes_mode='summary')
    cierre['resumen_devuelve'] = None
    cerrar(tray, wav_factory)
    md = cierre['guardadas']
    assert md is not None and mg.brief_mode_of(md.read_text(encoding='utf-8')) == 'off'
    assert 'Comite_Semanal' in md.name and cierre['procesado'] == 1


def test_sin_minutas_no_llama_a_claude_para_minutas(tray, cierre, tr_dirs, wav_factory):
    ajustes(tr_dirs, minutes_mode='off')
    cerrar(tray, wav_factory)
    md = cierre['guardadas']
    assert cierre['completas'] == 0 and cierre['resumen'] == 0
    assert md.name.startswith('20261019_1553_Comite_Semanal')
    assert mg.brief_mode_of(md.read_text(encoding='utf-8')) == 'off'


@pytest.mark.parametrize('modo', ['full', 'summary', 'off'])
def test_en_todos_los_niveles_el_transcript_queda_junto_a_la_reunion(tray, cierre, tr_dirs, wav_factory, modo):
    """Es lo que abre la pestana Transcript y lo que lee _recover_pending para
    no reprocesar: el .md con la fecha de la grabacion y su _transcript.txt."""
    ajustes(tr_dirs, minutes_mode=modo)
    cerrar(tray, wav_factory)
    md = cierre['guardadas']
    assert md.name.startswith('20261019_1553_')
    assert md.with_name(md.stem + '_transcript.txt').read_text(encoding='utf-8') == TRANSCRIPT
    assert cierre['procesado'] == 1


def test_si_las_minutas_completas_fallan_la_reunion_se_guarda_igual(tray, cierre, tr_dirs, wav_factory,
                                                                  monkeypatch):
    """Revision del 20/10/2026 (ALTO, ya pasaba antes): si Claude no devolvia
    minutas, el cierre hacia return: sin .md la reunion no salia en el panel,
    su transcript no se podia abrir y _recover_pending la reprocesaba en cada
    arranque. Ahora quedan unas minimas, con "Generar minutas completas"."""
    monkeypatch.setattr(mg, 'generate_minutes', lambda *a, **k: None)
    cerrar(tray, wav_factory)
    md = cierre['guardadas']
    assert md is not None and md.name.startswith('20261019_1553_')
    assert mg.brief_mode_of(md.read_text(encoding='utf-8')) == 'off'
    assert md.with_name(md.stem + '_transcript.txt').read_text(encoding='utf-8') == TRANSCRIPT
    assert cierre['procesado'] == 1
    assert cierre['enriquecer'] == 0 and cierre['sin_acciones'] == [TRANSCRIPT[:4000]]


def test_importar_un_transcript_sin_minutas_lo_guarda_igual(tr_dirs, monkeypatch):
    """Revision del 20/10/2026: al importar, si Claude no devolvia minutas el
    transcript se perdia ("Claude returned empty"). Ahora queda con minimas."""
    import html_exporter
    src = tr_dirs / 'externo.txt'
    src.write_text(TRANSCRIPT, encoding='utf-8')
    monkeypatch.setattr(mg, 'generate_minutes', lambda *a, **k: None)
    monkeypatch.setattr(html_exporter, 'export_to_html', lambda *a, **k: None)
    archivados = []
    monkeypatch.setattr(ae, 'save_without_actions', lambda md, d, extra_text=None, on_done=None:
                        archivados.append(extra_text))
    api = object.__new__(aw.AppAPI)
    api.pick_file = lambda *_a: str(src)
    run_id = api.import_transcript()['run_id']
    t0 = time.monotonic()
    while not api.get_import_status(run_id)['done'] and time.monotonic() - t0 < 5:
        time.sleep(0.01)
    st = api.get_import_status(run_id)
    md = __import__('pathlib').Path(st['path'])
    assert st['error'] == '' and mg.brief_mode_of(md.read_text(encoding='utf-8')) == 'off'
    assert md.with_name(md.stem + '_transcript.txt').read_text(encoding='utf-8') == TRANSCRIPT
    assert archivados == [TRANSCRIPT[:4000]]


def test_acciones_apagadas_con_minutas_completas(tray, cierre, tr_dirs, wav_factory):
    ajustes(tr_dirs, actions_enabled=False)
    cerrar(tray, wav_factory)
    assert cierre['completas'] == 1 and cierre['enriquecer'] == 0
    assert cierre['sin_acciones'] == [None], "las completas bastan para reconocer el proyecto"


@pytest.mark.parametrize('modo, sugiere', [('full', True), ('summary', False), ('off', False)])
def test_las_sugerencias_del_glosario_solo_con_minutas_completas(tray, cierre, tr_dirs, wav_factory,
                                                                 monkeypatch, modo, sugiere):
    """Pedir sugerencias al glosario manda el transcript a Claude: con las minutas
    breves o apagadas, quien usa Noted eligio no mandarle la reunion entera."""
    import app_window
    ajustes(tr_dirs, minutes_mode=modo)
    pedidas = []
    monkeypatch.setattr(ta.TrayApp, '_suggest_glossary_terms', lambda self, *a: pedidas.append(a))
    monkeypatch.setattr(ae, 'enrich_and_save', lambda md, d, on_done=None, **_k: on_done and on_done())
    monkeypatch.setattr(ae, 'save_without_actions',
                        lambda md, d, extra_text=None, on_done=None: on_done and on_done())
    monkeypatch.setattr(app_window, 'open_app', lambda *a, **k: None)

    cerrar(tray, wav_factory)

    t0 = time.monotonic()
    while not pedidas and time.monotonic() - t0 < (2 if sugiere else 0.3):
        time.sleep(0.02)
    assert bool(pedidas) is sugiere


def test_un_ajuste_raro_vale_como_completas(tr_dirs):
    ajustes(tr_dirs, minutes_mode='turbo', actions_enabled='no')
    assert config.minutes_mode() == 'full'
    assert config.actions_enabled() is True, "solo un False explicito las apaga"


def test_los_ajustes_se_releen_en_cada_llamada(tr_dirs):
    """El panel los cambia en otro proceso: leidos al importar no valdrian."""
    ajustes(tr_dirs, minutes_mode='off')
    assert config.minutes_mode() == 'off'
    ajustes(tr_dirs, minutes_mode='summary')
    assert config.minutes_mode() == 'summary'


def test_un_settings_con_bom_se_lee(tr_dirs):
    (tr_dirs / 'settings.json').write_bytes(b'\xef\xbb\xbf{"minutes_mode": "off"}')
    assert config.minutes_mode() == 'off'


# ── Las minutas breves ───────────────────────────────────────────────────────

def test_las_minutas_breves_llevan_titulo_y_marcador_invisible():
    raw = mg.brief_minutes('Comite', 'summary', 'TITULO: Taller Atlas\n\nSe preparo el taller.')
    title, content = mg.extract_title_from_minutes(raw)
    assert title == 'Taller Atlas'
    assert mg.brief_mode_of(content) == 'summary'
    assert 'Se preparo el taller.' in content
    assert '<!--' in content, "el marcador es un comentario HTML: no se ve al renderizar"


def test_un_marcador_en_mitad_del_texto_no_cuenta():
    """Estructural, no una palabra: unas minutas completas que CITEN el marcador
    (p. ej. hablando de esta funcion) no son breves."""
    texto = '## Resumen\n\nSe hablo de "<!-- noted:minutes=off -->" en la reunion.\n'
    assert mg.brief_mode_of(texto) is None


def test_el_panel_ve_una_reunion_sin_minutas_y_sabe_que_es_breve(tr_dirs):
    md = tr_dirs / 'minutes' / '20261019_1553_Comite_Semanal.md'
    _title, content = mg.extract_title_from_minutes(mg.brief_minutes('Comite Semanal', 'off'))
    md.write_text(content, encoding='utf-8')
    api = object.__new__(aw.AppAPI)
    api._load_pins = lambda: set()
    entry = next(m for m in api.get_meetings() if m['path'] == str(md))
    assert entry['brief'] == 'off'


# ── Acciones: el proyecto se detecta siempre, y on_done una sola vez ─────────

def test_sin_acciones_se_detecta_el_proyecto_y_on_done_va_una_vez(tr_dirs, monkeypatch):
    """Revision del 19/10: con unas minutas sin acciones, on_done se llamaba DOS
    veces (archivaba, exportaba y abria el panel dos veces) y el proyecto no se
    detectaba, asi que la reunion no se archivaba."""
    md = tr_dirs / 'minutes' / '20261019_1553_Comite.md'
    md.write_text('## Resumen\n\nNada que hacer.\n', encoding='utf-8')
    detectados, hechos = [], []
    monkeypatch.setattr(ae, '_detect_and_save_project', lambda m, d, extra=None: detectados.append(m))
    ae._run_enrichment(md, tr_dirs, on_done=lambda: hechos.append(1))
    assert detectados == [md] and hechos == [1]


def test_sin_acciones_deja_el_json_vacio_y_detecta_con_el_transcript(tr_dirs, monkeypatch):
    md = tr_dirs / 'minutes' / '20261019_1553_Comite.md'
    md.write_text('TITULO: Comite\n', encoding='utf-8')
    vistos, hecho = [], threading.Event()
    monkeypatch.setattr(ae, '_detect_and_save_project', lambda m, d, extra=None: vistos.append(extra))
    ae.save_without_actions(md, tr_dirs, extra_text='cliente Atlas', on_done=hecho.set)
    assert hecho.wait(5)
    assert json.loads(ae.get_actions_path(md).read_text(encoding='utf-8'))['actions'] == []
    assert vistos == ['cliente Atlas']


def test_la_deteccion_del_proyecto_ve_el_transcript_si_se_le_da(tr_dirs, monkeypatch):
    md = tr_dirs / 'minutes' / '20261019_1553_Comite.md'
    md.write_text('TITULO: Comite\n', encoding='utf-8')
    ae._save_json(md, [], {})
    (tr_dirs / 'projects.json').write_text(json.dumps({'projects': [{'id': 'atlas', 'name': 'Atlas'}]}),
                                           encoding='utf-8')
    prompts, cmds = [], []

    def run(cmd, **k):
        prompts.append(k.get('input'))
        cmds.append(cmd)
        return type('R', (), {'returncode': 0, 'stdout': 'atlas', 'stderr': ''})()

    monkeypatch.setattr(ae, '_get_claude_bin', lambda: 'claude')
    monkeypatch.setattr(ae.subprocess, 'run', run)
    ae._detect_and_save_project(md, tr_dirs, extra_text='Hablamos del cliente Atlas')
    assert 'Hablamos del cliente Atlas' in prompts[0]
    assert json.loads(ae.get_actions_path(md).read_text(encoding='utf-8'))['project_id'] == 'atlas'
    # Revision del 20/10/2026: ahora lleva transcript; sin este flag el CLI
    # guardaba la sesion (con ese texto) en ~/.claude/projects.
    assert '--no-session-persistence' in cmds[0]


def test_un_fallo_en_on_done_queda_en_el_log(tr_dirs, monkeypatch, caplog):
    """Revision del 20/10/2026: si on_done fallaba, archivado y exportacion se
    saltaban sin dejar rastro."""
    md = tr_dirs / 'minutes' / '20261019_1553_Comite.md'
    md.write_text('TITULO: Comite\n', encoding='utf-8')
    monkeypatch.setattr(ae, '_detect_and_save_project', lambda *a, **k: None)
    hecho = threading.Event()

    def on_done():
        hecho.set()
        raise RuntimeError('boom en on_done')

    ae.save_without_actions(md, tr_dirs, on_done=on_done)
    assert hecho.wait(5)
    t0 = time.monotonic()
    while 'boom en on_done' not in caplog.text and time.monotonic() - t0 < 2:
        time.sleep(0.01)
    assert 'boom en on_done' in caplog.text
