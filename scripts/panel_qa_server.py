"""El panel de Noted en un navegador normal, para probarlo (/gstack-qa).

pywebview solo inyecta su puente (window.pywebview.api) dentro de su propia
ventana, asi que fuera de ella el panel no arranca. Este servidor sirve web/
con un puente equivalente por HTTP, que llama a los metodos REALES de AppAPI,
contra un sandbox con reuniones y una grabacion en vivo inventadas: nunca con
datos de nadie.

    python scripts/panel_qa_server.py [--port 8799] [--sandbox DIR]

- Solo escucha en 127.0.0.1.
- Se niega a arrancar en un checkout donde corre Noted (hay .lock): ahi estan
  los datos de verdad.
- Todo lo que el panel escribe (ajustes, pins, tareas) va al sandbox.
- Las preguntas sobre la reunion llaman a Claude de verdad (la reunion es
  inventada, ver .claude/evals/meeting_qa/reunion_sintetica.txt).
"""
import argparse
import hmac
import json
import os
import secrets
import shutil
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'web'
SAMPLE = ROOT / '.claude' / 'evals' / 'meeting_qa' / 'reunion_sintetica.txt'
# Rutas de datos que cada modulo copia de config al importarse (las mismas que
# redirige tests/conftest.py).
SEAMS = ('PROJECT_DIR', 'BASE_OUTPUT_DIR', 'RECORDINGS_DIR', 'MINUTES_DIR', 'INBOX_DIR',
         'TASKS_FILE', 'BUCKETS_FILE', 'CLI_CONTROL_FILE', 'LOG_FILE', '_CANCEL_FILE', 'LOCK_FILE')

SANDBOX_MARK = '.noted_panel_qa'

# El token va en una cabecera propia: una web ajena no puede leerlo (misma
# origen) ni mandar la cabecera sin un preflight que este servidor no contesta.
_BRIDGE = """
// Puente de QA: hace de window.pywebview.api por HTTP (scripts/panel_qa_server.py).
window.pywebview = { api: new Proxy({}, { get: (_, name) => async (...args) => {
  const r = await fetch('/api/' + String(name), { method: 'POST', body: JSON.stringify(args),
                                                  headers: { 'X-QA-Token': '__TOKEN__' } });
  const data = await r.json();
  if (!data.ok) throw new Error(data.error);
  return data.result;
} }) };
window.addEventListener('load', () => window.dispatchEvent(new Event('pywebviewready')));
"""


def bridge_js(token: str) -> str:
    return _BRIDGE.replace('__TOKEN__', token)


def prepare_sandbox(sandbox: Path) -> str | None:
    """Deja el sandbox vacio y marcado. Devuelve el motivo si no se atreve.

    Solo borra una carpeta que este mismo script marco (SANDBOX_MARK) o que este
    vacia: con --sandbox apuntando al padre del repo, un rmtree a ciegas se
    llevaba todos los repos (revision del 19/10/2026)."""
    sandbox = sandbox.resolve()
    if sandbox == ROOT or ROOT in sandbox.parents or sandbox in ROOT.parents:
        return 'el sandbox no puede contener el repo ni estar dentro de el'
    if sandbox.exists():
        if not sandbox.is_dir():
            return f'{sandbox} no es una carpeta'
        if not (sandbox / SANDBOX_MARK).exists() and any(sandbox.iterdir()):
            return f'{sandbox} tiene contenido y no es un sandbox de este script'
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True)
    (sandbox / SANDBOX_MARK).write_text('sandbox de scripts/panel_qa_server.py: se borra al arrancar\n',
                                        encoding='utf-8')
    return None


def noted_running_here() -> bool:
    """Un .lock con el PID de un python vivo, el mismo criterio que
    main._check_single_instance. Un .lock huerfano (su PID ya es de otro
    programa) no cuenta."""
    lock = ROOT / '.lock'
    if not lock.exists():
        return False
    try:
        import psutil
        proc = psutil.Process(int(lock.read_text().strip()))
        return 'python' in proc.name().lower()
    except Exception:
        return False


def sandbox_paths(sandbox: Path) -> None:
    """Redirige las rutas de datos al sandbox ANTES de importar el panel: los
    modulos que se importen despues copian ya las del sandbox."""
    sys.path.insert(0, str(ROOT))
    import config
    rebase(config, sandbox)
    import app_window  # noqa: F401  (importa el resto de modulos del panel)
    for mod in list(sys.modules.values()):
        rebase(mod, sandbox)


def rebase(mod, sandbox: Path) -> None:
    for name in SEAMS:
        value = getattr(mod, name, None)
        if isinstance(value, Path):
            try:
                rel = value.resolve().relative_to(ROOT)
            except ValueError:
                continue
            setattr(mod, name, sandbox / rel)


def seed(sandbox: Path) -> None:
    """Una reunion con minutas completas, una con minutas breves y una grabacion
    en vivo en curso. Todo inventado."""
    minutes, recordings = sandbox / 'minutes', sandbox / 'recordings'
    for d in (minutes, recordings / 'processed', sandbox / 'inbox'):
        d.mkdir(parents=True, exist_ok=True)
    transcript = SAMPLE.read_text(encoding='utf-8')

    full = minutes / '20261019_1000_Taller_Atlas.md'
    full.write_text('## Resumen Ejecutivo\n\nSe preparo el taller de priorizacion del proyecto Atlas: '
                    'jueves 23 a las 10:00 en la sala Nube.\n\n## Decisiones\n\n- Miro para el taller.\n',
                    encoding='utf-8')
    full.with_name(full.stem + '_transcript.txt').write_text(transcript, encoding='utf-8')
    (minutes / f'{full.stem}_actions.json').write_text(json.dumps({
        'minutes': str(full), 'project_id': 'none', 'actions': [{
            'index': 0, 'type': 'human', 'title': 'Enviar la agenda definitiva', 'executed': False,
            'assignee': 'Felipe', 'deadline': None, 'claude_executable': False}]}), encoding='utf-8')

    sys.path.insert(0, str(ROOT))
    from minutes_generator import brief_minutes, extract_title_from_minutes
    brief = minutes / '20261019_1100_Seguimiento_Atlas.md'
    _t, content = extract_title_from_minutes(brief_minutes(
        'Seguimiento Atlas', 'summary', 'TITULO: Seguimiento Atlas\n\nSe repasaron fecha y sitio del taller.'))
    brief.write_text(content, encoding='utf-8')
    brief.with_name(brief.stem + '_transcript.txt').write_text(transcript, encoding='utf-8')

    stem = '2026-10-19_12-00_Entrevista_QA'
    records = []
    for line in transcript.splitlines()[:20]:
        mm, ss = line[1:6].split(':')
        t = int(mm) * 60 + int(ss)
        track = 'mic' if '[Felipe]' in line else 'loop'
        text = line.split(': ', 1)[1]
        records.append({'track': track, 'start_frame': t * 16000, 'end_frame': (t + 10) * 16000,
                        'segments': [[t, text]], 'lang': 'es', 'model': 'small'})
    (recordings / f'{stem}.live.jsonl').write_text(
        ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records), encoding='utf-8')
    (recordings / f'{stem}.live.json').write_text(json.dumps({'state': 'running', 'mode': 'live',
                                                              'lang': 'es', 'model': 'small'}), encoding='utf-8')
    (recordings / f'{stem}.live.ctl').write_text(json.dumps({'mode': 'live', 'gate': 'free'}), encoding='utf-8')
    (sandbox / '.pipeline_status.json').write_text(json.dumps({'jobs': [{
        'stage': 'recording', 'label': 'Recording 05:00', 'elapsed': 300, 'pct': None,
        'title': 'Entrevista Qa', 'time': '12:00', 'step': 0, 'total_steps': 3, 'step_label': 'Grabando',
        'mode': 'live', 'rec_stem': stem, 'live_stem': stem}]}), encoding='utf-8')
    (sandbox / 'settings.json').write_text(json.dumps({'language': 'es', 'onboarding_completed': True,
                                                       'user_name': 'Felipe'}), encoding='utf-8')


class _Server(ThreadingHTTPServer):
    # En Windows, SO_REUSEADDR deja a dos servidores compartir puerto sin error,
    # y las peticiones iban al otro (medido el 19/10/2026 con un http.server de
    # otro proyecto en el mismo puerto). Que falle si el puerto esta cogido.
    allow_reuse_address = False


def make_handler(api, token: str, hosts: set[str]):
    """hosts: los Host validos (127.0.0.1:puerto, localhost:puerto). Otro Host es
    DNS rebinding: una web ajena que resuelve su dominio a 127.0.0.1."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _host_ok(self) -> bool:
            return self.headers.get('Host', '') in hosts

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b'host', 'text/plain')
            path = self.path.split('?', 1)[0]
            if path in ('/', '/index.html'):
                html = (WEB / 'index.html').read_text(encoding='utf-8')
                html = html.replace('<script', '<script src="/qa_bridge.js"></script>\n<script', 1)
                return self._send(200, html.encode('utf-8'), 'text/html; charset=utf-8')
            if path == '/qa_bridge.js':
                return self._send(200, bridge_js(token).encode('utf-8'), 'application/javascript')
            target = (WEB / path.lstrip('/')).resolve()
            if WEB.resolve() not in target.parents or not target.is_file():
                return self._send(404, b'not found', 'text/plain')
            ctype = {'.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml',
                     '.png': 'image/png', '.html': 'text/html; charset=utf-8'}.get(target.suffix,
                                                                                  'application/octet-stream')
            self._send(200, target.read_bytes(), ctype)

        def do_POST(self):
            if not self._host_ok() or not hmac.compare_digest(self.headers.get('X-QA-Token', ''), token):
                return self._send(403, json.dumps({'ok': False, 'error': 'forbidden'}).encode(),
                                  'application/json')
            name = self.path.split('?', 1)[0].removeprefix('/api/')
            method = getattr(api, name, None)
            if name.startswith('_') or not callable(method):
                return self._send(404, json.dumps({'ok': False, 'error': f'no existe {name}'}).encode(),
                                  'application/json')
            try:
                args = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'[]')
                body = {'ok': True, 'result': method(*args)}
            except Exception as e:                   # como pywebview: el error llega a la promesa
                body = {'ok': False, 'error': f'{type(e).__name__}: {e}'}
            self._send(200, json.dumps(body, ensure_ascii=False, default=str).encode('utf-8'),
                       'application/json')

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8799)
    ap.add_argument('--sandbox', type=Path, default=Path(tempfile.gettempdir()) / 'noted_panel_qa')
    args = ap.parse_args()
    if noted_running_here():
        print(f'NO: en {ROOT} corre Noted (.lock). Lanza esto desde otro checkout.', file=sys.stderr)
        return 2
    sandbox = args.sandbox.resolve()
    problem = prepare_sandbox(sandbox)
    if problem:
        print(f'NO: {problem}.', file=sys.stderr)
        return 2
    os.environ['OUTPUT_DIR'] = str(sandbox)
    seed(sandbox)
    sandbox_paths(sandbox)
    import app_window
    import config
    # Un output_dir en settings.json manda sobre OUTPUT_DIR: si apuntara a datos
    # reales, mejor no arrancar.
    for name in ('PROJECT_DIR', 'MINUTES_DIR', 'RECORDINGS_DIR'):
        for mod in (config, app_window):
            p = Path(getattr(mod, name)).resolve()
            if p != sandbox and sandbox not in p.parents:
                print(f'NO: {mod.__name__}.{name} no esta en el sandbox.', file=sys.stderr)
                return 2
    api = app_window.AppAPI()
    hosts = {f'127.0.0.1:{args.port}', f'localhost:{args.port}'}
    try:
        server = _Server(('127.0.0.1', args.port), make_handler(api, secrets.token_urlsafe(24), hosts))
    except OSError as e:
        print(f'NO: el puerto {args.port} esta ocupado ({e}). Usa --port.', file=sys.stderr)
        return 2
    print(f'Panel de QA en http://127.0.0.1:{args.port}  (sandbox {sandbox})', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app_window._close_ask_sessions()
    return 0


if __name__ == '__main__':
    sys.exit(main())
