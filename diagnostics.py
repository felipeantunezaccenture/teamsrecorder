"""Diagnostico: se puede grabar una reunion ahora mismo?

Responde a la misma pregunta que scripts/noted_monitor.ps1, pero desde dentro
de la app. Los dos coexisten a proposito y no se solapan:

  - El .ps1 es para cuando Noted NO arranca. Por eso no depende de Python.
  - Este modulo alimenta el boton de Settings, que solo puede pulsarse con la
    app viva, asi que puede permitirse usar las librerias del proyecto.

La prueba activa no se limita a mirar la configuracion: reproduce un tono y
graba a la vez por los DOS caminos que usa Noted (microfono con sounddevice y
loopback WASAPI con pyaudiowpatch). Mirar volumenes solo dice como esta
configurado el equipo; grabar es lo unico que demuestra que la reunion se
grabaria de verdad.

Los textos no se traducen aqui: se devuelven claves i18n y sus parametros, y
los traduce la UI web con su propio diccionario (web/app.js).
"""
import logging
import os
import subprocess
import threading
import time

import psutil

from config import PROJECT_DIR

log = logging.getLogger(__name__)

SEV_OK = 0
SEV_WARN = 1
SEV_BAD = 2

UPTIME_WARN_HOURS = 72
VOL_WARN_PERCENT = 20

# Por debajo de esto se considera que no salio/entro senal en la prueba.
SILENCE_FLOOR = 0.005
# Nivel a partir del cual se da por bueno que se capto la voz.
MIC_GOOD = 0.02


def _procs(needle: str, name_parts: tuple) -> list:
    """Procesos de ESTA instalacion que contienen `needle` en su linea de
    comandos. Filtrar solo por nombre de proceso cogeria el Python de otros
    proyectos del usuario."""
    found = []
    needle = needle.lower()
    for p in psutil.process_iter(['name', 'cmdline', 'create_time']):
        try:
            nm = (p.info['name'] or '').lower()
            if not any(part in nm for part in name_parts):
                continue
            cmd = ' '.join(p.info['cmdline'] or []).lower()
            if needle in cmd:
                found.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def _fmt_uptime(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return f'{int(seconds)}s'
    if minutes < 60:
        return f'{minutes}m'
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f'{hours}h {minutes}m'
    days, hours = divmod(hours, 24)
    return f'{days}d {hours}h'


def _endpoint_volume(kind: str):
    """IAudioEndpointVolume del dispositivo por defecto, o None.

    Se va al enumerador en vez de usar GetMicrophone()/GetSpeakers(): en esta
    version de pycaw no son simetricos (uno devuelve el IMMDevice crudo y el
    otro un envoltorio AudioDevice sin .Activate), y el envoltorio hacia
    fallar siempre la fila de la salida.
    """
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    flow = 1 if kind == 'mic' else 0           # 0 = render, 1 = capture
    enumerator = AudioUtilities.GetDeviceEnumerator()
    if enumerator is None:
        return None
    dev = enumerator.GetDefaultAudioEndpoint(flow, 1)   # 1 = eMultimedia
    if dev is None:
        return None
    iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return iface.QueryInterface(IAudioEndpointVolume)


def _endpoint(kind: str) -> dict:
    """Mute y volumen del dispositivo por defecto. kind: 'mic' | 'spk'."""
    try:
        vol = _endpoint_volume(kind)
        if vol is None:
            return {'found': False}
        return {
            'found': True,
            'muted': bool(vol.GetMute()),
            'volume': int(round(vol.GetMasterVolumeLevelScalar() * 100)),
        }
    except Exception as e:
        log.debug('No se pudo leer el endpoint %s: %s', kind, e)
        return {'found': False}


def _audio_row(row_id: str, kind: str, missing_key: str, idle_key: str) -> tuple:
    """Devuelve (row, sev) para microfono o salida."""
    info = _endpoint(kind)

    if not info.get('found'):
        return ({'id': row_id, 'sev': SEV_BAD, 'meter': True,
                 'state_key': 'dg_st_none', 'detail_key': missing_key, 'detail_args': []},
                SEV_BAD)

    if info['muted']:
        return ({'id': row_id, 'sev': SEV_BAD, 'meter': True,
                 'state_key': 'dg_st_muted', 'detail_key': 'dg_d_muted', 'detail_args': [],
                 'fix': f'unmute_{row_id}', 'fix_key': 'dg_fix_unmute'},
                SEV_BAD)

    vol = info['volume']
    if vol <= VOL_WARN_PERCENT:
        return ({'id': row_id, 'sev': SEV_WARN, 'meter': True,
                 'state_key': 'dg_st_volume', 'state_args': [vol],
                 'detail_key': 'dg_d_vol_low', 'detail_args': []},
                SEV_WARN)

    return ({'id': row_id, 'sev': SEV_OK, 'meter': True,
             'state_key': 'dg_st_ready',
             'detail_key': idle_key, 'detail_args': [vol]},
            SEV_OK)


def collect() -> dict:
    """Estado actual, sin tocar nada. Barato: se puede llamar en bucle."""
    rows = []
    problems = SEV_OK
    now = time.time()

    # ── Grabador ──
    # Nota: este codigo corre DENTRO del daemon, asi que aqui siempre estara
    # vivo. La fila se mantiene porque lo que importa es cuanto lleva: un
    # proceso de dias acumula fugas y conviene reiniciarlo antes de una
    # reunion importante.
    daemon = _procs(str(PROJECT_DIR / 'main.py'), ('python',))
    if not daemon:
        rows.append({'id': 'recorder', 'sev': SEV_BAD, 'meter': False,
                     'state_key': 'dg_st_stopped', 'detail_key': 'dg_d_daemon_down',
                     'detail_args': [], 'fix': 'start_daemon', 'fix_key': 'dg_fix_start'})
        problems = SEV_BAD
    else:
        try:
            up = now - min(p.info['create_time'] for p in daemon)
        except Exception:
            up = 0
        if up >= UPTIME_WARN_HOURS * 3600:
            rows.append({'id': 'recorder', 'sev': SEV_WARN, 'meter': False,
                         'state_key': 'dg_st_old', 'detail_key': 'dg_d_uptime_old',
                         'detail_args': [_fmt_uptime(up)]})
            problems = max(problems, SEV_WARN)
        else:
            rows.append({'id': 'recorder', 'sev': SEV_OK, 'meter': False,
                         'state_key': 'dg_st_rec_ok', 'detail_key': 'dg_d_uptime',
                         'detail_args': [_fmt_uptime(up)]})

    # ── Vigilante ──
    watchdog = _procs(str(PROJECT_DIR / 'watchdog.ps1'), ('powershell', 'pwsh'))
    if not watchdog:
        rows.append({'id': 'watchdog', 'sev': SEV_WARN, 'meter': False,
                     'state_key': 'dg_st_stopped', 'detail_key': 'dg_d_wd_down',
                     'detail_args': [], 'fix': 'start_watchdog', 'fix_key': 'dg_fix_start'})
        problems = max(problems, SEV_WARN)
    else:
        try:
            wup = now - min(p.info['create_time'] for p in watchdog)
        except Exception:
            wup = 0
        rows.append({'id': 'watchdog', 'sev': SEV_OK, 'meter': False,
                     'state_key': 'dg_st_watching', 'detail_key': 'dg_d_wd_ok',
                     'detail_args': [_fmt_uptime(wup)]})

    # ── Audio ──
    mic_row, mic_sev = _audio_row('mic', 'mic', 'dg_d_mic_missing', 'dg_d_mic_idle')
    spk_row, spk_sev = _audio_row('spk', 'spk', 'dg_d_spk_missing', 'dg_d_spk_idle')
    rows.append(mic_row)
    rows.append(spk_row)
    problems = max(problems, mic_sev, spk_sev)

    if _is_recording():
        verdict = {'sev': 3, 'title_key': 'dg_v_rec_title', 'hint_key': 'dg_v_rec_hint'}
    elif problems >= SEV_BAD:
        verdict = {'sev': SEV_BAD, 'title_key': 'dg_v_bad_title', 'hint_key': 'dg_v_bad_hint'}
    elif problems == SEV_WARN:
        verdict = {'sev': SEV_WARN, 'title_key': 'dg_v_warn_title', 'hint_key': 'dg_v_warn_hint'}
    else:
        verdict = {'sev': SEV_OK, 'title_key': 'dg_v_ok_title', 'hint_key': 'dg_v_ok_hint'}

    return {'rows': rows, 'verdict': verdict, 'problems': problems,
            'recording': verdict['sev'] == 3}


def _is_recording() -> bool:
    """True si hay una grabacion en curso. Nunca hagas la prueba de audio si
    lo devuelve: abrir el micro en mitad de una reunion la estropearia."""
    import json

    status = PROJECT_DIR / '.pipeline_status.json'
    if status.exists():
        try:
            data = json.loads(status.read_text(encoding='utf-8'))
            if any(j.get('stage') == 'recording' for j in data.get('jobs', [])):
                return True
        except Exception:
            pass

    sentinel = PROJECT_DIR / '.recording_active'
    if sentinel.exists():
        try:
            pid = int(sentinel.read_text(encoding='utf-8').strip())
            return psutil.pid_exists(pid)
        except Exception:
            return False
    return False


def apply_fix(action: str) -> dict:
    """Ejecuta el arreglo que ofrece una fila. Devuelve {'ok': bool, ...}.

    Solo se exponen arreglos seguros y reversibles. Reiniciar el grabador no
    esta aqui a proposito: el grabador ES esta misma app, y matarlo cerraria
    la ventana desde la que el usuario acaba de pulsar el boton.
    """
    if action in ('start_daemon', 'start_watchdog'):
        return _start_watchdog()
    if action in ('unmute_mic', 'unmute_spk'):
        return _set_mute('mic' if action.endswith('mic') else 'spk', False)
    return {'ok': False, 'error': f'accion desconocida: {action}'}


def _ps_quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _tr_env(script: str, timeout: float) -> subprocess.CompletedProcess:
    """`script` en un PowerShell con tr_env.ps1 cargado. La puerta de reinicio
    (Get-TRActiveJobs) y el arranque (Start-TRDaemon) viven alli una sola vez,
    para el hook post-merge, restart_noted y /update-noted: copiarlos aqui es
    como divergieron las dos rutas de reinicio el 19/10/2026."""
    cmd = f". {_ps_quote(PROJECT_DIR / 'tr_env.ps1')}; {script}"
    return subprocess.run(
        ['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
         '-WindowStyle', 'Hidden', '-Command', cmd],
        capture_output=True, text=True, timeout=timeout, cwd=str(PROJECT_DIR),
        creationflags=0x08000000 if os.name == 'nt' else 0,   # CREATE_NO_WINDOW
    )


def _active_jobs() -> int | None:
    """Trabajos en curso segun Get-TRActiveJobs: grabando, guardando, procesando
    o en cola. None si no se pudo saber, que para reiniciar cuenta como ocupado."""
    try:
        r = _tr_env(f"@(Get-TRActiveJobs -Root {_ps_quote(PROJECT_DIR)}).Count", timeout=60)
        return int(r.stdout.strip().splitlines()[-1])
    except Exception as e:
        log.warning(f"diagnostico: no se pudo consultar si Noted esta trabajando: {e}")
        return None


def _start_watchdog() -> dict:
    """Arranca el vigilante, que a su vez levanta el grabador si hace falta.

    Arrancar main.py por nuestra cuenta crearia una segunda instancia peleando
    por el .lock con la que el vigilante acabaria levantando (ver tr_env.ps1).
    Si el grabador ya corre, el vigilante ve el .lock valido y espera pasivo.

    Con Start-TRDaemon y no con un Popen propio: lanzado desde aqui, el
    vigilante seria hijo de este panel, y Start-TRDaemon se lo pide al
    Explorador para que no dependa de quien lo arranca (Start-TRWatchdogViaShell).
    """
    script = PROJECT_DIR / 'watchdog.ps1'
    if not script.exists():
        return {'ok': False, 'error': 'watchdog.ps1 no encontrado'}
    try:
        _tr_env(f"Start-TRDaemon -Root {_ps_quote(PROJECT_DIR)} -TimeoutSeconds 2 | Out-Null",
                timeout=120)
        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _watchdog_running() -> bool:
    return bool(_procs(str(PROJECT_DIR / 'watchdog.ps1'), ('powershell', 'pwsh')))


def restart_app() -> dict:
    """Reinicia Noted desde la propia app.

    NO mata el grabador a secas. El grabador es esta misma app: si no hubiera
    un vigilante vivo que lo resucite, el usuario se quedaria sin Noted y sin
    la ventana desde la que acaba de pulsar el boton, sin forma de volver.
    Asi que primero se garantiza quien lo levante y solo despues se tira.

    La muerte se agenda en un hilo aparte para poder responder al navegador
    antes de que el proceso desaparezca; si no, la llamada moriria a medias y
    la UI mostraria un error en un reinicio que en realidad fue bien.
    """
    if _is_recording():
        return {'ok': False, 'reason': 'recording'}
    # Grabar no es lo unico que un reinicio corta: tambien el procesado y la
    # cola. El 13/10/2026 un reinicio 39 s despues de las minutas perdio el
    # coaching y las acciones de una reunion. La puerta es la compartida.
    jobs = _active_jobs()
    if jobs is None or jobs > 0:
        return {'ok': False, 'reason': 'busy'}

    if not _watchdog_running():
        started = _start_watchdog()
        if not started.get('ok'):
            return {'ok': False, 'reason': 'no_watchdog', 'error': started.get('error')}
        # Darle tiempo a engancharse antes de tirar el grabador.
        for _ in range(10):
            time.sleep(0.5)
            if _watchdog_running():
                break
        else:
            return {'ok': False, 'reason': 'no_watchdog'}

    threading.Timer(1.0, _kill_daemon).start()
    return {'ok': True}


def _kill_daemon() -> None:
    """Mata el grabador. Los hijos primero: si muere el padre antes, deja
    huerfanos con el .lock tomado y el vigilante no puede volver a arrancar."""
    procs = _procs(str(PROJECT_DIR / 'main.py'), ('python',))
    if not procs:
        return

    pids = {p.pid for p in procs}
    children, parents = [], []
    for p in procs:
        try:
            (children if p.ppid() in pids else parents).append(p)
        except Exception:
            parents.append(p)

    for p in children + parents:
        try:
            p.kill()
        except Exception:
            pass


def _set_mute(kind: str, muted: bool) -> dict:
    try:
        vol = _endpoint_volume(kind)
        if vol is None:
            return {'ok': False, 'error': f'no hay dispositivo {kind}'}
        vol.SetMute(1 if muted else 0, None)
        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


class _Loopback:
    """Captura WASAPI loopback corta, solo para medir el pico.

    Repite la seleccion de dispositivo de wasapi_loopback_worker.py en vez de
    importarla: aquel es un script-subproceso con su propio protocolo por
    stdout y no esta pensado para llamarse en proceso.
    """

    def __init__(self):
        self._pa = None
        self._stream = None
        self.peak = 0.0
        self.ok = False

    def start(self) -> bool:
        try:
            import numpy as np
            import pyaudiowpatch as pyaudio
        except ImportError as e:
            log.debug('loopback no disponible: %s', e)
            return False

        try:
            pa = pyaudio.PyAudio()
            info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default = pa.get_device_info_by_index(info['defaultOutputDevice'])
        except Exception as e:
            log.debug('WASAPI no disponible: %s', e)
            return False

        candidates = []
        if default.get('isLoopbackDevice', False):
            candidates.append(default)
        else:
            others = []
            for i in range(pa.get_device_count()):
                try:
                    d = pa.get_device_info_by_index(i)
                except Exception:
                    continue
                if not d.get('isLoopbackDevice', False):
                    continue
                if default['name'] in d['name']:
                    candidates.append(d)
                else:
                    others.append(d)
            candidates.extend(others)

        for dev in candidates:
            channels = min(int(dev['maxInputChannels']), 2)
            if channels < 1:
                continue

            def callback(in_data, frame_count, time_info, status):
                try:
                    samples = np.frombuffer(in_data, dtype=np.float32)
                    if samples.size:
                        current = float(np.max(np.abs(samples)))
                        if current > self.peak:
                            self.peak = current
                except Exception:
                    pass
                return (None, pyaudio.paContinue)

            try:
                self._stream = pa.open(
                    format=pyaudio.paFloat32,
                    channels=channels,
                    rate=int(dev['defaultSampleRate']),
                    input=True,
                    input_device_index=dev['index'],
                    frames_per_buffer=int(dev['defaultSampleRate'] * 0.05),
                    stream_callback=callback,
                )
                self._stream.start_stream()
                self._pa = pa
                self.ok = True
                return True
            except Exception as e:
                log.debug("loopback '%s' no se pudo abrir: %s", dev['name'], e)
                continue

        pa.terminate()
        return False

    def stop(self) -> float:
        try:
            if self._stream is not None:
                self._stream.stop_stream()
                self._stream.close()
            if self._pa is not None:
                self._pa.terminate()
        except Exception:
            pass
        return self.peak


def run_audio_test(seconds: float = 2.0) -> dict:
    """Suena un tono y graba a la vez por micro y por loopback.

    Devuelve picos 0..1, o -1 si ese camino no se pudo abrir siquiera.
    """
    if _is_recording():
        return {'ok': False, 'reason': 'recording'}

    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as e:
        return {'ok': False, 'reason': 'deps', 'error': str(e)}

    rate = 44100
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    # 440 Hz a volumen moderado: tiene que oirse para la prueba, no asustar.
    tone = (0.25 * np.sin(2 * np.pi * 440 * t)).astype(np.float32).reshape(-1, 1)

    loop = _Loopback()
    loop.start()

    mic_peak = -1.0
    try:
        # playrec reproduce y graba en el mismo reloj: el micro oye el tono si
        # hay altavoces, y la voz si el usuario habla.
        rec = sd.playrec(tone, samplerate=rate, channels=1, dtype='float32')
        sd.wait()
        if rec is not None and rec.size:
            mic_peak = float(np.max(np.abs(rec)))
    except Exception as e:
        log.debug('fallo la grabacion de micro en la prueba: %s', e)
        mic_peak = -1.0
    finally:
        try:
            sd.stop()
        except Exception:
            pass
        loop_peak = loop.stop() if loop.ok else -1.0

    return {'ok': True, 'mic_peak': mic_peak, 'loop_peak': loop_peak}
