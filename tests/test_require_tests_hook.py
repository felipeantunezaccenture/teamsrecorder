"""Tests del hook que obliga a escribir tests.

Si el mecanismo que obliga no esta probado, no sirve de nada: un hook roto
falla abierto y nadie se entera. Asi que la regla se aplica a si misma.
"""
import importlib.util
import io
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

HOOK = Path(__file__).resolve().parent.parent / '.claude' / 'hooks' / 'require_tests.py'


@pytest.fixture(scope='module')
def hook():
    spec = importlib.util.spec_from_file_location('require_tests', HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Que cuenta como codigo de la app ──────────────────────────────────────

@pytest.mark.parametrize('ruta', [
    'tray_app.py',
    'audio_recorder.py',
    'web/../transcriber.py',
    'hooks/send_update_email.py',
])
def test_el_codigo_de_la_app_exige_test(hook, ruta):
    assert hook._es_codigo_de_app(ruta) is True


@pytest.mark.parametrize('ruta', [
    'tests/test_algo.py',                   # el test mismo
    '.claude/hooks/require_tests.py',       # el propio hook
    'check_audio.py',                       # scripts de diagnostico manual
    'check_teams.py',
    'diagnostico.py',
    'tools/_merge_wavs.py',
    'README.md',                            # documentacion
    'web/app.js',                           # no es Python
    'pyproject.toml',
    'requirements.txt',
])
def test_lo_que_no_exige_test(hook, ruta):
    assert hook._es_codigo_de_app(ruta) is False


# ── El bloqueo ────────────────────────────────────────────────────────────

@pytest.fixture
def ejecutar(hook, monkeypatch):
    """Ejecuta el hook con una lista de ficheros cambiados simulada."""
    def run(cambiados, stop_hook_active=False):
        monkeypatch.setattr(hook, '_cambiados', lambda: cambiados)
        monkeypatch.setattr(
            'sys.stdin',
            io.StringIO(json.dumps({'stop_hook_active': stop_hook_active})),
        )
        return hook.main()
    return run


def test_codigo_sin_tests_bloquea(ejecutar):
    assert ejecutar(['tray_app.py']) == 2


def test_codigo_con_tests_pasa(ejecutar):
    assert ejecutar(['tray_app.py', 'tests/test_pipeline_cancel.py']) == 0


def test_solo_tests_pasa(ejecutar):
    assert ejecutar(['tests/test_algo.py']) == 0


def test_sin_cambios_pasa(ejecutar):
    assert ejecutar([]) == 0


def test_solo_documentacion_pasa(ejecutar):
    """Cambiar un README no necesita test: el hook no debe ser un peaje."""
    assert ejecutar(['README.md', 'docs/guia.md']) == 0


def test_solo_configuracion_pasa(ejecutar):
    assert ejecutar(['pyproject.toml', '.gitignore']) == 0


def test_varios_ficheros_de_codigo_sin_tests_bloquean(ejecutar):
    assert ejecutar(['tray_app.py', 'transcriber.py', 'storage.py']) == 2


def test_un_script_de_diagnostico_solo_no_bloquea(ejecutar):
    assert ejecutar(['check_audio.py']) == 0


def test_no_insiste_si_ya_bloqueo(ejecutar):
    """Anti-bucle: sin esto, un turno legitimamente sin test se quedaria
    atrapado dando vueltas."""
    assert ejecutar(['tray_app.py'], stop_hook_active=True) == 0


def test_el_motivo_del_bloqueo_nombra_los_ficheros(ejecutar, capsys):
    """El mensaje va a stderr y es lo unico que el agente recibe: si no dice
    QUE fichero falta cubrir, el bloqueo es inutil."""
    ejecutar(['tray_app.py', 'transcriber.py'])
    err = capsys.readouterr().err
    assert 'tray_app.py' in err
    assert 'transcriber.py' in err
    assert 'pytest' in err, "el mensaje deberia decir como comprobarlo"


def test_stdin_invalido_no_revienta_el_hook(hook, monkeypatch):
    """Un hook que lanza una excepcion falla abierto y deja pasar todo. Debe
    seguir bloqueando aunque no entienda la entrada."""
    monkeypatch.setattr(hook, '_cambiados', lambda: ['tray_app.py'])
    monkeypatch.setattr('sys.stdin', io.StringIO('esto no es json'))
    assert hook.main() == 2


# ── Lectura real de git ───────────────────────────────────────────────────

def test_cambiados_lee_git_de_verdad(hook):
    """No comprueba el contenido (cambia en cada sesion), solo que la lectura
    de git funciona y devuelve rutas relativas con barras."""
    resultado = hook._cambiados()
    assert isinstance(resultado, list)
    assert all('\\' not in r for r in resultado), "git deberia dar barras /"


# ── Modo pre-commit: misma logica, otra fuente y otro codigo de salida ────

def test_en_modo_staged_bloquea_con_codigo_1(hook, monkeypatch):
    """pre-commit espera 1, no 2."""
    monkeypatch.setattr(hook, '_en_staging', lambda: ['tray_app.py'])
    assert hook.main(['--staged']) == 1


def test_en_modo_staged_con_tests_pasa(hook, monkeypatch):
    monkeypatch.setattr(hook, '_en_staging', lambda: ['tray_app.py', 'tests/test_x.py'])
    assert hook.main(['--staged']) == 0


def test_el_modo_staged_ignora_el_arbol_de_trabajo(hook, monkeypatch):
    """Lo que importa en un commit es lo que ENTRA en el commit: un .py
    modificado pero sin anadir no debe bloquearlo."""
    monkeypatch.setattr(hook, '_en_staging', lambda: ['README.md'])
    monkeypatch.setattr(hook, '_cambiados', lambda: ['tray_app.py'])
    assert hook.main(['--staged']) == 0


def test_el_modo_staged_no_lee_stdin(hook, monkeypatch):
    """El hook de pre-commit no recibe JSON por stdin: si lo intentara leer se
    quedaria esperando y colgaria el commit."""
    monkeypatch.setattr(hook, '_en_staging', lambda: [])

    class StdinQueRevienta:
        def read(self):
            raise AssertionError("no debe leer stdin en modo --staged")

    monkeypatch.setattr('sys.stdin', StdinQueRevienta())
    assert hook.main(['--staged']) == 0


def test_el_mensaje_de_pre_commit_explica_como_saltarlo(hook, monkeypatch, capsys):
    monkeypatch.setattr(hook, '_en_staging', lambda: ['tray_app.py'])
    hook.main(['--staged'])
    assert '--no-verify' in capsys.readouterr().err


def test_git_que_falla_no_rompe_el_hook(hook, monkeypatch):
    def boom(*_a, **_k):
        raise OSError('git no encontrado')

    monkeypatch.setattr(hook.subprocess, 'run', boom)
    assert hook._git('diff') == []
