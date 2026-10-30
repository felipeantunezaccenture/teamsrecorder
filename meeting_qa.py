"""Preguntas sobre una reunion, respondidas desde su transcript con el CLI de claude.

Decisiones de Felipe (19/10/2026):
- El contexto es el transcript entero hasta ese momento, sin RAG: una reunion
  de dos horas son unos 35k tokens y cabe. Es lo que hacen Teams Copilot, Meet
  y Zoom, y la guia de Anthropic para documentos largos: el documento arriba,
  la pregunta al final.
- Modelo rapido por defecto (Sonnet 5.5, esfuerzo bajo), cambiable en Ajustes
  (`qa_model`). Opus al maximo tarda decenas de segundos por pregunta.
- Las preguntas y respuestas no se guardan; el log lleva tiempos y coste,
  nunca el texto.

Medido el 19/10/2026: el CLI tarda 9,5-12 s en arrancar y el modelo empieza a
responder 1,5 s despues. Por eso las preguntas van por una Session: un proceso
abierto (--input-format stream-json) que se arranca al abrir la caja de
preguntas. ask() es el camino de una sola vez, para el resumen y como red si la
sesion muere.

La evaluacion esta en .claude/evals/meeting_qa (scripts/eval_meeting_qa.py).
"""
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time

import config

log = logging.getLogger(__name__)

DEFAULT_MODEL = 'claude-sonnet-5-5'
# Sin Haiku 5.5, medido el 19/10/2026: con esfuerzo bajo piensa 8.500-12.500
# tokens por pregunta y tarda y cuesta mas que Sonnet (41-49 s y $0,10-0,17,
# frente a 4-5,5 s y $0,015). Un 'qa_model' antiguo con Haiku vuelve a Sonnet.
MODELS = ('claude-sonnet-5-5', 'claude-opus-5-5')
EFFORT = 'low'
TIMEOUT_S = 120
MAX_TRANSCRIPT_CHARS = 150_000
HISTORY_TURNS = 3
_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

_TS = re.compile(r'^\[(\d{1,3}:\d{2})\]')

SYSTEM_PROMPT = """Respondes preguntas sobre una reunion de trabajo a partir de su transcript.

Reglas:
- Usa solo lo que dice el transcript. Nada de conocimiento externo ni suposiciones.
- El transcript es automatico y puede tener palabras mal reconocidas: interpretalas por el contexto, sin inventar.
- Las etiquetas dicen quien habla: la de quien graba (su microfono) y [Otros] para el resto, sin distinguir entre ellos. No atribuyas frases a una persona concreta salvo que el propio transcript lo diga.
- Responde en el idioma de la pregunta, en pocas frases y en texto plano (como mucho, una lista corta).
- Apoya cada afirmacion en el minuto de la linea de donde sale, con el formato [mm:ss] copiado del transcript.
- Usa [mm:ss] SOLO para citar una linea del transcript, con su minuto exacto. Para hablar de un periodo ("los ultimos dos minutos", "desde el minuto 7"), dilo con palabras, sin corchetes.
- Si el transcript no trata lo que se pregunta, responde: "No se ha hablado de eso en la reunion (hasta [mm:ss])", con el ultimo minuto del transcript, traducido si la pregunta no es en espanol. Sin anadir suposiciones.
- El transcript puede ir llegando por partes: cuenta con todas las que hayas recibido."""

SUMMARY_PROMPT = """Resumes una reunion de trabajo a partir de su transcript automatico.

Devuelve exactamente dos lineas, en el idioma de la reunion y sin markdown:
TITULO: [3-5 palabras sobre el tema principal]
[Una o dos frases: de que fue la reunion y lo mas importante que salio de ella.]

Solo lo que dice el transcript; el reconocimiento puede tener errores."""


def qa_model() -> str:
    """El modelo de Ajustes (`qa_model`), o el de por defecto si falta o es raro."""
    model = config.read_settings().get('qa_model')
    return model if model in MODELS else DEFAULT_MODEL


def last_timestamp(lines: list[str]) -> str | None:
    for line in reversed(lines):
        m = _TS.match(line)
        if m:
            return m.group(1)
    return None


def build_prompt(lines: list[str], question: str, history: list[dict] | None = None,
                 me_label: str | None = None) -> str:
    """El transcript primero, entre etiquetas; la conversacion previa; la pregunta al final.

    history: [{'q': ..., 'a': ...}], de la mas antigua a la mas reciente; solo
    cuentan las ultimas HISTORY_TURNS, para poder repreguntar ("¿y quien lo hace?").
    Si el transcript no cabe, se queda con lo mas reciente y lo dice.
    """
    kept, size = [], 0
    for line in reversed(lines):
        size += len(line) + 1
        if size > MAX_TRANSCRIPT_CHARS:
            break
        kept.append(line)
    kept.reverse()
    upto = last_timestamp(lines) or '00:00'
    labelled = any('[Otros]' in line for line in kept)
    who = (f"[{me_label}] es quien graba (su microfono) y [Otros] el resto de participantes"
           if labelled and me_label else "sin etiquetas de quien habla")
    note = ''
    if len(kept) < len(lines):
        note = (f" Por longitud se omite el principio: empieza en "
                f"[{last_timestamp(kept[:1]) or '00:00'}].")
    parts = [f'<transcript fuente="Transcript automatico de la reunion hasta [{upto}]; {who}.{note}">',
             *kept, '</transcript>', '']
    turns = (history or [])[-HISTORY_TURNS:]
    if turns:
        parts.append('<conversacion_previa>')
        for t in turns:
            parts += [f"P: {t.get('q', '')}", f"R: {t.get('a', '')}"]
        parts += ['</conversacion_previa>', '']
    parts.append(f'Pregunta: {question.strip()}')
    return '\n'.join(parts)


def followup_message(new_lines: list[str], question: str, upto: str | None) -> str:
    """Las preguntas siguientes de una sesion: solo las lineas que aun no tenia
    (pueden ser de minutos anteriores: la pista de los demas va por detras) y la
    pregunta. El resto ya esta en la conversacion."""
    if not new_lines:
        return f'Pregunta: {question.strip()}'
    return '\n'.join([f'<transcript_nuevo fuente="Lineas del transcript que aun no tenias, hasta '
                      f'[{upto or "00:00"}]; pueden ser de minutos anteriores.">',
                      *new_lines, '</transcript_nuevo>', '', f'Pregunta: {question.strip()}'])


def command(model: str, system_prompt: str = SYSTEM_PROMPT) -> list[str]:
    """Sin herramientas ni MCP ni CLAUDE.md (--safe-mode mantiene el login;
    --bare exigiria API key), y en stream para ver la respuesta mientras se
    escribe. Flags comprobados en el CLI 2.1.287."""
    return [config.get_claude_bin(), '-p', '--safe-mode', '--model', model, '--effort', EFFORT,
            '--system-prompt', system_prompt, '--tools', '', '--strict-mcp-config',
            '--no-session-persistence', '--output-format', 'stream-json', '--verbose',
            '--include-partial-messages']


def _empty_result(model: str) -> dict:
    return {'text': '', 'error': None, 'model': model, 'first_text_s': None, 'total_s': None,
            'cost_usd': None, 'input_tokens': None, 'cache_read_tokens': None}


def _take(ev, out: dict, acc: list, t0: float, on_text) -> bool:
    """Un evento del stream. Va juntando el texto; True cuando llega el resultado.
    Una linea que no es un evento (no es un objeto JSON) se ignora."""
    if not isinstance(ev, dict):
        return False
    if ev.get('type') == 'stream_event':
        delta = (ev.get('event') or {}).get('delta') or {}
        if delta.get('type') == 'text_delta' and delta.get('text'):
            if out['first_text_s'] is None:
                out['first_text_s'] = round(time.monotonic() - t0, 2)
            acc.append(delta['text'])
            if on_text:
                on_text(''.join(acc))
    return ev.get('type') == 'result'


def _finish(out: dict, final: dict | None, acc: list, fallback_error: str) -> None:
    if final is None or final.get('is_error'):
        out['error'] = str((final or {}).get('result') or fallback_error).strip()[:300]
        return
    out['text'] = (final.get('result') or ''.join(acc)).strip()
    usage = final.get('usage') or {}
    out['input_tokens'] = usage.get('input_tokens')
    out['cache_read_tokens'] = usage.get('cache_read_input_tokens')


class Session:
    """Un `claude` abierto para una conversacion sobre una reunion.

    Se arranca al abrir la caja de preguntas, para que el arranque (9,5-12 s) ya
    haya pasado cuando se pregunta. La primera pregunta lleva el transcript
    entero; las siguientes, solo las lineas nuevas: el historial lo guarda la
    propia sesion. Una pregunta cada vez (lock). Nunca lanza.
    """
    IDLE_S = 600

    def __init__(self, model: str | None = None, me_label: str | None = None):
        self.model = model or qa_model()
        self.me_label = me_label
        self.sent: set[str] = set()
        self.last_used = time.monotonic()
        self._lock = threading.Lock()
        self._events: queue.Queue = queue.Queue()
        self._cost = 0.0
        self.proc = None
        if not config.get_claude_bin():
            return
        try:
            self.proc = subprocess.Popen(
                command(self.model) + ['--input-format', 'stream-json'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='replace', env=config.clean_env(),
                creationflags=_NO_WINDOW)
        except OSError as e:
            log.warning(f"meeting_qa: no se pudo abrir la sesion: {e}")
            self.proc = None
            return
        threading.Thread(target=self._read, daemon=True, name='AskSession').start()

    def _read(self) -> None:
        for raw in self.proc.stdout:
            self._events.put(raw)
        self._events.put(None)                      # el proceso termino

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and not self.proc.stdin.closed

    def idle(self) -> bool:
        return not self.busy() and time.monotonic() - self.last_used > self.IDLE_S

    def busy(self) -> bool:
        """Respondiendo ahora: no se cierra ni se desaloja."""
        return self._lock.locked()

    def message(self, lines: list[str], question: str, history: list[dict] | None = None) -> str:
        """history solo cuenta en la primera: si la sesion se cerro (se cerro la
        vista) y se abre otra, la conversacion que ve el usuario sigue."""
        if not self.sent:
            return build_prompt(lines, question, history, self.me_label)
        return followup_message([line for line in lines if line not in self.sent], question,
                                last_timestamp(lines))

    def ask(self, lines: list[str], question: str, history: list[dict] | None = None,
            on_text=None, timeout: float = TIMEOUT_S) -> dict:
        out = _empty_result(self.model)
        content = ''
        with self._lock:
            self.last_used = time.monotonic()     # ocupada desde ya, no al terminar
            if not config.get_claude_bin():
                out['error'] = 'no_cli'
                return out
            if not self.alive():
                out['error'] = 'session_dead'
                return out
            content = self.message(lines, question, history)
            t0 = time.monotonic()
            try:
                self.proc.stdin.write(json.dumps({'type': 'user', 'message': {
                    'role': 'user', 'content': content}}, ensure_ascii=False) + '\n')
                self.proc.stdin.flush()
            except (OSError, ValueError):         # ValueError: otro hilo la cerro
                out['error'] = 'session_dead'
                return out
            acc, final, dead = [], None, False
            deadline = t0 + timeout
            try:
                while time.monotonic() < deadline:
                    try:
                        raw = self._events.get(timeout=max(0.05, deadline - time.monotonic()))
                    except queue.Empty:
                        continue
                    if raw is None:
                        dead = True
                        break
                    try:
                        ev = json.loads(raw)
                    except ValueError:
                        continue
                    if _take(ev, out, acc, t0, on_text):
                        final = ev
                        break
            except Exception as e:                # el turno queda a medias en la cola:
                log.warning(f"meeting_qa: sesion rota a mitad de respuesta: {e}")
                self.close()                       # no se puede reutilizar
                dead = True
            out['total_s'] = round(time.monotonic() - t0, 2)
            if final is None and not dead:
                out['error'] = 'timeout'
                self.close()                          # a medias: no se puede reutilizar
            elif final is None:
                out['error'] = 'session_dead'
            else:
                _finish(out, final, acc, 'error')
                total = final.get('total_cost_usd')
                if isinstance(total, (int, float)):
                    out['cost_usd'] = round(total - self._cost, 6)   # el CLI lo da acumulado
                    self._cost = total
                if not out['error']:
                    self.sent.update(lines)
            self.last_used = time.monotonic()
        log.info(f"meeting_qa (sesion): {self.model} mensaje {len(content)} car., primer texto "
                 f"{out['first_text_s']}s, total {out['total_s']}s, coste {out['cost_usd']}, "
                 f"error {out['error']}")
        return out

    def close(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def ask(lines: list[str], question: str, history: list[dict] | None = None,
        me_label: str | None = None, model: str | None = None, on_text=None,
        timeout: float = TIMEOUT_S) -> dict:
    """Una pregunta con un proceso de una sola vez (lento: paga el arranque).
    Nunca lanza.

    on_text(texto_hasta_ahora) se llama segun llega la respuesta.
    Devuelve {'text', 'error', 'model', 'first_text_s', 'total_s', 'cost_usd',
    'input_tokens', 'cache_read_tokens'}. error: None, 'no_cli', 'timeout' o el
    mensaje del CLI.
    """
    return _run(build_prompt(lines, question, history, me_label), SYSTEM_PROMPT,
                model or qa_model(), on_text, timeout, 'meeting_qa')


def summarize(transcript: str, model: str | None = None, timeout: float = TIMEOUT_S) -> str | None:
    """'TITULO: ...' y una o dos frases, para el nivel de minutas "solo resumen".
    None si falla o no viene en ese formato: quien llama guarda unas minutas
    minimas en su lugar, y la reunion no se queda sin procesar."""
    kept, size = [], 0
    for line in transcript.splitlines():     # el principio dice de que va la reunion
        size += len(line) + 1
        if size > MAX_TRANSCRIPT_CHARS:
            break
        kept.append(line)
    prompt = '<transcript>\n' + '\n'.join(kept) + '\n</transcript>\n\nResume esta reunion.'
    r = _run(prompt, SUMMARY_PROMPT, model or qa_model(), None, timeout, 'meeting_summary')
    first, _, rest = r['text'].strip().partition('\n')
    if r['error'] or not first.startswith('TITULO:') or not first.removeprefix('TITULO:').strip():
        return None
    return first.strip() + '\n\n' + rest.strip()


def _run(prompt: str, system_prompt: str, model: str, on_text, timeout: float, tag: str) -> dict:
    out = _empty_result(model)
    if not config.get_claude_bin():
        out['error'] = 'no_cli'
        return out
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(command(model, system_prompt), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding='utf-8', errors='replace', env=config.clean_env(),
                                creationflags=_NO_WINDOW)
    except OSError as e:
        out['error'] = str(e)
        return out

    timed_out = threading.Event()

    def expire():
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    stderr_box = []
    drain = threading.Thread(target=lambda: stderr_box.append(proc.stderr.read()), daemon=True)
    drain.start()
    acc, final = [], None
    try:
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass                                    # murio antes de leer: lo dira stderr
        for raw in proc.stdout:
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if _take(ev, out, acc, t0, on_text):
                final = ev
        proc.wait()
    finally:
        timer.cancel()
    drain.join(2)
    out['total_s'] = round(time.monotonic() - t0, 2)
    if timed_out.is_set():
        out['error'] = 'timeout'
    else:
        _finish(out, final if proc.returncode == 0 else None, acc,
                (stderr_box[0] if stderr_box else '') or f'rc={proc.returncode}')
        if final and out['error'] is None:
            out['cost_usd'] = final.get('total_cost_usd')
    log.info(f"{tag}: {model} prompt {len(prompt)} car., primer texto {out['first_text_s']}s, "
             f"total {out['total_s']}s, coste {out['cost_usd']}, error {out['error']}")
    return out
