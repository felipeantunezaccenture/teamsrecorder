"""Preguntas sobre una reunion (meeting_qa), sin llamar a ningun modelo.

La calidad de las respuestas la mide la evaluacion (.claude/evals/meeting_qa).
Aqui se protege la fontaneria, que es lo que puede fallar en silencio:
1. El prompt: transcript arriba, pregunta al final, y en una sesion solo las
   lineas nuevas (reenviarlo entero en cada pregunta multiplicaria el coste).
2. El CLI: sin herramientas ni MCP, en stream, y nunca colgado: el tiempo
   agotado mata el proceso y lo dice.
3. Si la sesion muere, la pregunta se responde igual (camino de una vez).
4. Las preguntas no se guardan en disco: solo viven en memoria.
"""
import io
import json
import os
import queue
import threading
import time

import pytest

import app_window as aw
import config
import meeting_qa as qa

pytestmark = pytest.mark.unit

LINES = ['[00:05] [Felipe]: Empezamos.', '[00:51] [Otros]: El taller es el jueves 23.',
         '[01:12] [Otros]: En la sala Nube.']


def turn(text, cost=0.01, error=False):
    half = len(text) // 2
    return [{'type': 'system', 'subtype': 'init'},
            {'type': 'stream_event', 'event': {'delta': {'type': 'text_delta', 'text': text[:half]}}},
            {'type': 'stream_event', 'event': {'delta': {'type': 'text_delta', 'text': text[half:]}}},
            {'type': 'result', 'subtype': 'error' if error else 'success', 'is_error': error,
             'result': text, 'total_cost_usd': cost, 'usage': {'input_tokens': 7, 'cache_read_input_tokens': 3}}]


class Stdin:
    def __init__(self, on_line=None):
        self.data, self.closed, self.on_line = '', False, on_line

    def write(self, s):
        if self.closed:
            raise ValueError('I/O operation on closed file.')    # como un fichero de verdad
        self.data += s
        if self.on_line:
            for line in s.splitlines():
                self.on_line(line)

    def flush(self):
        pass

    def close(self):
        self.closed = True
        if self.on_line:
            self.on_line(None)


class OneShot:
    """El proceso de una sola vez: lee el prompt y saca un turno."""
    def __init__(self, events, rc=0, stderr='', hang=False):
        self.stdin, self.returncode, self._rc = Stdin(), None, rc
        self.killed = threading.Event()
        self.stderr = io.StringIO(stderr)
        self.stdout = self._out(events, hang)

    def _out(self, events, hang):
        if hang:
            self.killed.wait(5)
            return
        for e in events:
            yield json.dumps(e) + '\n'

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self._rc
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9
        self.killed.set()


class SessionProc:
    """El proceso abierto: un turno por cada mensaje que le llega."""
    def __init__(self, answers):
        self.answers, self.messages, self.returncode = list(answers), [], None
        self._q = queue.Queue()
        self.stdin = Stdin(on_line=self._line)
        self.stdout = self._out()

    def _line(self, line):
        if line is None:
            self.returncode = 0
            self._q.put(None)
            return
        self.messages.append(json.loads(line)['message']['content'])
        events = self.answers.pop(0) if self.answers else None
        if events is None:                       # se "cae" sin responder
            self.returncode = 1
            self._q.put(None)
            return
        for e in events:
            self._q.put(json.dumps(e) + '\n')

    def _out(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            yield item

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9
        self._q.put(None)


@pytest.fixture
def cli(monkeypatch):
    """Popen sustituido; guarda el comando y el proceso de cada llamada."""
    st = {'next': [], 'calls': []}

    def popen(cmd, **kwargs):
        proc = st['next'].pop(0)
        st['calls'].append({'cmd': cmd, 'proc': proc, 'kwargs': kwargs})
        return proc

    monkeypatch.setattr(qa.subprocess, 'Popen', popen)
    monkeypatch.setattr(config, 'get_claude_bin', lambda: 'claude')
    return st


# ── El prompt ────────────────────────────────────────────────────────────────

def test_transcript_arriba_y_pregunta_al_final():
    """La guia de Anthropic para documentos largos: hasta un 30% mejor."""
    p = qa.build_prompt(LINES, '¿Dónde es el taller?', me_label='Felipe')
    assert p.index('<transcript') < p.index(LINES[0]) < p.index('</transcript>')
    assert p.splitlines()[-1] == 'Pregunta: ¿Dónde es el taller?'
    assert 'hasta [01:12]' in p and '[Felipe] es quien graba' in p


def test_solo_las_tres_ultimas_preguntas_previas():
    hist = [{'q': f'p{i}', 'a': f'r{i}'} for i in range(5)]
    p = qa.build_prompt(LINES, 'otra', hist)
    assert 'P: p1' not in p and 'P: p2' in p and 'R: r4' in p


def test_una_reunion_enorme_se_queda_con_lo_mas_reciente_y_lo_dice(monkeypatch):
    monkeypatch.setattr(qa, 'MAX_TRANSCRIPT_CHARS', 70)
    p = qa.build_prompt(LINES, 'x')
    assert LINES[0] not in p and LINES[-1] in p
    assert 'se omite el principio' in p


def test_sin_pista_de_los_demas_no_inventa_etiquetas():
    p = qa.build_prompt(['[00:01] hola'], 'x', me_label='Felipe')
    assert 'sin etiquetas de quien habla' in p


def test_el_comando_va_sin_herramientas_ni_mcp_y_en_stream():
    cmd = qa.command('claude-sonnet-5-5')
    for flag in ('--safe-mode', '--strict-mcp-config', '--no-session-persistence', '--include-partial-messages'):
        assert flag in cmd
    assert cmd[cmd.index('--tools') + 1] == ''
    assert cmd[cmd.index('--effort') + 1] == 'low'
    assert cmd[cmd.index('--output-format') + 1] == 'stream-json'


def test_el_modelo_sale_de_ajustes_y_uno_raro_no_vale(tr_dirs):
    assert qa.qa_model() == qa.DEFAULT_MODEL
    (tr_dirs / 'settings.json').write_text('{"qa_model": "claude-opus-5-5"}', encoding='utf-8')
    assert qa.qa_model() == 'claude-opus-5-5'
    (tr_dirs / 'settings.json').write_text('{"qa_model": "gpt-9"}', encoding='utf-8')
    assert qa.qa_model() == qa.DEFAULT_MODEL


def test_haiku_no_se_ofrece_y_si_estaba_elegido_vuelve_a_sonnet(tr_dirs):
    """Regression: ISSUE-002 - "Haiku 5.5 (el mas rapido)" era lo contrario.
    Found by /gstack-qa on 2026-10-19. Medido: con esfuerzo bajo, Haiku 5.5
    piensa 8.500-12.500 tokens por pregunta; primer texto a los 41-49 s y
    $0,10-0,17, frente a 4-5,5 s y $0,015 de Sonnet 5.5."""
    assert 'claude-haiku-5-5' not in qa.MODELS
    (tr_dirs / 'settings.json').write_text('{"qa_model": "claude-haiku-5-5"}', encoding='utf-8')
    assert qa.qa_model() == qa.DEFAULT_MODEL == 'claude-sonnet-5-5'


# ── Una sola vez ─────────────────────────────────────────────────────────────

def test_la_respuesta_llega_por_trozos_y_entera_al_final(cli):
    cli['next'].append(OneShot(turn('En la sala Nube [01:12].', cost=0.02)))
    vistos = []
    r = qa.ask(LINES, '¿Dónde?', on_text=vistos.append)
    assert r['text'] == 'En la sala Nube [01:12].' and r['error'] is None
    assert len(vistos) == 2 and vistos[-1] == r['text'] and r['first_text_s'] is not None
    assert r['cost_usd'] == 0.02
    assert '¿Dónde?' in cli['calls'][0]['proc'].stdin.data and cli['calls'][0]['proc'].stdin.closed


def test_sin_cli_lo_dice_sin_lanzar_nada(cli, monkeypatch):
    monkeypatch.setattr(config, 'get_claude_bin', lambda: None)
    assert qa.ask(LINES, 'x')['error'] == 'no_cli'
    assert cli['calls'] == []


def test_un_error_del_cli_se_devuelve(cli):
    cli['next'].append(OneShot([], rc=1, stderr='Not logged in'))
    assert 'Not logged in' in qa.ask(LINES, 'x')['error']


def test_un_resultado_con_error_se_devuelve(cli):
    cli['next'].append(OneShot(turn('rate limited', error=True)))
    r = qa.ask(LINES, 'x')
    assert r['error'] == 'rate limited' and r['text'] == ''


def test_si_se_cuelga_se_mata_y_lo_dice(cli):
    proc = OneShot([], hang=True)
    cli['next'].append(proc)
    r = qa.ask(LINES, 'x', timeout=0.2)
    assert r['error'] == 'timeout' and proc.killed.is_set()


def test_el_resumen_devuelve_titulo_y_frase(cli):
    cli['next'].append(OneShot(turn('TITULO: Taller Atlas\nSe preparo el taller.')))
    assert qa.summarize('\n'.join(LINES)) == 'TITULO: Taller Atlas\n\nSe preparo el taller.'


def test_un_resumen_sin_titulo_no_vale(cli):
    cli['next'].append(OneShot(turn('La reunion fue sobre el taller.')))
    assert qa.summarize('\n'.join(LINES)) is None


# ── La sesion abierta ────────────────────────────────────────────────────────

def test_la_sesion_manda_el_transcript_una_vez_y_luego_solo_lo_nuevo(cli):
    """Reenviarlo entero en cada pregunta multiplicaria coste y tiempo."""
    proc = SessionProc([turn('uno', cost=0.01), turn('dos', cost=0.03), turn('tres', cost=0.04)])
    cli['next'].append(proc)
    s = qa.Session(model='claude-sonnet-5-5', me_label='Felipe')
    assert s.ask(LINES[:2], 'primera')['text'] == 'uno'
    r2 = s.ask(LINES, 'segunda')
    r3 = s.ask(LINES, 'tercera')
    first, second, third = proc.messages
    assert '<transcript' in first and LINES[0] in first
    assert '<transcript_nuevo' in second and LINES[2] in second and LINES[0] not in second
    assert third == 'Pregunta: tercera'
    assert r2['cost_usd'] == 0.02 and r3['cost_usd'] == 0.01, "el CLI da el coste acumulado"
    s.close()


def test_la_conversacion_previa_solo_va_en_la_primera(cli):
    """Si se cerro la vista y se abre otra sesion, la conversacion sigue."""
    proc = SessionProc([turn('a'), turn('b')])
    cli['next'].append(proc)
    s = qa.Session()
    s.ask(LINES, 'p2', history=[{'q': 'p1', 'a': 'r1'}])
    s.ask(LINES, 'p3', history=[{'q': 'p1', 'a': 'r1'}, {'q': 'p2', 'a': 'a'}])
    assert 'P: p1' in proc.messages[0] and 'P: p1' not in proc.messages[1]
    s.close()


def test_si_la_sesion_se_cae_lo_dice_para_ir_por_el_camino_de_una_vez(cli):
    proc = SessionProc([None])
    cli['next'].append(proc)
    s = qa.Session()
    assert s.ask(LINES, 'x')['error'] == 'session_dead'
    assert s.ask(LINES, 'y')['error'] == 'session_dead'


def test_una_respuesta_fallida_no_da_las_lineas_por_enviadas(cli):
    proc = SessionProc([turn('fallo', error=True), turn('ok')])
    cli['next'].append(proc)
    s = qa.Session()
    s.ask(LINES, 'x')
    s.ask(LINES, 'y')
    assert '<transcript ' in proc.messages[1], "la siguiente vuelve a llevar el transcript"
    s.close()


def test_si_la_sesion_no_responde_se_cierra(cli):
    class Muda(SessionProc):
        def _line(self, line):
            if line is not None:
                self.messages.append(line)
    proc = Muda([])
    cli['next'].append(proc)
    s = qa.Session()
    assert s.ask(LINES, 'x', timeout=0.2)['error'] == 'timeout'
    assert proc.stdin.closed, "a medias no se puede reutilizar"


def test_si_otro_hilo_cerro_la_sesion_no_lanza(cli):
    """Revision del 20/10/2026: con stdin cerrado por otro hilo, write lanza
    ValueError (no OSError) y rompia el "nunca lanza": no se probaba la red."""
    cli['next'].append(SessionProc([turn('a')]))
    s = qa.Session()
    s.proc.stdin.closed = True
    s.proc.returncode = None                      # el proceso aun no ha muerto
    assert s.ask(LINES, 'x')['error'] == 'session_dead'


def test_una_linea_que_no_es_un_evento_no_rompe_la_sesion(cli):
    cli['next'].append(SessionProc([[['no', 'es', 'un', 'evento'], *turn('ok')]]))
    s = qa.Session()
    assert s.ask(LINES, 'x')['text'] == 'ok'
    s.close()


def test_mientras_responde_la_sesion_no_cuenta_como_inactiva(cli):
    """Revision del 20/10/2026: last_used solo se movia al terminar. Una sesion
    precalentada hace mas de 10 min contaba como inactiva mientras respondia, y
    calentar otra conversacion la cerraba a media respuesta."""
    soltar = threading.Event()

    class Lenta(SessionProc):
        def _line(self, line):
            if line is None:
                return super()._line(line)
            self.messages.append(line)
            threading.Thread(target=lambda: (soltar.wait(5), [self._q.put(json.dumps(e) + '\n')
                                                               for e in turn('ok')]), daemon=True).start()

    cli['next'].append(Lenta([]))
    s = qa.Session()
    s.last_used -= 10_000
    hilo = threading.Thread(target=s.ask, args=(LINES, 'x'), daemon=True)
    hilo.start()
    time.sleep(0.1)
    assert s.busy() and not s.idle()
    soltar.set()
    hilo.join(5)
    s.close()


# ── La API del panel ─────────────────────────────────────────────────────────

ENSURE_SWEEPER = getattr(aw, '_ensure_ask_sweeper', None)


@pytest.fixture
def api(monkeypatch):
    aw._ask_runs.clear()
    aw._close_ask_sessions()
    # El barrido de fondo, solo en el test que lo prueba: en los demas no hace
    # falta y seguiria vivo entre tests.
    monkeypatch.setattr(aw, '_ensure_ask_sweeper', lambda: None, raising=False)
    a = object.__new__(aw.AppAPI)
    yield a
    aw._close_ask_sessions()


def esperar_respuesta(api, run_id, timeout=5):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        st = api.get_ask_status(run_id)
        if st['done']:
            return st
        time.sleep(0.01)
    raise AssertionError('no termino')


@pytest.mark.parametrize('source, ref, question, error', [
    ('live', 'reunion', '   ', 'empty'),
    ('otra', 'reunion', 'x', 'bad_source'),
    ('live', '../fuera', 'x', 'bad_ref'),
    ('meeting', 'C:/Windows/win.ini', 'x', 'bad_ref'),
])
def test_preguntas_que_no_se_aceptan(api, source, ref, question, error):
    assert api.ask_meeting(source, ref, question) == {'ok': False, 'error': error}


def test_sin_transcript_no_hay_pregunta(api, monkeypatch):
    monkeypatch.setattr(api, 'get_live_transcript', lambda stem: {'lines': []}, raising=False)
    assert api.ask_meeting('live', 'reunion', 'x')['error'] == 'no_transcript'


def test_en_vivo_responde_por_la_sesion(api, cli, monkeypatch):
    monkeypatch.setattr(api, 'get_live_transcript', lambda stem: {'lines': LINES}, raising=False)
    cli['next'].append(SessionProc([turn('En la sala Nube [01:12].')]))
    r = api.ask_meeting('live', 'reunion', '¿Dónde?')
    assert r['ok'] and r['upto'] == '01:12'
    st = esperar_respuesta(api, r['run_id'])
    assert st['text'] == 'En la sala Nube [01:12].' and st['error'] is None
    assert '--input-format' in cli['calls'][0]['cmd']


def test_si_la_sesion_muere_responde_igual_por_el_camino_de_una_vez(api, cli, monkeypatch):
    monkeypatch.setattr(api, 'get_live_transcript', lambda stem: {'lines': LINES}, raising=False)
    cli['next'] += [SessionProc([None]), OneShot(turn('En la sala Nube.'))]
    st = esperar_respuesta(api, api.ask_meeting('live', 'reunion', '¿Dónde?')['run_id'])
    assert st['text'] == 'En la sala Nube.' and st['error'] is None


def test_una_reunion_terminada_pregunta_sobre_su_transcript(api, cli, tr_dirs):
    md = tr_dirs / 'minutes' / '20261019_1553_Comite.md'
    md.write_text('## Resumen\n', encoding='utf-8')
    md.with_name(md.stem + '_transcript.txt').write_text('\n'.join(LINES), encoding='utf-8')
    proc = SessionProc([turn('El jueves 23 [00:51].')])
    cli['next'].append(proc)
    st = esperar_respuesta(api, api.ask_meeting('meeting', str(md), '¿Cuándo?')['run_id'])
    assert st['text'] == 'El jueves 23 [00:51].'
    assert LINES[1] in proc.messages[0]


def test_como_mucho_dos_sesiones_abiertas(api, cli):
    cli['next'] += [SessionProc([]), SessionProc([]), SessionProc([])]
    for k in ('meeting:a', 'meeting:b', 'meeting:c'):
        aw._ask_session(k)
    assert set(aw._ask_sessions) == {'meeting:b', 'meeting:c'}
    assert cli['calls'][0]['proc'].stdin.closed, "la mas antigua se cierra"


def test_nunca_se_desaloja_la_sesion_en_vivo_ni_una_ocupada(api, cli):
    """Revision del 20/10/2026: pasar el raton por la caja de una tercera reunion
    desalojaba la mas antigua, aunque fuera la en vivo o estuviera respondiendo."""
    cli['next'] += [SessionProc([]), SessionProc([]), SessionProc([]), SessionProc([])]
    vivo = aw._ask_session('live:a')
    ocupada = aw._ask_session('meeting:b')
    ocupada._lock.acquire()
    try:
        aw._ask_session('meeting:c')
        assert {'live:a', 'meeting:b', 'meeting:c'} <= set(aw._ask_sessions)
        assert vivo.alive() and ocupada.alive()
    finally:
        ocupada._lock.release()
    aw._ask_session('meeting:d')
    assert 'meeting:b' not in aw._ask_sessions or 'meeting:c' not in aw._ask_sessions
    assert 'live:a' in aw._ask_sessions


def test_cerrar_la_vista_con_una_pregunta_en_marcha_no_lanza_otra(api, cli, monkeypatch):
    """Revision del 20/10/2026: cerrar la vista mataba la sesion, la pregunta
    volvia como session_dead y la red lanzaba otro claude que nadie iba a ver."""
    class Muda(SessionProc):
        def _line(self, line):
            if line is None:
                return super()._line(line)
            self.messages.append(line)

    monkeypatch.setattr(api, 'get_live_transcript', lambda stem: {'lines': LINES}, raising=False)
    cli['next'].append(Muda([]))
    run_id = api.ask_meeting('live', 'reunion', 'x')['run_id']
    t0 = time.monotonic()
    while not (cli['calls'] and cli['calls'][0]['proc'].messages) and time.monotonic() - t0 < 5:
        time.sleep(0.01)
    api.ask_close('live', 'reunion')
    st = esperar_respuesta(api, run_id)
    assert st['error'] == 'closed'
    assert len(cli['calls']) == 1, "no se lanzo la red de una sola vez"


def test_una_ruta_de_red_se_rechaza_sin_ir_a_buscarla(api, monkeypatch):
    """Revision del 20/10/2026: resolve() sobre \\\\host\\share contactaba por SMB
    antes de rechazarla."""
    from pathlib import Path
    monkeypatch.setattr(Path, 'resolve', lambda self, *a, **k: (_ for _ in ()).throw(AssertionError('red')))
    assert api.ask_meeting('meeting', r'\\host\share\20261019_1000_x.md', 'x')['error'] == 'bad_ref'


def test_cambiar_de_modelo_en_ajustes_abre_una_sesion_nueva(api, cli, tr_dirs):
    cli['next'] += [SessionProc([]), SessionProc([])]
    first = aw._ask_session('live:a')
    (tr_dirs / 'settings.json').write_text('{"qa_model": "claude-opus-5-5"}', encoding='utf-8')
    second = aw._ask_session('live:a')
    assert second is not first and second.model == 'claude-opus-5-5'


def test_cerrar_la_vista_cierra_su_sesion(api, cli):
    proc = SessionProc([])
    cli['next'].append(proc)
    aw._ask_session('live:a')
    assert api.ask_close('live', 'a') is True
    assert proc.stdin.closed and 'live:a' not in aw._ask_sessions


# ── Las sesiones que nadie usa se cierran solas (27/10/2026) ────────────────
# Un panel abierto desde la tarde anterior tenia dos claude (~480 MB de reserva
# cada uno) con el disco a 0,86 GB: los 10 min sin uso solo se miraban al abrir
# otra conversacion.

def abierta(cli, key):
    cli['next'].append(SessionProc([]))
    return aw._ask_session(key)


def test_una_sesion_sin_usar_se_cierra_sola(api, cli):
    s = abierta(cli, 'meeting:a')
    s.last_used -= 10_000
    aw._sweep_ask_sessions()
    assert s.proc.stdin.closed and 'meeting:a' not in aw._ask_sessions


def test_el_barrido_no_cierra_una_reciente_ni_una_que_responde(api, cli):
    reciente = abierta(cli, 'meeting:a')
    ocupada = abierta(cli, 'live:b')
    ocupada.last_used -= 10_000
    ocupada._lock.acquire()
    try:
        aw._sweep_ask_sessions()
    finally:
        ocupada._lock.release()
    assert reciente.alive() and ocupada.alive()
    assert set(aw._ask_sessions) == {'meeting:a', 'live:b'}


def test_pedir_la_sesion_cuenta_como_uso(api, cli):
    """Si no, el barrido podia cerrarla entre pedirla y preguntar."""
    s = abierta(cli, 'meeting:a')
    s.last_used -= 10_000
    assert aw._ask_session('meeting:a') is s and not s.idle()


def test_el_barrido_corre_mientras_haya_sesiones(api, cli, monkeypatch):
    monkeypatch.setattr(aw, '_ASK_SWEEP_S', 0.01)
    monkeypatch.setattr(aw, '_ensure_ask_sweeper', ENSURE_SWEEPER)
    s = abierta(cli, 'meeting:a')
    hilo = aw._ask_sweeper
    time.sleep(0.2)                                  # unas cuantas vueltas
    assert hilo.is_alive() and s.alive(), "con una sesion reciente sigue, sin cerrarla"
    s.last_used -= 10_000
    hilo.join(5)
    assert not hilo.is_alive(), "sin sesiones, el barrido termina"
    assert s.proc.stdin.closed and aw._ask_sweeper is None
    abierta(cli, 'meeting:b')
    assert aw._ask_sweeper is not None and aw._ask_sweeper is not hilo, "y vuelve con la siguiente"


def test_si_el_barrido_no_arranca_se_reintenta(api, monkeypatch):
    """Revision del 27/10/2026: sin memoria, start() lanza, y dejaba puesto un
    hilo que nunca arranco: no se volvia a intentar."""
    class SinHilos(aw.threading.Thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(aw.threading, 'Thread', SinHilos)
    monkeypatch.setattr(aw, '_ask_sweeper', None)
    ENSURE_SWEEPER()
    assert aw._ask_sweeper is None


@pytest.fixture
def grabando(tr_dirs, monkeypatch):
    """Se esta grabando; el disco y la memoria, a gusto de cada test."""
    import disk_guard
    import live_transcriber
    estado = {'disco': 50.0, 'memoria': True}
    monkeypatch.setattr(disk_guard, 'system_free_gb', lambda: estado['disco'])
    monkeypatch.setattr(live_transcriber, '_memory_floor_ok', lambda: estado['memoria'])
    monkeypatch.setattr(aw, '_ask_yielding', False)
    (tr_dirs / '.recording_active').write_text(str(os.getpid()))   # el de pytest: un python vivo
    return estado


def test_sin_grabacion_nunca_cede(tr_dirs, monkeypatch):
    import disk_guard
    import live_transcriber
    monkeypatch.setattr(disk_guard, 'system_free_gb', lambda: 0.5)
    monkeypatch.setattr(live_transcriber, '_memory_floor_ok', lambda: False)
    assert aw._ask_yield_reason() is None


@pytest.mark.parametrize('proceso', ['muerto', 'otro programa'])
def test_el_aviso_de_una_grabacion_que_murio_no_cuenta(grabando, monkeypatch, proceso):
    """Revision del 27/10/2026: si el daemon muere grabando, .recording_active
    se queda hasta la siguiente grabacion."""
    import psutil

    class Otro:
        def __init__(self, pid):
            if proceso == 'muerto':
                raise psutil.NoSuchProcess(pid)

        def name(self):
            return 'explorer.exe'

    monkeypatch.setattr(psutil, 'Process', Otro)
    grabando['disco'] = 0.5
    assert aw._ask_yield_reason() is None


def test_grabando_cede_con_poco_disco_o_poca_memoria(grabando):
    assert aw._ask_yield_reason() is None
    grabando['disco'] = 2.0
    assert aw._ask_yield_reason() == 'disco'
    grabando.update(disco=50.0, memoria=False)
    assert aw._ask_yield_reason() == 'memoria'


def test_el_disco_tiene_histeresis(grabando):
    """Revision del 27/10/2026: rondando los 3 GB, se alternaba entre sesion y
    una sola vez, y la sesion vieja no veia las preguntas de en medio."""
    grabando['disco'] = 2.9
    assert aw._ask_yield_reason() == 'disco'
    grabando['disco'] = 3.5
    assert aw._ask_yield_reason() == 'disco', "sigue cediendo hasta WARN_CLEAR_GB"
    grabando['disco'] = 4.1
    assert aw._ask_yield_reason() is None
    grabando['disco'] = 3.5
    assert aw._ask_yield_reason() is None


def test_grabando_con_poco_disco_se_cierran_las_que_no_responden(api, cli, grabando):
    reciente = abierta(cli, 'live:a')
    ocupada = abierta(cli, 'meeting:b')
    grabando['disco'] = 2.0
    ocupada._lock.acquire()
    try:
        aw._sweep_ask_sessions()
    finally:
        ocupada._lock.release()
    assert reciente.proc.stdin.closed and ocupada.alive()
    assert set(aw._ask_sessions) == {'meeting:b'}


def test_grabando_con_poco_disco_no_se_precalienta(api, cli, grabando):
    grabando['disco'] = 2.0
    assert api.ask_warmup('live', 'reunion') is False
    time.sleep(0.05)
    assert cli['calls'] == [] and aw._ask_sessions == {}


def test_grabando_con_poco_disco_la_pregunta_no_deja_un_claude_abierto(api, cli, grabando, monkeypatch):
    monkeypatch.setattr(api, 'get_live_transcript', lambda stem: {'lines': LINES}, raising=False)
    grabando['disco'] = 2.0
    cli['next'].append(OneShot(turn('En la sala Nube.')))
    st = esperar_respuesta(api, api.ask_meeting('live', 'reunion', '¿Dónde?')['run_id'])
    assert st['text'] == 'En la sala Nube.' and st['error'] is None
    assert '--input-format' not in cli['calls'][0]['cmd'], "una sola vez, no una sesion"
    assert aw._ask_sessions == {}


def test_grabando_con_poco_disco_la_pregunta_cierra_antes_la_sesion_abierta(api, cli, grabando, monkeypatch):
    """Si siguiera abierta, habria dos claude a la vez, y al volver a ella no
    veria esta pregunta (revision del 27/10/2026)."""
    monkeypatch.setattr(api, 'get_live_transcript', lambda stem: {'lines': LINES}, raising=False)
    vieja = abierta(cli, 'live:reunion')
    grabando['disco'] = 2.0
    cli['next'].append(OneShot(turn('En la sala Nube.')))
    st = esperar_respuesta(api, api.ask_meeting('live', 'reunion', '¿Dónde?')['run_id'])
    assert st['text'] == 'En la sala Nube.'
    assert vieja.proc.stdin.closed and aw._ask_sessions == {}


def test_las_preguntas_no_dejan_nada_en_disco(api, cli, monkeypatch, tr_dirs):
    """Decision de Felipe (19/10/2026): solo las ve quien pregunta, y no se guardan."""
    monkeypatch.setattr(api, 'get_live_transcript', lambda stem: {'lines': LINES}, raising=False)
    antes = sorted(p.relative_to(tr_dirs) for p in tr_dirs.rglob('*'))
    cli['next'].append(SessionProc([turn('respuesta secreta')]))
    esperar_respuesta(api, api.ask_meeting('live', 'reunion', 'pregunta secreta')['run_id'])
    assert sorted(p.relative_to(tr_dirs) for p in tr_dirs.rglob('*')) == antes


def test_una_pregunta_desconocida_se_da_por_terminada(api):
    assert api.get_ask_status('no-existe') == {'text': '', 'done': True, 'error': 'unknown_run', 'upto': None}
