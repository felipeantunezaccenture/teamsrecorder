"""El lock de instancia unica.

Dos daemons a la vez significan dos grabadores peleandose por el microfono y
escribiendo sobre el mismo fichero. El lock lo impide, y su fallo clasico es
que la SEGUNDA instancia le robe el lock a la primera: aqui esta bien resuelto
y estos tests lo dejan clavado.
"""
import ctypes

import pytest

import main

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _mutex_libre(monkeypatch):
    """Simula que el mutex de Windows esta libre antes de cada test.

    Sin esto, la primera llamada a _check_single_instance() adquiere el mutex
    para el lifetime del proceso y todos los tests siguientes lo ven como
    ERROR_ALREADY_EXISTS aunque no haya ningun daemon real corriendo.
    """
    class _FakeKernel32:
        def CreateMutexW(self, attr, owner, name):
            return 999

        def GetLastError(self):
            return 0  # success: mutex adquirido limpio

        def ReleaseMutex(self, h):
            pass

        def CloseHandle(self, h):
            pass

    monkeypatch.setattr(ctypes, 'windll',
                        type('_windll', (), {'kernel32': _FakeKernel32()})())
    main.__dict__.pop('_SINGLETON_MUTEX', None)
    yield
    main.__dict__.pop('_SINGLETON_MUTEX', None)


@pytest.fixture
def psutil_falso(monkeypatch):
    """Controla que procesos existen y como se llaman, sin depender de los
    procesos reales de la maquina."""
    import psutil

    estado = {'vivos': {}, 'nombres': {}, 'lanza': None}

    class ProcesoFalso:
        def __init__(self, pid):
            self._pid = pid

        def name(self):
            if estado['lanza']:
                raise estado['lanza']
            return estado['nombres'].get(self._pid, 'python.exe')

    monkeypatch.setattr(psutil, 'pid_exists', lambda pid: estado['vivos'].get(pid, False))
    monkeypatch.setattr(psutil, 'Process', ProcesoFalso)
    return estado


def test_sin_lock_se_puede_arrancar(tr_dirs, psutil_falso):
    assert main._check_single_instance() is True
    assert main.LOCK_FILE.exists()


def test_el_lock_guarda_el_pid_propio(tr_dirs, psutil_falso):
    import os
    main._check_single_instance()
    assert main.LOCK_FILE.read_text().strip() == str(os.getpid())


def test_con_otra_instancia_viva_no_arranca(tr_dirs, psutil_falso):
    main.LOCK_FILE.write_text('4242')
    psutil_falso['vivos'][4242] = True
    psutil_falso['nombres'][4242] = 'pythonw.exe'

    assert main._check_single_instance() is False


def test_al_rechazar_el_arranque_NO_se_pisa_el_lock_ajeno(tr_dirs, psutil_falso):
    """Este es el invariante que importa. Si la segunda instancia sobrescribiera
    el lock, al salir borraria el de la primera y quedaria todo desprotegido."""
    main.LOCK_FILE.write_text('4242')
    psutil_falso['vivos'][4242] = True
    psutil_falso['nombres'][4242] = 'pythonw.exe'

    main._check_single_instance()

    assert main.LOCK_FILE.read_text().strip() == '4242'


def test_un_lock_de_un_proceso_muerto_se_reutiliza(tr_dirs, psutil_falso):
    """El caso del watchdog: el daemon murio sin limpiar y hay que poder
    relanzarlo."""
    main.LOCK_FILE.write_text('4242')
    psutil_falso['vivos'][4242] = False

    assert main._check_single_instance() is True


def test_un_lock_de_un_proceso_que_no_es_python_se_reutiliza(tr_dirs, psutil_falso):
    """El PID se reciclo y ahora lo tiene otra cosa: no es nuestra instancia."""
    main.LOCK_FILE.write_text('4242')
    psutil_falso['vivos'][4242] = True
    psutil_falso['nombres'][4242] = 'chrome.exe'

    assert main._check_single_instance() is True


@pytest.mark.parametrize('basura', ['abc', '', '   ', '12.5', 'pid=3'])
def test_un_lock_corrupto_no_bloquea_el_arranque(tr_dirs, psutil_falso, basura):
    """Un lock ilegible no puede dejar la app inarrancable."""
    main.LOCK_FILE.write_text(basura)
    assert main._check_single_instance() is True


def test_si_no_se_puede_leer_el_nombre_del_proceso_se_arranca(tr_dirs, psutil_falso):
    """psutil.AccessDenied con un proceso de otro usuario: ante la duda, no
    dejar la app bloqueada."""
    import psutil

    main.LOCK_FILE.write_text('4242')
    psutil_falso['vivos'][4242] = True
    psutil_falso['lanza'] = psutil.AccessDenied(4242)

    assert main._check_single_instance() is True


@pytest.mark.parametrize('nombre', ['python.exe', 'pythonw.exe', 'Python3.13.exe'])
def test_reconoce_las_variantes_del_ejecutable_de_python(tr_dirs, psutil_falso, nombre):
    main.LOCK_FILE.write_text('4242')
    psutil_falso['vivos'][4242] = True
    psutil_falso['nombres'][4242] = nombre

    assert main._check_single_instance() is False


def test_un_ejecutable_empaquetado_NO_seria_reconocido(tr_dirs, psutil_falso):
    """LIMITACION CONOCIDA: la comprobacion es `'python' in proc.name()`. Si
    algun dia la app se congela con PyInstaller y corre como
    noted.exe, el lock deja de proteger. Queda fijado para que quien
    haga ese cambio se encuentre este test."""
    main.LOCK_FILE.write_text('4242')
    psutil_falso['vivos'][4242] = True
    psutil_falso['nombres'][4242] = 'noted.exe'

    assert main._check_single_instance() is True, \
        "si esto cambia, la deteccion ya no depende del nombre 'python'"
