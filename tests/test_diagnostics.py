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
import pytest

import diagnostics


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
