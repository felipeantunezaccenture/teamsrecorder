"""La puerta que impide reiniciar Noted con trabajo a medias.

INCIDENTES QUE ORIGINARON ESTOS TESTS
- 13/10/2026: un reinicio con la grabacion ya terminada corto el pipeline 39 s
  despues de guardar las minutas de una reunion de cliente. Se perdieron sus
  notas de coaching y sus acciones. 377ee27 arreglo scripts/restart_noted.ps1.
- 19/10/2026: el ff-merge de dc6c1f3 disparo hooks/post-merge, que reinicia
  Noted por la OTRA ruta, restart_after_update.ps1. Esa solo miraba
  `stage == 'recording'`: con una reunion procesandose o en cola la habria
  cortado igual. Ese dia no habia trabajos y no se perdio nada.

Dos rutas de reinicio con dos copias de la puerta: se arreglo una y la otra
no. Ahora la puerta vive una sola vez, Get-TRActiveJobs en tr_env.ps1, y estos
tests vigilan que todo reinicio pase por ella.

COMO SE PRUEBA SIN TOCAR NADA REAL
Se monta una instalacion falsa en tmp_path con una copia del script, y a la
copia de tr_env.ps1 se le anaden al final dobles de todo lo que para, arranca,
espera o avisa. La ultima definicion de una funcion gana, y PowerShell
resuelve una funcion antes que un cmdlet (Start-Sleep), asi que el script real
corre entero pero cada efecto queda anotado en calls.log en vez de ocurrir. Es
el mismo mecanismo que Mock en Pester. Aunque la puerta fallase, el test no
podria parar ningun proceso.

"Daemon vivo" es un .lock con el PID de un Python vivo, lo mismo que mira
main.py: se usa el PID del propio pytest.

scripts/restart_noted.ps1 NO se ejecuta aqui a proposito: si su puerta fallase
mataria cualquier Python con una ventana titulada "Noted", que en esta maquina
es el panel real de la app. Para el basta la comprobacion de texto.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_PWSH = shutil.which('powershell') or shutil.which('pwsh')

# PID 4 es siempre el proceso System de Windows: vivo y no es Python. Hace de
# PID reciclado, que main.py tampoco cuenta como instancia.
_PID_AJENO = 4

GRABANDO = {'stage': 'recording', 'label': 'Recording 12:03', 'elapsed': 723, 'pct': None,
            'title': 'Cliente', 'time': '10:12', 'step': 0, 'total_steps': 3,
            'step_label': 'Grabando'}
PROCESANDO = {'stage': 'processing', 'label': 'Generando minutas...', 'pct': None,
              'title': 'Cliente', 'time': '10:12', 'step': 2, 'total_steps': 3,
              'step_label': 'Generando minutas'}
EN_COLA = {'stage': 'queued', 'label': '2026-10-13_10-12_Cliente', 'pct': 0,
           'stem': '2026-10-13_10-12_Cliente'}

_PARADAS = ('Stop-TRWatchdog', 'Stop-TRDaemon', 'Start-TRDaemon')


# -- Comprobacion barata, sin PowerShell: corre en CI y en pre-commit --------

@pytest.mark.unit
@pytest.mark.parametrize('script', ['restart_after_update.ps1', 'scripts/restart_noted.ps1'])
def test_todo_reinicio_consulta_la_puerta_antes_de_parar_nada(script):
    """LA COMPROBACION QUE HABRIA EVITADO EL SEGUNDO INCIDENTE: el fallo no fue
    una puerta mal escrita sino una ruta de reinicio que tenia la suya propia.
    Si alguien anade otra, o vuelve a copiar la logica, esto salta."""
    texto = (REPO / script).read_text(encoding='utf-8-sig')
    puerta = texto.find('Get-TRActiveJobs')
    assert puerta >= 0, (
        f"{script} no consulta Get-TRActiveJobs (tr_env.ps1): decide por su cuenta "
        "si se puede reiniciar y acabara divergiendo, como ya paso")
    paradas = [m.start() for m in re.finditer(r'Stop-(?:Process|TRDaemon|TRWatchdog)\b', texto)]
    assert paradas, f"{script} ya no para nada: revisa si este test sigue teniendo sentido"
    assert puerta < min(paradas), f"{script} para algo ANTES de consultar la puerta"


@pytest.mark.unit
def test_update_noted_consulta_la_puerta_antes_del_pull():
    """El hook post-merge solo puede no reiniciar: cuando corre, el merge ya ha
    cambiado los .py en disco bajo un daemon que sigue importando modulos a
    mitad del pipeline. La puerta buena va ANTES del pull."""
    texto = (REPO / '.claude' / 'commands' / 'update-noted.md').read_text(encoding='utf-8')
    puerta = texto.find('Get-TRActiveJobs')
    assert puerta >= 0, "/update-noted no consulta Get-TRActiveJobs antes de actualizar"
    assert puerta < texto.find('git pull'), "/update-noted hace el pull antes de mirar la puerta"


@pytest.mark.unit
def test_restart_noted_busca_python_con_get_trpython():
    """Sin .venv cae al Python global (9b4e15a), pero por Get-TRPython:
    Get-Command python puede devolver el alias de WindowsApps, que abre la
    Store en vez de arrancar la app."""
    texto = (REPO / 'scripts' / 'restart_noted.ps1').read_text(encoding='utf-8-sig')
    assert 'Get-TRPython' in texto, "restart_noted.ps1 busca Python por su cuenta"
    assert 'Get-Command python' not in texto, "restart_noted.ps1 puede acabar en el alias de WindowsApps"


# -- El hook de verdad, con PowerShell ----------------------------------------

def _dobles(raiz: Path, log: Path) -> str:
    anota = f"Add-Content -LiteralPath '{log}' -Encoding UTF8 -Value"
    despues = raiz / '.pipeline_status.after_sleep.json'
    estado = raiz / '.pipeline_status.json'
    return f"""

# ---- Dobles de test, anadidos por tests/test_restart_guard.py ----
function Stop-TRWatchdog {{ param($Root) {anota} 'Stop-TRWatchdog'; 0 }}
function Stop-TRDaemon {{ param($Root) {anota} 'Stop-TRDaemon'; 0 }}
function Update-TRDependencies {{ param($Root) {anota} 'Update-TRDependencies'; $true }}
function Start-TRDaemon {{ param($Root, $TimeoutSeconds) {anota} 'Start-TRDaemon'; $true }}
function Show-TRNotification {{ param($Text, $Title) {anota} "Show-TRNotification: $Text" }}
function Start-Sleep {{
    param($Seconds)
    {anota} "Start-Sleep $Seconds"
    # Lo que el daemon escribiria mientras el script espera.
    if (Test-Path -LiteralPath '{despues}') {{
        Move-Item -LiteralPath '{despues}' -Destination '{estado}' -Force
    }}
}}
"""


@pytest.fixture
def noted(tmp_path):
    """Instalacion falsa. Devuelve un objeto para preparar el estado y lanzar
    el hook."""
    raiz = tmp_path / 'noted'
    raiz.mkdir()
    log = raiz / 'calls.log'
    shutil.copy(REPO / 'restart_after_update.ps1', raiz)
    tr_env = (REPO / 'tr_env.ps1').read_text(encoding='utf-8-sig')
    (raiz / 'tr_env.ps1').write_text(tr_env + _dobles(raiz, log), encoding='utf-8-sig')

    class Noted:
        def daemon_vivo(self, pid=None):
            (raiz / '.lock').write_text(str(pid or os.getpid()))

        def centinela(self, pid=None):
            (raiz / '.recording_active').write_text(str(pid or os.getpid()))

        def estado(self, *jobs, crudo=None, tras_la_espera=False):
            nombre = '.pipeline_status.after_sleep.json' if tras_la_espera else '.pipeline_status.json'
            texto = crudo if crudo is not None else json.dumps({'jobs': list(jobs)})
            (raiz / nombre).write_text(texto, encoding='utf-8')

        def hook(self):
            r = subprocess.run(
                [_PWSH, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                 '-File', str(raiz / 'restart_after_update.ps1')],
                capture_output=True, text=True, timeout=120)
            llamadas = log.read_text(encoding='utf-8-sig').splitlines() if log.exists() else []
            return r, llamadas

    return Noted()


def _reinicio(llamadas):
    return [c for c in llamadas if c in _PARADAS]


def _avisos(llamadas):
    return [c for c in llamadas if c.startswith('Show-TRNotification')]


_con_powershell = pytest.mark.skipif(not _PWSH, reason='no hay PowerShell en esta maquina')


@pytest.mark.parametrize('job, palabra', [
    (PROCESANDO, 'procesando'),
    (EN_COLA, 'procesando'),
    (GRABANDO, 'grabacion'),
], ids=['procesando', 'en_cola', 'grabando'])
@pytest.mark.integration
@_con_powershell
def test_con_trabajo_en_curso_el_hook_avisa_y_no_reinicia(noted, job, palabra):
    """EL INCIDENTE: con una reunion procesandose, un git pull la cortaba."""
    noted.daemon_vivo()
    noted.estado(job)

    r, llamadas = noted.hook()

    assert not _reinicio(llamadas), (
        f"con un trabajo '{job['stage']}' el hook reinicio igual: {llamadas}\n{r.stdout}{r.stderr}")
    avisos = _avisos(llamadas)
    assert len(avisos) == 1 and palabra in avisos[0].lower(), (
        f"el aviso no dice que se esta {palabra!r}: {avisos}")
    assert job['label'] in r.stdout, "la salida no dice que trabajo esta en marcha"
    assert r.returncode == 0, "el hook nunca debe fallar: el merge ya ocurrio"


@pytest.mark.parametrize('lock_pid', [None, _PID_AJENO], ids=['sin_lock', 'pid_reciclado'])
@pytest.mark.integration
@_con_powershell
def test_con_el_daemon_muerto_los_trabajos_residuales_no_bloquean(noted, lock_pid):
    """Si el daemon murio a mitad, el fichero se queda con trabajos que ya nadie
    va a terminar. Bloquear ahi dejaria la app sin arrancar."""
    if lock_pid:
        noted.daemon_vivo(lock_pid)
    noted.estado(PROCESANDO)

    r, llamadas = noted.hook()

    assert _reinicio(llamadas) == list(_PARADAS), f"{llamadas}\n{r.stdout}{r.stderr}"
    assert not _avisos(llamadas)


@pytest.mark.parametrize('crudo', ['{"jobs": []}', ''], ids=['vacio', 'a_medio_escribir'])
@pytest.mark.integration
@_con_powershell
def test_en_reposo_reinicia_tras_una_segunda_lectura(noted, crudo):
    """Entre sacar un trabajo de la cola y marcarlo como procesando, la lista
    sale vacia un instante: una sola lectura no basta. Y un fichero que el
    daemon esta reescribiendo (truncado) no puede inventarse un trabajo."""
    noted.daemon_vivo()
    noted.estado(crudo=crudo)

    r, llamadas = noted.hook()

    assert 'Start-Sleep 3' in llamadas, f"no hubo segunda lectura: {llamadas}"
    assert _reinicio(llamadas) == list(_PARADAS), f"{llamadas}\n{r.stdout}{r.stderr}"


@pytest.mark.integration
@_con_powershell
def test_un_trabajo_que_aparece_durante_la_espera_bloquea(noted):
    noted.daemon_vivo()
    noted.estado()
    noted.estado(PROCESANDO, tras_la_espera=True)

    r, llamadas = noted.hook()

    assert not _reinicio(llamadas), f"{llamadas}\n{r.stdout}{r.stderr}"
    assert _avisos(llamadas)


@pytest.mark.parametrize('dueno, bloquea', [('daemon', True), ('ajeno', False)])
@pytest.mark.integration
@_con_powershell
def test_guardando_el_wav_bloquea_aunque_el_estado_este_vacio(noted, dueno, bloquea):
    """Al colgar, set_recording(False) escribe la lista vacia y el WAV se mezcla
    y guarda despues, en otro hilo, antes de entrar en la cola: segundos en una
    reunion larga en los que .pipeline_status.json no dice nada. Solo lo cubre
    .recording_active. Uno que no es del daemon actual (murio grabando) no
    cuenta."""
    noted.daemon_vivo()
    noted.centinela(os.getpid() if dueno == 'daemon' else _PID_AJENO)
    noted.estado()

    r, llamadas = noted.hook()

    if bloquea:
        assert not _reinicio(llamadas), f"{llamadas}\n{r.stdout}{r.stderr}"
        assert 'grabacion' in _avisos(llamadas)[0].lower()
    else:
        assert _reinicio(llamadas) == list(_PARADAS), f"{llamadas}\n{r.stdout}{r.stderr}"
