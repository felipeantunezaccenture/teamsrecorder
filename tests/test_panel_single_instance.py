"""Un solo panel (26/10/2026).

Felipe: al hacer clic en el icono de la bandeja, el panel se abria dos veces.
pystray lanza "Abrir la aplicacion" en cada WM_LBUTTONUP, asi que un doble clic
son dos aperturas en ~300 ms; open_app miraba .app_window.pid, que el panel
escribe al cabo de unos segundos, y lanzaba dos. Y los dos paneles compartian
ese fichero: al cerrar uno se borraba, y desde entonces cada clic abria otro.

LO QUE PROTEGEN
1. El panel es de instancia unica por un mutex con nombre (el patron estandar
   de Windows, el mismo que usa main.py para el daemon). Probado contra la API
   real: un doble de la API no habria visto un fallo como el del 23/10.
2. El segundo que arranca no abre ventana: deja la ruta al primero, lo trae al
   frente y sale, sin llegar a cargar pywebview.
3. Cerrar un panel no borra el .app_window.pid de otro.
"""
import ctypes
import uuid

import pytest

import app_window as aw

pytestmark = pytest.mark.unit

windows = pytest.mark.skipif(not hasattr(ctypes, 'windll'), reason='solo Windows')


@pytest.fixture
def mutex_propio(monkeypatch):
    """Un nombre de mutex solo para este test, y el handle cerrado al acabar."""
    monkeypatch.setattr(aw, '_WINDOW_MUTEX', f'Noted_AppWindow_test_{uuid.uuid4().hex}')
    monkeypatch.setattr(aw, '_WINDOW_MUTEX_HANDLE', None, raising=False)
    yield
    h = getattr(aw, '_WINDOW_MUTEX_HANDLE', None)
    if h:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))


@windows
def test_el_primer_panel_se_queda_y_el_segundo_no(mutex_propio):
    primero = aw._claim_single_window()
    handle = aw._WINDOW_MUTEX_HANDLE
    segundo = aw._claim_single_window()
    ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(aw._WINDOW_MUTEX_HANDLE))
    aw._WINDOW_MUTEX_HANDLE = handle
    assert primero is True and segundo is False


def test_el_segundo_panel_manda_la_ruta_al_primero_y_sale(tr_dirs, monkeypatch):
    import sys
    enfocadas = []
    monkeypatch.setattr(aw, '_claim_single_window', lambda: False)
    monkeypatch.setattr(aw, '_focus_existing_window', lambda: enfocadas.append(1))
    monkeypatch.setitem(sys.modules, 'webview', None)      # importarlo lanzaria ImportError
    aw._run_window('C:/minutes/20261026_0900_Comite.md')
    assert (aw.PROJECT_DIR / '.app_navigate.txt').read_text(encoding='utf-8') == 'C:/minutes/20261026_0900_Comite.md'
    assert enfocadas == [1]
    assert not (aw.PROJECT_DIR / '.app_window.pid').exists()


def test_cerrar_un_panel_no_borra_el_pid_de_otro(tr_dirs):
    pid_file = aw.PROJECT_DIR / '.app_window.pid'
    pid_file.write_text('111', encoding='utf-8')
    aw._release_pid_file(pid_file, 222)
    assert pid_file.read_text(encoding='utf-8') == '111'
    aw._release_pid_file(pid_file, 111)
    assert not pid_file.exists()


# -- Lo que se ve desde fuera: traer el panel delante, y cuando no se puede ---

@pytest.mark.parametrize('focus, enfocadas', [(True, [1]), (False, [])])
def test_con_un_panel_abierto_se_le_manda_la_ruta_y_solo_se_trae_delante_si_se_pide(
        tr_dirs, monkeypatch, focus, enfocadas):
    """focus=False (77140da): al terminar las minutas, la ruta va al panel ya
    abierto pero no se le pone delante de lo que se este haciendo."""
    import os
    vistas = []
    monkeypatch.setattr(aw, '_focus_existing_window', lambda: vistas.append(1))
    (aw.PROJECT_DIR / '.app_window.pid').write_text(str(os.getpid()), encoding='utf-8')

    aw.open_app('C:/minutes/20261026_0900_Comite.md', focus=focus)

    assert (aw.PROJECT_DIR / '.app_navigate.txt').read_text(encoding='utf-8') == 'C:/minutes/20261026_0900_Comite.md'
    assert vistas == enfocadas


class _User32:
    def __init__(self, hwnd):
        self.hwnd, self.llamadas = hwnd, []

    def FindWindowW(self, _cls, titulo):
        self.llamadas.append(('find', titulo))
        return self.hwnd

    def ShowWindow(self, hwnd, cmd):
        self.llamadas.append(('show', hwnd, cmd))

    def SetForegroundWindow(self, hwnd):
        self.llamadas.append(('front', hwnd))


@pytest.mark.parametrize('hwnd', [123, 0], ids=['abierto', 'no_encontrado'])
def test_traer_el_panel_delante(monkeypatch, hwnd):
    import types
    user32 = _User32(hwnd)
    monkeypatch.setattr(aw.os, 'name', 'nt')
    monkeypatch.setattr(ctypes, 'windll', types.SimpleNamespace(user32=user32), raising=False)
    aw._focus_existing_window()
    if hwnd:
        assert user32.llamadas == [('find', 'Noted'), ('show', 123, 9), ('front', 123)]
    else:
        assert user32.llamadas == [('find', 'Noted')]


@pytest.mark.parametrize('falla', ['sin_handle', 'excepcion'])
def test_si_el_mutex_no_se_puede_crear_se_abre_el_panel(monkeypatch, falla):
    """Mejor abrir de mas que no abrir: sin mutex no se sabe si hay otro panel."""
    class CreateMutexW:
        """Como una funcion de ctypes: admite que se le pongan restype y argtypes."""

        def __call__(self, *_a):
            if falla == 'excepcion':
                raise OSError('sin permisos')
            return 0

    class Kernel32:
        def __init__(self):
            self.CreateMutexW = CreateMutexW()

    monkeypatch.setattr(ctypes, 'WinDLL', lambda *_a, **_k: Kernel32(), raising=False)
    monkeypatch.setattr(aw.os, 'name', 'nt')
    assert aw._claim_single_window() is True


def test_cerrar_un_panel_sin_pid_no_lanza(tr_dirs):
    aw._release_pid_file(aw.PROJECT_DIR / '.app_window.pid', 111)
