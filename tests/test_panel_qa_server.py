"""El servidor que abre el panel en un navegador para QA (scripts/panel_qa_server.py).

Lo que protege: que la QA nunca toque datos reales ni hable con otro servidor.
- No arranca donde corre Noted (un .lock de un python vivo); uno huerfano no cuenta.
- Las rutas de datos se redirigen al sandbox; las de codigo (web/) no.
- El puerto ocupado es un error, no un puerto compartido en silencio (Windows).
- Por el puente solo pasan los metodos publicos de AppAPI.
"""
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_PATH = Path(__file__).resolve().parent.parent / 'scripts' / 'panel_qa_server.py'
_spec = importlib.util.spec_from_file_location('panel_qa_server', _PATH)
qs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qs)


def test_un_lock_huerfano_no_impide_arrancar(tmp_path, monkeypatch):
    """19/10/2026: el .lock del Noted de prueba de la manana tenia un PID que
    ya era de un proceso de Edge."""
    monkeypatch.setattr(qs, 'ROOT', tmp_path)
    assert qs.noted_running_here() is False
    (tmp_path / '.lock').write_text('999999999')
    assert qs.noted_running_here() is False


def test_un_lock_de_un_python_vivo_impide_arrancar(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(qs, 'ROOT', tmp_path)
    (tmp_path / '.lock').write_text(str(os.getpid()))        # este pytest es un python
    assert qs.noted_running_here() is True


def test_las_rutas_de_datos_van_al_sandbox_y_las_demas_no(tmp_path, monkeypatch):
    root, sandbox = tmp_path / 'repo', tmp_path / 'sandbox'
    monkeypatch.setattr(qs, 'ROOT', root)
    mod = SimpleNamespace(PROJECT_DIR=root, MINUTES_DIR=root / 'minutes', TASKS_FILE=root / 'tasks.json',
                          WEB_DIR=root / 'web', RECORDINGS_DIR=Path('C:/otra/parte'))
    qs.rebase(mod, sandbox)
    assert mod.PROJECT_DIR == sandbox and mod.MINUTES_DIR == sandbox / 'minutes'
    assert mod.TASKS_FILE == sandbox / 'tasks.json'
    assert mod.WEB_DIR == root / 'web', "no es una ruta de datos"
    assert mod.RECORDINGS_DIR == Path('C:/otra/parte'), "fuera del repo no se toca"


def test_el_puerto_ocupado_es_un_error():
    assert qs._Server.allow_reuse_address is False


TOKEN, HOSTS = 'tok123', {'127.0.0.1:8799', 'localhost:8799'}


def _post(handler_cls, name, args, token=TOKEN, host='127.0.0.1:8799'):
    h = handler_cls.__new__(handler_cls)
    h.path = f'/api/{name}'
    body = json.dumps(args).encode()
    h.headers = {'Content-Length': str(len(body)), 'X-QA-Token': token, 'Host': host}
    h.rfile, h.wfile = io.BytesIO(body), io.BytesIO()
    sent = {}
    h.send_response = lambda code: sent.__setitem__('code', code)
    h.send_header = lambda *a: None
    h.end_headers = lambda: None
    h.do_POST()
    return sent['code'], json.loads(h.wfile.getvalue())


def test_el_puente_llama_a_la_api_y_devuelve_sus_errores():
    class Api:
        def hola(self, nombre):
            return f'hola {nombre}'

        def falla(self):
            raise ValueError('mal')

        def _privado(self):
            return 'no'

    handler = qs.make_handler(Api(), TOKEN, HOSTS)
    assert _post(handler, 'hola', ['Felipe']) == (200, {'ok': True, 'result': 'hola Felipe'})
    code, body = _post(handler, 'falla', [])
    assert code == 200 and body == {'ok': False, 'error': 'ValueError: mal'}
    assert _post(handler, '_privado', [])[0] == 404
    assert _post(handler, 'no_existe', [])[0] == 404


@pytest.mark.parametrize('token, host', [
    ('', '127.0.0.1:8799'),                 # otra web: un POST "simple" sin la cabecera
    ('otro', '127.0.0.1:8799'),
    (TOKEN, 'atacante.example:8799'),       # DNS rebinding: el Host es el del atacante
])
def test_sin_el_token_y_el_host_del_panel_no_se_llama_a_nada(token, host):
    """Revision del 19/10/2026 (CRITICO): cualquier web abierta en el navegador
    podia hacer fetch('http://127.0.0.1:8799/api/open_file', {method: 'POST',
    mode: 'no-cors', ...}) y abrir cualquier fichero con os.startfile.
    Escuchar solo en 127.0.0.1 no lo impide."""
    llamadas = []

    class Api:
        def open_file(self, path):
            llamadas.append(path)

    code, _ = _post(qs.make_handler(Api(), TOKEN, HOSTS), 'open_file', ['C:/x.exe'], token=token, host=host)
    assert code == 403 and llamadas == []


def test_el_puente_lleva_el_token():
    assert "'X-QA-Token': 'tok123'" in qs.bridge_js('tok123')


@pytest.mark.parametrize('caso', ['padre_del_repo', 'carpeta_ajena_con_cosas'])
def test_nunca_borra_una_carpeta_que_no_sea_suya(tmp_path, monkeypatch, caso):
    """Revision del 19/10/2026 (ALTO): con --sandbox apuntando al padre del repo,
    el rmtree del arranque se llevaba todos los repos, sin avisar."""
    root = tmp_path / 'repos' / 'noted'
    root.mkdir(parents=True)
    monkeypatch.setattr(qs, 'ROOT', root)
    if caso == 'padre_del_repo':
        target = tmp_path / 'repos'
    else:
        target = tmp_path / 'mis_cosas'
        target.mkdir()
        (target / 'importante.txt').write_text('no borrar')
    assert qs.prepare_sandbox(target) is not None
    assert root.exists() and (caso == 'padre_del_repo' or (target / 'importante.txt').exists())


def test_un_sandbox_suyo_se_rehace_y_queda_marcado(tmp_path, monkeypatch):
    monkeypatch.setattr(qs, 'ROOT', tmp_path / 'repo')
    sandbox = tmp_path / 'sb'
    assert qs.prepare_sandbox(sandbox) is None
    (sandbox / 'resto.txt').write_text('de la vez anterior')
    assert qs.prepare_sandbox(sandbox) is None
    assert not (sandbox / 'resto.txt').exists() and (sandbox / qs.SANDBOX_MARK).exists()
