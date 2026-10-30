"""Quien reinicia Noted no debe poder llevarselo por delante al cerrarse.

INCIDENTE QUE ORIGINO ESTOS TESTS (28/10/2026)
El 27/10 el hook post-merge reinicio Noted desde una sesion de Claude Code.
Start-TRDaemon lanzaba el watchdog con Start-Process, asi que el watchdog, y el
daemon que lanza, eran hijos de esa sesion y heredaban su job de Windows. El
28/10 a las 09:12 la app de Claude se colgo, Windows la cerro, y Noted cayo con
ella sin dejar una linea en el log: 9 minutos sin grabar. Medido despues: un
proceso lanzado con Start-Process desde la sesion queda DENTRO del job de
Claude; uno abierto por el Explorador queda fuera.

Ahora Start-TRDaemon pide al Explorador que abra scripts/start_watchdog.vbs,
como el arranque al iniciar sesion, y el watchdog es hijo del shell, no de
quien reinicia. Si no se puede (sin explorer en la sesion, sin VBScript, .vbs
asociado a otra cosa, o el watchdog no aparece) arranca como siempre: un Noted
atado a quien lo reinicio es mejor que ninguno.

Como en test_restart_guard.py, a una copia de tr_env.ps1 se le anaden al final
dobles de todo lo que lanza, espera o mira procesos: el codigo real corre
entero y cada efecto queda anotado en calls.log en vez de ocurrir.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_PWSH = shutil.which('powershell') or shutil.which('pwsh')


def integracion(test):
    return pytest.mark.integration(
        pytest.mark.skipif(not _PWSH, reason='no hay PowerShell en esta maquina')(test))


# -- Comprobaciones baratas, sin PowerShell: corren en CI y en pre-commit ------

def _cuerpo(texto: str, funcion: str) -> str:
    inicio = texto.find(f'function {funcion} ')
    assert inicio >= 0, f"no se encontro {funcion}"
    fin = texto.find('\nfunction ', inicio + 1)
    return texto[inicio:fin if fin > 0 else None]


@pytest.mark.unit
def test_start_trdaemon_pide_el_watchdog_al_shell_antes_de_lanzarlo_el():
    cuerpo = _cuerpo((REPO / 'tr_env.ps1').read_text(encoding='utf-8-sig'), 'Start-TRDaemon')
    shell = cuerpo.find('Start-TRWatchdogViaShell')
    propio = cuerpo.find("Start-Process -FilePath 'powershell.exe'")
    assert shell >= 0, "Start-TRDaemon ya no pide el watchdog al shell"
    assert propio > shell, "Start-TRDaemon lanza el watchdog como hijo antes de pedirlo al shell"


@pytest.mark.unit
def test_instalar_noted_arranca_la_app_por_la_misma_ruta():
    """/install-noted corre en una sesion de Claude: si arrancase el watchdog
    con su propio Start-Process, la primera instalacion ya naceria atada a ella."""
    texto = (REPO / '.claude' / 'skills' / 'install-noted' / 'SKILL.md').read_text(encoding='utf-8')
    assert 'Start-TRDaemon' in texto
    assert not re.search(r'Start-Process[^\n]*watchdog\.ps1', texto), (
        "install-noted lanza watchdog.ps1 por su cuenta en vez de con Start-TRDaemon")


# -- Start-TRDaemon de verdad, con dobles -------------------------------------

def _dobles(raiz: Path, log: Path) -> str:
    anota = f"Add-Content -LiteralPath '{log}' -Encoding UTF8 -Value"
    arriba = raiz / 'watchdog_up'
    return f"""

# ---- Dobles de test, anadidos por tests/test_detached_launch.py ----
function Get-TRWatchdogProcess {{
    param($Root)
    if (Test-Path -LiteralPath '{arriba}') {{ @([pscustomobject]@{{ ProcessId = 1 }}) }} else {{ @() }}
}}
function Get-TRDaemonProcess {{
    param($Root)
    if (Test-Path -LiteralPath '{arriba}') {{ @([pscustomobject]@{{ ProcessId = 2 }}) }} else {{ @() }}
}}
function Test-TRShellLaunch {{ Test-Path -LiteralPath '{raiz / 'shell_ok'}' }}
function Start-Sleep {{ param($Seconds) {anota} "Start-Sleep $Seconds" }}
function Start-Process {{
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, $ErrorAction, [switch]$PassThru)
    {anota} ("Start-Process $FilePath | " + ($ArgumentList -join ' '))
    $arranca = if ($FilePath -like '*explorer.exe') {{ '{raiz / 'shell_arranca'}' }} else {{ $null }}
    if (-not $arranca -or (Test-Path -LiteralPath $arranca)) {{
        New-Item -ItemType File -Path '{arriba}' -Force | Out-Null
    }}
}}
"""


@pytest.fixture
def noted(tmp_path):
    return _instalacion(tmp_path, 'noted')


def _instalacion(tmp_path: Path, nombre: str):
    raiz = tmp_path / nombre
    (raiz / 'scripts').mkdir(parents=True)
    log = raiz / 'calls.log'
    shutil.copy(REPO / 'watchdog.ps1', raiz)
    shutil.copy(REPO / 'scripts' / 'start_watchdog.vbs', raiz / 'scripts')
    tr_env = (REPO / 'tr_env.ps1').read_text(encoding='utf-8-sig')
    (raiz / 'tr_env.ps1').write_text(tr_env + _dobles(raiz, log), encoding='utf-8-sig')

    class Noted:
        root = raiz

        def shell(self, disponible=True, arranca=True):
            for nombre, si in (('shell_ok', disponible), ('shell_arranca', arranca)):
                f = raiz / nombre
                if si:
                    f.touch()
                else:
                    f.unlink(missing_ok=True)

        def arrancar(self, shell_seconds=3):
            comando = (f". '{raiz / 'tr_env.ps1'}'; "
                       f"Start-TRDaemon -Root '{raiz}' -TimeoutSeconds 1 -ShellSeconds {shell_seconds}")
            r = subprocess.run([_PWSH, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', comando],
                               capture_output=True, text=True, timeout=120)
            assert r.returncode == 0, r.stdout + r.stderr
            return r.stdout.strip().splitlines()[-1] == 'True'

        def llamadas(self):
            return log.read_text(encoding='utf-8-sig').splitlines() if log.exists() else []

        def lanzamientos(self):
            return [c.removeprefix('Start-Process ').split(' | ', 1)
                    for c in self.llamadas() if c.startswith('Start-Process')]

    return Noted()


@integracion
def test_con_el_shell_disponible_el_watchdog_lo_abre_el_explorador(noted):
    """LA COMPROBACION QUE HABRIA EVITADO EL INCIDENTE: el watchdog no se lanza
    como hijo de quien reinicia, sino a traves del Explorador."""
    noted.shell()

    assert noted.arrancar()

    [(programa, argumentos)] = noted.lanzamientos()
    assert programa.lower().endswith('\\explorer.exe'), noted.llamadas()
    assert argumentos == f'"{noted.root / "scripts" / "start_watchdog.vbs"}"'


@integracion
def test_sin_shell_arranca_como_siempre(noted):
    noted.shell(disponible=False)

    assert noted.arrancar()

    [(programa, argumentos)] = noted.lanzamientos()
    assert programa == 'powershell.exe'
    assert str(noted.root / 'watchdog.ps1') in argumentos


@integracion
def test_si_el_watchdog_no_aparece_tras_pedirlo_al_shell_arranca_como_siempre(noted):
    """El Explorador no devuelve nada: si VBScript esta bloqueado, nadie avisa.
    Se espera un rato y, si no hay watchdog, mejor uno atado que ninguno."""
    noted.shell(arranca=False)

    assert noted.arrancar(shell_seconds=3)

    (explorador, _), (siempre, _) = noted.lanzamientos()
    assert explorador.lower().endswith('\\explorer.exe')
    assert siempre == 'powershell.exe'
    esperas = [c for c in noted.llamadas() if c.startswith('Start-Sleep')]
    assert len(esperas) >= 3, "no espero al watchdog antes de lanzarlo otra vez"


@integracion
def test_una_instalacion_sin_el_vbs_arranca_como_siempre(noted):
    (noted.root / 'scripts' / 'start_watchdog.vbs').unlink()
    noted.shell()

    assert noted.arrancar()

    [(programa, _)] = noted.lanzamientos()
    assert programa == 'powershell.exe'


@integracion
@pytest.mark.parametrize('nombre', ['no~ted', 'pct%USERNAME%x'])
def test_una_ruta_que_el_explorador_reescribe_va_por_el_camino_de_siempre(tmp_path, nombre):
    """Con %VAR% el Explorador no abre el .vbs; con un nombre 8.3 wscript pasa
    la ruta larga y Get-TRWatchdogProcess no reconoce el watchdog, que se
    lanzaria dos veces."""
    noted = _instalacion(tmp_path, nombre)
    noted.shell()

    assert noted.arrancar()

    [(programa, _)] = noted.lanzamientos()
    assert programa == 'powershell.exe'


@integracion
def test_con_el_watchdog_vivo_no_lanza_nada(noted):
    noted.shell()
    (noted.root / 'watchdog_up').touch()

    assert noted.arrancar()

    assert noted.lanzamientos() == []


# -- Sin dobles: el Explorador y el .vbs de verdad ----------------------------
#
# Los tests de arriba sustituyen Test-TRShellLaunch. Si la de verdad devolviese
# siempre $false (una propiedad mal escrita vale $null en PS 5.1, sin error),
# todos pasarian y cada reinicio volveria a atar Noted a quien lo lanza: el
# incidente otra vez, en silencio. Estos dos lo impiden en una maquina con
# escritorio, y se saltan donde no hay shell (CI).

_JOB = r'''
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class TJob {
    [DllImport("user32.dll")] public static extern IntPtr GetShellWindow();
    [DllImport("kernel32.dll")] static extern bool QueryInformationJobObject(IntPtr j, int c, IntPtr i, int l, IntPtr r);
    public static long[] Pids() {
        int n = 8 + 8 * 8192; IntPtr p = Marshal.AllocHGlobal(n);
        try { if (!QueryInformationJobObject(IntPtr.Zero, 3, p, n, IntPtr.Zero)) return null;
              int c = Marshal.ReadInt32(p, 4); long[] r = new long[c];
              for (int i = 0; i < c; i++) r[i] = Marshal.ReadIntPtr(p, 8 + 8 * i).ToInt64();
              return r; }
        finally { Marshal.FreeHGlobal(p); }
    }
}
"@
if ([TJob]::GetShellWindow() -eq [IntPtr]::Zero) { 'SIN_SHELL'; exit 0 }
'''


def _powershell(script: Path) -> list[str]:
    r = subprocess.run([_PWSH, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    salida = r.stdout.split()
    if 'SIN_SHELL' in salida:
        pytest.skip('sin escritorio: no hay shell al que pedirle nada')
    return salida


@integracion
def test_en_una_maquina_con_escritorio_el_shell_esta_disponible(tmp_path):
    script = tmp_path / 'shell.ps1'
    script.write_text(_JOB + f". '{REPO / 'tr_env.ps1'}'\nTest-TRShellLaunch\n", encoding='utf-8-sig')

    assert _powershell(script)[-1] == 'True', (
        "Test-TRShellLaunch dice que no hay shell en una maquina con escritorio: cada reinicio "
        "lanzaria el watchdog como hijo de quien reinicia")


@integracion
def test_el_explorador_lanza_el_watchdog_fuera_del_job_de_quien_lo_pide(tmp_path):
    """De punta a punta, con un watchdog falso que solo espera: el Explorador
    abre el .vbs real, Get-TRWatchdogProcess lo ve, y no esta en el job de
    quien lo pidio. El control, uno lanzado con Start-Process, si lo esta: sin
    el, una medida que nunca ve nada dentro pasaria igual."""
    raiz = tmp_path / 'noted'
    (raiz / 'scripts').mkdir(parents=True)
    shutil.copy(REPO / 'scripts' / 'start_watchdog.vbs', raiz / 'scripts')
    (raiz / 'watchdog.ps1').write_text('Start-Sleep 20\n', encoding='utf-8')
    script = tmp_path / 'e2e.ps1'
    script.write_text(_JOB + f"""
. '{REPO / 'tr_env.ps1'}'
if (-not (Start-TRWatchdogViaShell -Root '{raiz}')) {{ 'NO_PEDIDO'; exit 0 }}
for ($i = 0; $i -lt 45 -and @(Get-TRWatchdogProcess -Root '{raiz}').Count -eq 0; $i++) {{ Start-Sleep 1 }}
$wd = @(Get-TRWatchdogProcess -Root '{raiz}')
if ($wd.Count -eq 0) {{ 'NO_APARECE'; exit 0 }}
$control = Start-Process -FilePath powershell.exe -ArgumentList '-NoProfile', '-Command', 'Start-Sleep 20' -WindowStyle Hidden -PassThru
$pids = [TJob]::Pids()
if ($null -eq $pids) {{ 'SIN_JOB' }} else {{
    'watchdog=' + ($pids -contains [long]$wd[0].ProcessId)
    'control=' + ($pids -contains [long]$control.Id)
}}
""", encoding='utf-8-sig')

    salida = _powershell(script)
    print(salida)

    assert 'NO_PEDIDO' not in salida, "Start-TRWatchdogViaShell no le pidio nada al Explorador"
    assert 'NO_APARECE' not in salida, "el Explorador no arranco el watchdog en 45 s"
    if 'SIN_JOB' in salida:
        return    # quien corre el test no esta en ningun job: no hay nada que heredar
    assert 'control=True' in salida, f"la medida no ve dentro ni lo lanzado con Start-Process: {salida}"
    assert 'watchdog=False' in salida, f"el watchdog quedo en el job de quien lo pidio: {salida}"


# -- El watchdog: si dos arrancan a la vez, se queda uno ----------------------
#
# Con el arranque a traves del shell aparece una carrera nueva: el watchdog
# que se pidio al Explorador llega tarde, justo cuando Start-TRDaemon ya lanzo
# el de siempre. Con "sal si hay otro", cada uno veia al otro y salian los dos.
# Ahora se queda el mas antiguo.

def _sonda_watchdog(tmp_path: Path, otros: list[tuple[str, str]]) -> str:
    """Corre watchdog.ps1 de verdad hasta justo antes de su bucle, con un
    tr_env.ps1 falso que no escribe nada fuera de tmp_path."""
    (tmp_path / 'main.py').touch()
    procesos = ', '.join(
        f"[pscustomobject]@{{ ProcessId = {pid}; CreationDate = [datetime]'{fecha}' }}" for pid, fecha in otros)
    (tmp_path / 'tr_env.ps1').write_text(f"""
function Save-TRRoot {{ param($Path) }}
function Get-TRPython {{ param($Root) [pscustomobject]@{{ Python = 'py'; Pythonw = 'pyw'; Source = 'test' }} }}
function Get-TRWatchdogProcess {{
    param($Root)
    @([pscustomobject]@{{ ProcessId = $PID; CreationDate = [datetime]'2026-10-28 10:00:00' }}, {procesos})
}}
""", encoding='utf-8-sig')
    texto = (REPO / 'watchdog.ps1').read_text(encoding='utf-8-sig')
    corte = texto.find('$python = $pyEnv.Pythonw')
    assert corte > 0, "no se encontro donde cortar el watchdog"
    salida = tmp_path / 'sigue.txt'
    salida.unlink(missing_ok=True)
    sonda = tmp_path / 'sonda.ps1'
    sonda.write_text(texto[:corte] + f"\n'sigue' | Out-File -LiteralPath '{salida}'\n", encoding='utf-8-sig')
    r = subprocess.run([_PWSH, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(sonda)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    return 'sigue' if salida.exists() else 'sale'


@integracion
def test_un_watchdog_sale_si_hay_otro_mas_antiguo(tmp_path):
    assert _sonda_watchdog(tmp_path, [('99999', '2026-10-28 09:59:00')]) == 'sale'


@integracion
def test_un_watchdog_sigue_si_el_otro_es_mas_nuevo(tmp_path):
    """EL CASO QUE ANTES FALLABA: el mas nuevo sale, y este tiene que quedarse.
    Con "sal si hay otro" salian los dos y Noted no arrancaba."""
    assert _sonda_watchdog(tmp_path, [('99999', '2026-10-28 10:00:05')]) == 'sigue'


@integracion
def test_con_la_misma_hora_se_queda_el_pid_mas_bajo(tmp_path):
    assert _sonda_watchdog(tmp_path, [('1', '2026-10-28 10:00:00')]) == 'sale'
    assert _sonda_watchdog(tmp_path, [('4294967295', '2026-10-28 10:00:00')]) == 'sigue'
