"""El diagnostico, y sobre todo su capacidad de ARREGLAR cosas.

POR QUE ESTE MODULO MERECE TESTS MAS QUE OTROS
----------------------------------------------
`diagnostics.py` no solo mira: actua. Mata el grabador, arranca el vigilante y
quita el mute de dispositivos reales. Y lo hace desde un boton que pulsa un
usuario que no sabe nada de .lock ni de watchdogs.

EL INVARIANTE QUE PROTEGEN ESTOS TESTS
--------------------------------------
El grabador ES la app: el proceso que sirve la ventana desde la que el usuario
pulsa "Reiniciar Noted". Si se le mata sin que haya un vigilante vivo que lo
levante, el usuario se queda sin Noted Y sin la ventana desde la que pedirlo
otra vez. Solo le quedaria la bandeja del sistema o un .ps1, que es justo lo
que no sabe que existe.

Por eso `restart_app()` garantiza primero quien resucite y solo despues mata.
`test_restart_app_no_mata_si_no_consigue_vigilante` es el test que impide que
alguien "simplifique" ese orden.

Todos son `unit`: ni COM, ni dispositivos, ni subprocess, ni procesos reales.
Lo que toca el sistema de verdad (`_endpoint`, `_start_watchdog`, `_procs`)
esta sustituido en cada test.
"""
import sys
import types

import numpy as np
import pytest

import diagnostics

REAL_ACTIVE_JOBS = diagnostics._active_jobs


@pytest.fixture(autouse=True)
def puerta_libre(monkeypatch):
    """La puerta de reinicio (Get-TRActiveJobs) es un PowerShell de verdad: aqui
    esta libre. Los tests que la prueban la sustituyen por la suya."""
    monkeypatch.setattr(diagnostics, '_active_jobs', lambda: 0)


class FakeProc:
    """Proceso de mentira con la forma que usa diagnostics: .pid, .info,
    .ppid() y .kill()."""

    def __init__(self, pid, name='python.exe', cmdline=(), create_time=0.0, ppid=0):
        self.pid = pid
        self.info = {'name': name, 'cmdline': list(cmdline), 'create_time': create_time}
        self._ppid = ppid
        self.killed = False

    def ppid(self):
        return self._ppid

    def kill(self):
        self.killed = True


class FakeTimer:
    """Sustituto de threading.Timer que NO ejecuta nada.

    Permite comprobar que la muerte queda AGENDADA y no ejecutada, que es la
    diferencia entre responder al navegador y morir a media llamada.
    """

    creados = []

    def __init__(self, delay, fn):
        self.delay, self.fn, self.started = delay, fn, False
        FakeTimer.creados.append(self)

    def start(self):
        self.started = True


@pytest.fixture
def sin_timers(monkeypatch):
    FakeTimer.creados = []
    monkeypatch.setattr(diagnostics.threading, 'Timer', FakeTimer)
    return FakeTimer


@pytest.fixture
def audio_sano(monkeypatch):
    """Micro y salida presentes, sin mute y a buen volumen."""
    monkeypatch.setattr(diagnostics, '_endpoint',
                        lambda kind: {'found': True, 'muted': False, 'volume': 80})


@pytest.fixture
def todo_parado(monkeypatch):
    """Ni grabador ni vigilante corriendo."""
    monkeypatch.setattr(diagnostics, '_procs', lambda needle, names: [])


# ── Filtrado de procesos ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_procs_solo_coge_los_procesos_de_esta_instalacion(monkeypatch, tr_dirs):
    """Filtrar solo por nombre ("todo lo que se llame python") mataria el
    Python de otros proyectos del usuario. El filtro es la RUTA."""
    mio = FakeProc(1, 'python.exe', [r'python', str(tr_dirs / 'main.py')])
    ajeno = FakeProc(2, 'python.exe', [r'python', r'C:\otro\proyecto\main.py'])
    otra_cosa = FakeProc(3, 'notepad.exe', [r'notepad', str(tr_dirs / 'main.py')])

    monkeypatch.setattr(diagnostics.psutil, 'process_iter',
                        lambda attrs=None: [mio, ajeno, otra_cosa])

    encontrados = diagnostics._procs(str(tr_dirs / 'main.py'), ('python',))
    assert [p.pid for p in encontrados] == [1]


@pytest.mark.unit
def test_procs_ignora_los_procesos_que_desaparecen(monkeypatch, tr_dirs):
    """process_iter devuelve procesos que pueden morir mientras se recorren."""
    class Volatil(FakeProc):
        @property
        def info(self):
            raise diagnostics.psutil.NoSuchProcess(99)

        @info.setter
        def info(self, _v):
            pass

    vivo = FakeProc(1, 'python.exe', ['python', str(tr_dirs / 'main.py')])
    monkeypatch.setattr(diagnostics.psutil, 'process_iter',
                        lambda attrs=None: [Volatil(99), vivo])

    assert [p.pid for p in diagnostics._procs(str(tr_dirs / 'main.py'), ('python',))] == [1]


# ── Formato ───────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize('segundos, esperado', [
    (5, '5s'),
    (90, '1m'),
    (3600, '1h 0m'),
    (3600 * 25, '1d 1h'),
])
def test_fmt_uptime(segundos, esperado):
    assert diagnostics._fmt_uptime(segundos) == esperado


# ── Estado ────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_collect_marca_fallo_si_no_hay_microfono(monkeypatch, todo_parado):
    """Sin microfono no se grabaria la voz del usuario: es FALLO, no aviso."""
    monkeypatch.setattr(diagnostics, '_endpoint', lambda kind: {'found': False})

    estado = diagnostics.collect()
    mic = next(r for r in estado['rows'] if r['id'] == 'mic')

    assert mic['sev'] == diagnostics.SEV_BAD
    assert estado['verdict']['title_key'] == 'dg_v_bad_title'


@pytest.mark.unit
def test_collect_ofrece_quitar_el_mute_si_esta_silenciado(monkeypatch, todo_parado):
    monkeypatch.setattr(diagnostics, '_endpoint',
                        lambda kind: {'found': True, 'muted': True, 'volume': 70})

    mic = next(r for r in diagnostics.collect()['rows'] if r['id'] == 'mic')

    assert mic['sev'] == diagnostics.SEV_BAD
    assert mic['fix'] == 'unmute_mic'


@pytest.mark.unit
def test_collect_avisa_si_el_volumen_esta_por_los_suelos(monkeypatch, todo_parado):
    """Un volumen al 5% graba, pero graba algo inservible: aviso, no fallo."""
    monkeypatch.setattr(diagnostics, '_endpoint',
                        lambda kind: {'found': True, 'muted': False, 'volume': 5})

    mic = next(r for r in diagnostics.collect()['rows'] if r['id'] == 'mic')

    assert mic['sev'] == diagnostics.SEV_WARN
    assert mic['state_args'] == [5]


@pytest.mark.unit
def test_collect_ofrece_arrancar_el_vigilante_si_esta_parado(monkeypatch, audio_sano, tr_dirs):
    """Sin vigilante la app no se levanta sola: es el caso que motivo el panel."""
    def solo_grabador(needle, names):
        return [FakeProc(1, create_time=0.0)] if 'main.py' in needle else []

    monkeypatch.setattr(diagnostics, '_procs', solo_grabador)

    wd = next(r for r in diagnostics.collect()['rows'] if r['id'] == 'watchdog')

    assert wd['sev'] == diagnostics.SEV_WARN
    assert wd['fix'] == 'start_watchdog'


@pytest.mark.unit
def test_collect_nunca_ofrece_un_arreglo_que_mate_la_app(monkeypatch, todo_parado):
    """El grabador es esta misma app. Un boton que lo reinicie desde dentro
    cerraria la ventana desde la que se pulsa, asi que no debe ofrecerse."""
    monkeypatch.setattr(diagnostics, '_endpoint',
                        lambda kind: {'found': True, 'muted': True, 'volume': 70})

    acciones = {r['fix'] for r in diagnostics.collect()['rows'] if r.get('fix')}

    assert acciones
    assert not {a for a in acciones if 'restart' in a or 'stop' in a or 'kill' in a}


# ── Grabacion en curso ────────────────────────────────────────────────────────

@pytest.mark.unit
def test_is_recording_detecta_el_sentinel_con_pid_vivo(monkeypatch, tr_dirs):
    (tr_dirs / '.recording_active').write_text('4321', encoding='utf-8')
    monkeypatch.setattr(diagnostics.psutil, 'pid_exists', lambda pid: pid == 4321)

    assert diagnostics._is_recording() is True


@pytest.mark.unit
def test_is_recording_ignora_un_sentinel_huerfano(monkeypatch, tr_dirs):
    """Un sentinel que quedo de un proceso muerto no debe bloquear para siempre
    los arreglos: seria imposible reiniciar nunca mas."""
    (tr_dirs / '.recording_active').write_text('4321', encoding='utf-8')
    monkeypatch.setattr(diagnostics.psutil, 'pid_exists', lambda pid: False)

    assert diagnostics._is_recording() is False


@pytest.mark.unit
def test_is_recording_mira_tambien_el_pipeline(tr_dirs):
    (tr_dirs / '.pipeline_status.json').write_text(
        '{"jobs": [{"stage": "recording"}]}', encoding='utf-8')

    assert diagnostics._is_recording() is True


# ── Arreglos ──────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_apply_fix_rechaza_una_accion_desconocida():
    """La accion llega desde el navegador: no puede despacharse a ciegas."""
    assert diagnostics.apply_fix('rm -rf')['ok'] is False
    assert diagnostics.apply_fix('_kill_daemon')['ok'] is False


@pytest.mark.unit
def test_arrancar_el_grabador_pasa_por_el_vigilante(monkeypatch):
    """Lanzar main.py por nuestra cuenta crearia una segunda instancia peleando
    por el .lock con la que el vigilante acabe levantando (ver tr_env.ps1)."""
    llamadas = []
    monkeypatch.setattr(diagnostics, '_start_watchdog',
                        lambda: llamadas.append('watchdog') or {'ok': True})

    assert diagnostics.apply_fix('start_daemon')['ok'] is True
    assert llamadas == ['watchdog']


# ── Reinicio: el invariante de seguridad ──────────────────────────────────────

@pytest.mark.unit
def test_restart_app_no_mata_si_no_consigue_vigilante(monkeypatch, sin_timers):
    """EL TEST IMPORTANTE DE ESTE FICHERO.

    Sin vigilante, matar al grabador deja al usuario sin app y sin la ventana
    desde la que pulso el boton. Ante la duda, NO se mata nada.
    """
    monkeypatch.setattr(diagnostics, '_is_recording', lambda: False)
    monkeypatch.setattr(diagnostics, '_watchdog_running', lambda: False)
    monkeypatch.setattr(diagnostics, '_start_watchdog',
                        lambda: {'ok': False, 'error': 'simulado'})

    res = diagnostics.restart_app()

    assert res['ok'] is False
    assert res['reason'] == 'no_watchdog'
    assert sin_timers.creados == [], 'se agendo una muerte sin quien resucite'


@pytest.mark.unit
def test_restart_app_no_mata_si_el_vigilante_no_llega_a_aparecer(monkeypatch, sin_timers):
    """_start_watchdog puede devolver ok (el Popen no fallo) y aun asi el
    vigilante morir al arrancar. Se comprueba que esta, no que se pidio."""
    monkeypatch.setattr(diagnostics, '_is_recording', lambda: False)
    monkeypatch.setattr(diagnostics, '_watchdog_running', lambda: False)
    monkeypatch.setattr(diagnostics, '_start_watchdog', lambda: {'ok': True})
    monkeypatch.setattr(diagnostics.time, 'sleep', lambda _s: None)

    res = diagnostics.restart_app()

    assert res['ok'] is False
    assert res['reason'] == 'no_watchdog'
    assert sin_timers.creados == []


@pytest.mark.unit
def test_restart_app_se_niega_si_hay_grabacion_en_curso(monkeypatch, sin_timers):
    """Reiniciar en mitad de una reunion pierde la grabacion."""
    monkeypatch.setattr(diagnostics, '_is_recording', lambda: True)

    res = diagnostics.restart_app()

    assert res == {'ok': False, 'reason': 'recording'}
    assert sin_timers.creados == []


@pytest.mark.unit
@pytest.mark.parametrize('trabajos, motivo', [(1, 'busy'), (None, 'unknown')],
                         ids=['procesando', 'no_se_sabe'])
def test_restart_app_se_niega_si_noted_esta_trabajando(monkeypatch, sin_timers, trabajos, motivo):
    """Grabar no es lo unico que un reinicio corta. El 13/10/2026 uno a mitad
    del procesado perdio el coaching y las acciones de una reunion. Si no se
    puede saber, tampoco se reinicia."""
    monkeypatch.setattr(diagnostics, '_is_recording', lambda: False)
    monkeypatch.setattr(diagnostics, '_active_jobs', lambda: trabajos)
    monkeypatch.setattr(diagnostics, '_watchdog_running', lambda: True)

    assert diagnostics.restart_app() == {'ok': False, 'reason': motivo}
    assert sin_timers.creados == []


@pytest.mark.unit
def test_los_trabajos_los_cuenta_la_puerta_compartida(monkeypatch):
    """Una sola puerta para todas las rutas de reinicio (tr_env.ps1): con dos
    copias, el 19/10/2026 se arreglo una y la otra siguio cortando procesados."""
    vistos = []

    def tr_env(script, timeout):
        vistos.append(script)
        return types.SimpleNamespace(stdout='2\r\n')

    monkeypatch.setattr(diagnostics, '_tr_env', tr_env)
    assert REAL_ACTIVE_JOBS() == 2
    assert 'Get-TRActiveJobs' in vistos[0]


@pytest.mark.unit
def test_si_la_puerta_no_responde_no_se_sabe(monkeypatch):
    def revienta(*_a, **_k):
        raise OSError('powershell bloqueado')

    monkeypatch.setattr(diagnostics, '_tr_env', revienta)
    assert REAL_ACTIVE_JOBS() is None


@pytest.mark.unit
def test_restart_app_responde_antes_de_matar(monkeypatch, sin_timers):
    """La muerte va agendada, no inmediata: si no, el proceso moriria a mitad
    de la llamada y la UI mostraria un error en un reinicio que salio bien."""
    monkeypatch.setattr(diagnostics, '_is_recording', lambda: False)
    monkeypatch.setattr(diagnostics, '_watchdog_running', lambda: True)

    res = diagnostics.restart_app()

    assert res['ok'] is True
    assert len(sin_timers.creados) == 1
    agendado = sin_timers.creados[0]
    assert agendado.fn is diagnostics._kill_daemon
    assert agendado.delay > 0, 'matar sin retardo corta la respuesta al navegador'
    assert agendado.started is True


@pytest.mark.unit
def test_kill_daemon_mata_los_hijos_antes_que_los_padres(monkeypatch):
    """Si muere el padre primero, el hijo queda huerfano con el .lock tomado y
    el vigilante no puede volver a arrancar: la app no vuelve."""
    padre = FakeProc(100, ppid=1)
    hijo = FakeProc(200, ppid=100)
    orden = []

    for p in (padre, hijo):
        p.kill = (lambda proc: lambda: orden.append(proc.pid))(p)

    monkeypatch.setattr(diagnostics, '_procs', lambda needle, names: [padre, hijo])

    diagnostics._kill_daemon()

    assert orden == [200, 100]


@pytest.mark.unit
def test_kill_daemon_no_explota_si_no_hay_nada_que_matar(monkeypatch, todo_parado):
    diagnostics._kill_daemon()      # no debe lanzar


# ── El COM de audio ───────────────────────────────────────────────────────────
#
# Los imports de pycaw/comtypes estan DENTRO de las funciones, asi que basta
# con dejar modulos de mentira en sys.modules antes de llamarlas. Sin esto
# estos tests hablarian con el audio real de la maquina, que es exactamente lo
# que la marca `unit` prohibe.

class VolumenFalso:
    def __init__(self, mute=0, escalar=0.42):
        self.mute, self.escalar = mute, escalar
        self.set_mute_calls = []

    def GetMute(self):
        return self.mute

    def GetMasterVolumeLevelScalar(self):
        return self.escalar

    def SetMute(self, valor, ctx):
        self.set_mute_calls.append((valor, ctx))


@pytest.fixture
def pycaw_falso(monkeypatch):
    volumen = VolumenFalso()
    pedidos = []

    class Iface:
        def QueryInterface(self, _cls):
            return volumen

    class Dispositivo:
        def Activate(self, _iid, _ctx, _params):
            return Iface()

    class Enumerador:
        def GetDefaultAudioEndpoint(self, flow, role):
            pedidos.append((flow, role))
            return Dispositivo()

    class AudioUtilities:
        @staticmethod
        def GetDeviceEnumerator():
            return Enumerador()

    comtypes = types.ModuleType('comtypes')
    comtypes.CLSCTX_ALL = 23
    paquete = types.ModuleType('pycaw')
    modulo = types.ModuleType('pycaw.pycaw')
    modulo.AudioUtilities = AudioUtilities
    modulo.IAudioEndpointVolume = type('IAudioEndpointVolume', (), {'_iid_': 'iid'})
    paquete.pycaw = modulo

    monkeypatch.setitem(sys.modules, 'comtypes', comtypes)
    monkeypatch.setitem(sys.modules, 'pycaw', paquete)
    monkeypatch.setitem(sys.modules, 'pycaw.pycaw', modulo)

    return types.SimpleNamespace(volumen=volumen, pedidos=pedidos)


@pytest.mark.unit
def test_endpoint_pide_captura_para_el_micro_y_render_para_la_salida(pycaw_falso):
    """El flujo no es cosmetico: pedir el flujo equivocado devuelve el
    dispositivo equivocado y la fila entera miente."""
    diagnostics._endpoint('mic')
    diagnostics._endpoint('spk')

    flujos = [flow for flow, _role in pycaw_falso.pedidos]
    assert flujos == [1, 0]          # 1 = captura, 0 = render


@pytest.mark.unit
def test_endpoint_convierte_el_escalar_a_porcentaje(pycaw_falso):
    pycaw_falso.volumen.escalar = 0.9
    assert diagnostics._endpoint('spk') == {'found': True, 'muted': False, 'volume': 90}


@pytest.mark.unit
def test_endpoint_reporta_el_mute(pycaw_falso):
    pycaw_falso.volumen.mute = 1
    assert diagnostics._endpoint('mic')['muted'] is True


@pytest.mark.unit
def test_endpoint_no_propaga_los_fallos_de_com(monkeypatch):
    """COM falla de formas creativas. El panel debe decir "no hay" y seguir,
    nunca tumbar la ventana entera."""
    def revienta(_kind):
        raise OSError('CoInitialize no llamado')

    monkeypatch.setattr(diagnostics, '_endpoint_volume', revienta)
    assert diagnostics._endpoint('mic') == {'found': False}


@pytest.mark.unit
def test_endpoint_sin_dispositivo_devuelve_no_encontrado(monkeypatch):
    monkeypatch.setattr(diagnostics, '_endpoint_volume', lambda _k: None)
    assert diagnostics._endpoint('spk') == {'found': False}


@pytest.mark.unit
def test_quitar_el_mute_actua_sobre_el_dispositivo(pycaw_falso):
    assert diagnostics.apply_fix('unmute_mic') == {'ok': True}
    assert pycaw_falso.volumen.set_mute_calls == [(0, None)]


@pytest.mark.unit
def test_quitar_el_mute_avisa_si_no_hay_dispositivo(monkeypatch):
    monkeypatch.setattr(diagnostics, '_endpoint_volume', lambda _k: None)
    res = diagnostics.apply_fix('unmute_spk')
    assert res['ok'] is False


# ── Arranque del vigilante ────────────────────────────────────────────────────

@pytest.mark.unit
def test_start_watchdog_pasa_por_start_trdaemon(monkeypatch, tr_dirs):
    """Con un Popen propio, el vigilante seria hijo de este panel y moriria con
    el. Start-TRDaemon se lo pide al Explorador (tr_env.ps1, 28/10/2026)."""
    (tr_dirs / 'watchdog.ps1').write_text('# de mentira', encoding='utf-8')
    vistos = []
    monkeypatch.setattr(diagnostics, '_tr_env', lambda script, timeout: vistos.append(script))
    monkeypatch.setattr(diagnostics, '_watchdog_running', lambda: True)
    monkeypatch.setattr(diagnostics.subprocess, 'Popen',
                        lambda *a, **k: pytest.fail('el vigilante no se lanza con un Popen propio'))

    assert diagnostics._start_watchdog() == {'ok': True}
    assert 'Start-TRDaemon' in vistos[0] and str(tr_dirs) in vistos[0]


@pytest.mark.unit
def test_tr_env_corre_oculto_y_con_tr_env_cargado(monkeypatch, tr_dirs):
    """Sin ventana oculta le aparece al usuario una consola negra en mitad de
    la pantalla cada vez que pulsa el boton."""
    llamadas = []
    monkeypatch.setattr(diagnostics.subprocess, 'run',
                        lambda cmd, **kw: llamadas.append((cmd, kw)) or types.SimpleNamespace(stdout=''))

    diagnostics._tr_env('Get-TRActiveJobs', timeout=5)

    cmd, kw = llamadas[0]
    assert cmd[0] == 'powershell' and 'Hidden' in cmd
    assert str(tr_dirs / 'tr_env.ps1') in cmd[-1] and cmd[-1].endswith('Get-TRActiveJobs')
    assert kw['cwd'] == str(tr_dirs) and kw['timeout'] == 5


@pytest.mark.unit
def test_start_watchdog_avisa_si_falta_el_script(tr_dirs):
    """Una instalacion a medias no debe acabar en un Popen que falla raro."""
    res = diagnostics._start_watchdog()
    assert res['ok'] is False
    assert 'watchdog.ps1' in res['error']


@pytest.mark.unit
def test_start_watchdog_captura_el_fallo_al_lanzar(monkeypatch, tr_dirs):
    (tr_dirs / 'watchdog.ps1').write_text('# de mentira', encoding='utf-8')

    def revienta(*_a, **_k):
        raise OSError('politica de ejecucion')

    monkeypatch.setattr(diagnostics, '_tr_env', revienta)

    res = diagnostics._start_watchdog()
    assert res['ok'] is False
    assert 'politica de ejecucion' in res['error']


# ── Captura loopback ──────────────────────────────────────────────────────────

def _modulo_pyaudio(dispositivos, abre=True, hay_wasapi=True):
    """Modulo pyaudiowpatch de mentira con la forma que usa _Loopback."""
    mod = types.ModuleType('pyaudiowpatch')
    mod.paWASAPI, mod.paFloat32, mod.paContinue = 13, 1, 0
    mod.ultima = None

    class Stream:
        def __init__(self):
            self.arrancado = self.parado = self.cerrado = False

        def start_stream(self):
            self.arrancado = True

        def stop_stream(self):
            self.parado = True

        def close(self):
            self.cerrado = True

    class PyAudio:
        def __init__(self):
            self.terminado = False
            self.callback = None
            self.stream = None
            self.abiertos = []
            mod.ultima = self

        def get_host_api_info_by_type(self, _t):
            if not hay_wasapi:
                raise OSError('sin WASAPI')
            return {'defaultOutputDevice': 0}

        def get_device_info_by_index(self, i):
            return dispositivos[i]

        def get_device_count(self):
            return len(dispositivos)

        def open(self, **kw):
            self.abiertos.append(kw['input_device_index'])
            if not abre:
                raise OSError('dispositivo ocupado')
            self.callback = kw['stream_callback']
            self.stream = Stream()
            return self.stream

        def terminate(self):
            self.terminado = True

    mod.PyAudio = PyAudio
    return mod


def _dispositivo(nombre, loopback, canales=2, indice=0):
    return {'name': nombre, 'isLoopbackDevice': loopback, 'maxInputChannels': canales,
            'defaultSampleRate': 48000, 'index': indice}


@pytest.mark.unit
def test_loopback_mide_el_pico_de_lo_que_suena(monkeypatch):
    """El pico es la prueba de que SALE sonido: es lo que distingue "configurado
    bien" de "se grabaria de verdad"."""
    mod = _modulo_pyaudio([_dispositivo('Altavoces (loopback)', True)])
    monkeypatch.setitem(sys.modules, 'pyaudiowpatch', mod)

    loop = diagnostics._Loopback()
    assert loop.start() is True

    muestras = np.array([0.0, 0.31, -0.5, 0.2], dtype=np.float32)
    mod.ultima.callback(muestras.tobytes(), len(muestras), None, None)

    assert loop.stop() == pytest.approx(0.5)
    assert mod.ultima.terminado is True


@pytest.mark.unit
def test_loopback_busca_otro_dispositivo_si_la_salida_no_es_loopback(monkeypatch):
    """La salida por defecto casi nunca es ya un loopback: hay que encontrar
    su gemelo, y por eso el worker del repo hace la misma busqueda."""
    dispositivos = [
        _dispositivo('Jabra EVOLVE 20', False, indice=0),
        _dispositivo('Otra cosa (loopback)', True, indice=1),
        _dispositivo('Jabra EVOLVE 20 (loopback)', True, indice=2),
    ]
    mod = _modulo_pyaudio(dispositivos)
    monkeypatch.setitem(sys.modules, 'pyaudiowpatch', mod)

    assert diagnostics._Loopback().start() is True
    # El gemelo del dispositivo activo va PRIMERO, antes que cualquier otro.
    assert mod.ultima.abiertos[0] == 2


@pytest.mark.unit
def test_loopback_se_rinde_sin_romper_si_no_hay_wasapi(monkeypatch):
    mod = _modulo_pyaudio([], hay_wasapi=False)
    monkeypatch.setitem(sys.modules, 'pyaudiowpatch', mod)

    loop = diagnostics._Loopback()
    assert loop.start() is False
    assert loop.ok is False


@pytest.mark.unit
def test_loopback_se_rinde_si_ningun_dispositivo_abre(monkeypatch):
    mod = _modulo_pyaudio([_dispositivo('Altavoces (loopback)', True)], abre=False)
    monkeypatch.setitem(sys.modules, 'pyaudiowpatch', mod)

    assert diagnostics._Loopback().start() is False
    assert mod.ultima.terminado is True


@pytest.mark.unit
def test_loopback_sin_la_libreria_no_rompe_el_panel(monkeypatch):
    """pyaudiowpatch puede no estar: el diagnostico debe seguir dando el resto."""
    monkeypatch.setitem(sys.modules, 'pyaudiowpatch', None)
    assert diagnostics._Loopback().start() is False


# ── La prueba activa de audio ─────────────────────────────────────────────────

class LoopbackFalso:
    def __init__(self, pico=0.4, ok=True):
        self.peak, self.ok = pico, ok
        self.parado = False

    def start(self):
        return self.ok

    def stop(self):
        self.parado = True
        return self.peak


@pytest.fixture
def sounddevice_falso(monkeypatch):
    grabado = {'datos': np.array([[0.0], [0.25], [-0.6]], dtype=np.float32)}

    mod = types.ModuleType('sounddevice')
    mod.llamadas = []

    def playrec(datos, samplerate, channels, dtype):
        mod.llamadas.append((len(datos), samplerate, channels, dtype))
        if isinstance(grabado['datos'], Exception):
            raise grabado['datos']
        return grabado['datos']

    mod.playrec = playrec
    mod.wait = lambda: None
    mod.stop = lambda: None

    monkeypatch.setitem(sys.modules, 'sounddevice', mod)
    return types.SimpleNamespace(mod=mod, grabado=grabado)


@pytest.mark.unit
def test_la_prueba_mide_los_dos_caminos_que_usa_noted(monkeypatch, sounddevice_falso):
    """Micro Y loopback: una reunion necesita los dos, y que uno vaya no dice
    nada del otro."""
    monkeypatch.setattr(diagnostics, '_is_recording', lambda: False)
    monkeypatch.setattr(diagnostics, '_Loopback', lambda: LoopbackFalso(pico=0.4))

    res = diagnostics.run_audio_test(seconds=0.1)

    assert res['ok'] is True
    assert res['mic_peak'] == pytest.approx(0.6)
    assert res['loop_peak'] == pytest.approx(0.4)


@pytest.mark.unit
def test_la_prueba_distingue_no_poder_grabar_de_grabar_silencio(monkeypatch, sounddevice_falso):
    """-1 (no se pudo abrir) y 0 (se abrio y no entro nada) son diagnosticos
    distintos: el primero es una averia, el segundo puede ser una sala callada."""
    monkeypatch.setattr(diagnostics, '_is_recording', lambda: False)
    monkeypatch.setattr(diagnostics, '_Loopback', lambda: LoopbackFalso(ok=False))
    sounddevice_falso.grabado['datos'] = OSError('micro ocupado')

    res = diagnostics.run_audio_test(seconds=0.1)

    assert res['mic_peak'] == -1.0
    assert res['loop_peak'] == -1.0


@pytest.mark.unit
def test_la_prueba_no_abre_el_micro_en_mitad_de_una_reunion(monkeypatch):
    """Abrir el micro mientras se graba es justo lo que no se puede hacer."""
    monkeypatch.setattr(diagnostics, '_is_recording', lambda: True)
    llamadas = []
    monkeypatch.setattr(diagnostics, '_Loopback',
                        lambda: llamadas.append('arrancado') or LoopbackFalso())

    assert diagnostics.run_audio_test() == {'ok': False, 'reason': 'recording'}
    assert llamadas == []


@pytest.mark.unit
def test_la_prueba_avisa_si_faltan_las_librerias(monkeypatch):
    monkeypatch.setattr(diagnostics, '_is_recording', lambda: False)
    monkeypatch.setitem(sys.modules, 'sounddevice', None)

    res = diagnostics.run_audio_test()
    assert res['ok'] is False
    assert res['reason'] == 'deps'


# ── El puente con la UI ───────────────────────────────────────────────────────
#
# Los cuatro metodos de AppAPI son finos a proposito, pero tienen un trabajo
# que no es trivial: NO dejar escapar una excepcion. Si una sube, cruza el
# puente de pywebview y la promesa de JavaScript se queda colgada para siempre,
# con el panel en blanco y sin ningun error visible.

@pytest.mark.unit
@pytest.mark.parametrize('metodo, funcion, args', [
    ('get_diagnostics', 'collect', ()),
    ('run_audio_test', 'run_audio_test', ()),
    ('apply_diagnostic_fix', 'apply_fix', ('start_watchdog',)),
    ('restart_app', 'restart_app', ()),
])
def test_la_api_nunca_deja_escapar_una_excepcion(monkeypatch, metodo, funcion, args):
    import app_window

    def revienta(*_a, **_k):
        raise RuntimeError('COM se murio')

    monkeypatch.setattr(diagnostics, funcion, revienta)

    res = getattr(app_window.AppAPI(), metodo)(*args)

    assert res['ok'] is False
    assert 'COM se murio' in res['error']


@pytest.mark.unit
def test_la_api_devuelve_el_estado_con_ok(monkeypatch):
    import app_window

    monkeypatch.setattr(diagnostics, 'collect',
                        lambda: {'rows': [], 'verdict': {'sev': 0}, 'recording': False})

    res = app_window.AppAPI().get_diagnostics()

    assert res['ok'] is True
    assert res['verdict'] == {'sev': 0}


@pytest.mark.unit
def test_la_api_pasa_la_accion_tal_cual(monkeypatch):
    """Si la API reinterpretara la accion, el rechazo de acciones desconocidas
    de apply_fix dejaria de valer para nada."""
    import app_window

    recibidas = []
    monkeypatch.setattr(diagnostics, 'apply_fix',
                        lambda a: recibidas.append(a) or {'ok': True})

    app_window.AppAPI().apply_diagnostic_fix('unmute_mic')

    assert recibidas == ['unmute_mic']


@pytest.mark.unit
def test_la_api_reenvia_el_motivo_del_rechazo(monkeypatch):
    """La UI distingue "hay grabacion" de "no hay vigilante" para decir cosas
    distintas: si se pierde el motivo, los dos quedan en un error generico."""
    import app_window

    monkeypatch.setattr(diagnostics, 'restart_app',
                        lambda: {'ok': False, 'reason': 'no_watchdog'})

    assert app_window.AppAPI().restart_app()['reason'] == 'no_watchdog'


@pytest.mark.unit
def test_start_watchdog_dice_si_el_vigilante_no_arranco(monkeypatch, tr_dirs):
    """PowerShell puede fallar sin que el comando lance: lo que cuenta es que
    el vigilante este, no que se pidiera."""
    (tr_dirs / 'watchdog.ps1').write_text('# de mentira', encoding='utf-8')
    monkeypatch.setattr(diagnostics, '_tr_env', lambda script, timeout: None)
    monkeypatch.setattr(diagnostics, '_watchdog_running', lambda: False)
    res = diagnostics._start_watchdog()
    assert res['ok'] is False and 'vigilante' in res['error']


@pytest.mark.unit
@pytest.mark.parametrize('grabando, trabajos', [(True, 0), (False, 1), (False, None)],
                         ids=['grabando', 'procesando', 'no_se_sabe'])
def test_kill_daemon_vuelve_a_mirar_la_puerta(monkeypatch, grabando, trabajos):
    """Entre la puerta de restart_app y la muerte pueden pasar mas de 30 s
    (arrancar el vigilante): una reunion que empezara en ese rato moriria."""
    proc = FakeProc(10, cmdline=['pythonw', 'main.py'])
    monkeypatch.setattr(diagnostics, '_procs', lambda *_a: [proc])
    monkeypatch.setattr(diagnostics, '_is_recording', lambda: grabando)
    monkeypatch.setattr(diagnostics, '_active_jobs', lambda: trabajos)
    diagnostics._kill_daemon()
    assert proc.killed is False


@pytest.mark.unit
@pytest.mark.parametrize('ruta, esperado', [
    ("C:\\Users\\ana\\Noted", "'C:\\Users\\ana\\Noted'"),
    ("C:\\Users\\O'Brien", "'C:\\Users\\O''Brien'"),
    ("C:\\Users\\O\u2019Brien", "'C:\\Users\\O\u2019\u2019Brien'"),
])
def test_ps_quote_duplica_todas_las_comillas_simples(ruta, esperado):
    """PowerShell toma como comilla simple tambien las tipograficas: sin
    duplicarlas, una ruta con una cerraba la cadena y la puerta no respondia."""
    assert diagnostics._ps_quote(ruta) == esperado
