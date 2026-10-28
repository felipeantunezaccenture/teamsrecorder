"""Transcripción en vivo: se transcribe mientras se graba, no después.

Cada LIVE_INTERVAL segundos un hilo lee el audio nuevo de los temporales de la
grabación (.mic.part / .loop.part, que el recorder vuelca a disco bloque a
bloque), lo transcribe con el mismo Whisper local y guarda lo ya hecho en
<grabacion>.live. Al parar, el pipeline solo transcribe la cola que quedó sin
hacer en lugar de la reunión entera.

Garantías que importan:
- Misma salida que la diarización de transcriber.py: `[mm:ss] [Nombre]: texto`
  para el micro y `[Otros]` para el sistema, ordenado por tiempo.
- Nada se pierde ni se duplica: cada pista lleva su propio puntero `done` y un
  segmento solo se confirma cuando está completo (acaba al menos _HOLD_SECS
  antes del final de la ventana); el resto se rehace en la ventana siguiente.
- Cualquier fallo (memoria, disco, estado incoherente) devuelve None y el
  pipeline hace la transcripción de siempre sobre el WAV completo.
"""
import json
import logging
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

import transcriber as tr
from config import ME_NAME, SAMPLE_RATE

log = logging.getLogger(__name__)

LIVE_INTERVAL = 60.0          # cada cuánto se mira si hay audio nuevo
_MIN_WINDOW_SECS = 15.0       # no merece la pena arrancar Whisper con menos
_MAX_WINDOW_SECS = float(tr._CHUNK_SECS)   # acota la memoria si hay atraso
_HOLD_SECS = 3.0              # cola de la ventana que se deja para la siguiente

# Por debajo de este pico no hay voz, solo ruido de fondo. Sin esta puerta, la
# normalización de abajo amplificaría ese ruido y Whisper lo transcribiría.
_MIN_PEAK = {'mic': 0.005, 'loop': 0.02}
_TARGET_PEAK = 0.8
_MAX_GAIN = 10.0

_TRACKS = ('mic', 'loop')


def _frames(path: Path | None) -> int:
    try:
        if path and Path(path).exists():
            return sf.info(str(path)).frames
    except Exception:
        pass
    return 0


class LiveTranscript:
    def __init__(self, state_path: Path, mic_part: Path | None = None,
                 loop_part: Path | None = None):
        self.state_path = Path(state_path)
        self._mic_part = mic_part
        self._loop_part = loop_part
        self.segments: dict[str, list[tuple[float, str]]] = {t: [] for t in _TRACKS}
        self.done: dict[str, float] = {t: 0.0 for t in _TRACKS}
        self.lang: str | None = None
        self.failed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Serializa la lectura de los .part con su borrado al terminar la
        # grabación: en Windows un fichero con un handle abierto no se puede
        # borrar, y quedaría huérfano en recordings/.
        self._io_lock = threading.Lock()
        self._model_name: str | None = None
        # Pistas del glosario (invitados + términos). None = aún sin calcular;
        # se calculan una vez, en el hilo, y se guardan en el estado.
        self.hotwords: str | None = None
        self.hotwords_fn = None

    def _hotwords(self) -> str:
        if self.hotwords is None:
            try:
                self.hotwords = self.hotwords_fn() if self.hotwords_fn else ''
            except Exception as e:
                log.debug(f"Sin pistas de glosario: {e}")
                self.hotwords = ''
        return self.hotwords

    # ── ciclo de vida ────────────────────────────────────────────────────────

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name='LiveTranscriber')
        self._thread.start()

    def stop(self):
        """Pide parar. Acaba la ventana en curso y no empieza otra."""
        self._stop.set()

    def quiesce(self):
        """Vuelve cuando el hilo ya no va a abrir los .part (tras stop())."""
        with self._io_lock:
            pass

    def join(self, timeout: float | None = None):
        if self._thread:
            self._thread.join(timeout)

    def _run(self):
        try:
            while not self._stop.wait(LIVE_INTERVAL):
                self.advance(self._mic_part, self._loop_part, final=False)
        except tr.TranscriptionCancelled:
            pass
        except Exception as e:
            self.failed = True
            log.warning(f"Transcripción en vivo desactivada ({e}); "
                        f"se transcribirá el audio completo al terminar", exc_info=True)
            self.state_path.unlink(missing_ok=True)

    # ── avance ───────────────────────────────────────────────────────────────

    def advance(self, mic_path, loop_path, final: bool, should_cancel=None):
        """Transcribe lo que falte de cada pista. Con final=True consume hasta
        el último fotograma y confirma todo, sin cola retenida."""
        paths = {'mic': mic_path, 'loop': loop_path}
        for track in _TRACKS:
            while self._advance_track(track, paths[track], final, should_cancel):
                pass
        self._save()

    def _read_window(self, track, path, final):
        """Devuelve (audio, start_secs) o None. Abre y cierra el fichero aquí
        dentro, bajo _io_lock, para no retener el handle mientras se transcribe."""
        with self._io_lock:
            if self._stop.is_set() and not final:
                return None
            if not path or not Path(path).exists():
                return None
            sr = SAMPLE_RATE
            try:
                with sf.SoundFile(str(path)) as f:
                    total = f.frames
                    start = int(self.done[track] * sr)
                    avail = total - start
                    if avail <= 0:
                        return None
                    if not final and avail < _MIN_WINDOW_SECS * sr:
                        return None
                    n = min(avail, int(_MAX_WINDOW_SECS * sr))
                    f.seek(start)
                    audio = f.read(n, dtype='float32', always_2d=False)
            except (RuntimeError, OSError):
                # La cabecera puede estar a medio actualizar: se reintenta luego.
                return None
        if not len(audio):
            return None
        return audio, start / sr

    def _advance_track(self, track, path, final, should_cancel) -> bool:
        """Procesa UNA ventana. Devuelve True si queda más por hacer ya."""
        tr._check_cancel(should_cancel)
        got = self._read_window(track, path, final)
        if got is None:
            return False
        audio, start = got
        window_len = len(audio) / SAMPLE_RATE
        window_end = start + window_len
        capped = window_len >= _MAX_WINDOW_SECS - 0.01

        peak = float(np.abs(audio).max())
        if peak < _MIN_PEAK[track]:
            self.done[track] = window_end
            return capped

        gain = min(_TARGET_PEAK / peak, _MAX_GAIN)
        segs = self._transcribe((audio * gain).astype(np.float32), start, should_cancel)

        if final:
            commit, next_done = segs, window_end
        else:
            hold = window_end - _HOLD_SECS
            commit, next_done = [], window_end
            for i, (s_start, s_end, _text) in enumerate(segs):
                if s_end <= hold:
                    commit.append(segs[i])
                else:
                    next_done = max(s_start, start)
                    break
            # Un solo segmento que no cabe nunca en la ventana pararía el avance
            # para siempre: con la ventana al máximo se confirma lo que haya.
            if next_done <= self.done[track] + 1e-6 and capped:
                commit, next_done = segs, window_end

        for s_start, _s_end, text in commit:
            self.segments[track].append((s_start, text))
        if next_done > self.done[track]:
            self.done[track] = next_done
        self._save()
        return capped and next_done >= window_end - 1e-6

    def _transcribe(self, audio, offset, should_cancel):
        hotwords = self._hotwords()     # fuera del lock: puede consultar Outlook
        with tr._infer_lock:
            if self._model_name is None:
                # Si ya hay un modelo cargado se usa ese: alternar entre dos
                # tamaños con la memoria justa descargaría uno para cargar otro.
                self._model_name = tr._model_name or tr._fallback_plan(tr.WHISPER_MODEL)[0][0]
            model = tr._get_model(self._model_name)
            args = tr._targs(hotwords)
            if self.lang:
                args['language'] = self.lang
                args.pop('language_detection_segments', None)
            segments, info = model.transcribe(audio, **args)
            out = []
            for seg in segments:
                tr._check_cancel(should_cancel)
                text = seg.text.strip()
                if text:
                    out.append((offset + seg.start, offset + seg.end, text))
        if self.lang is None and out:
            self.lang = tr._remap(getattr(info, 'language', None))
        return out

    # ── resultado ────────────────────────────────────────────────────────────

    def finish(self, mic_path, loop_path, should_cancel=None) -> tuple[str, str] | None:
        """Espera al hilo, transcribe la cola con las pistas finales y devuelve
        (texto, idioma). None si no se puede garantizar que esté completo."""
        self.stop()
        self.join()
        if self.failed or not Path(mic_path).exists():
            return None
        for track, path in (('mic', mic_path), ('loop', loop_path)):
            if path and Path(path).exists():
                dur = _frames(path) / SAMPLE_RATE
                if self.done[track] > dur + 1.0:
                    log.warning(f"Estado en vivo incoherente en '{track}' "
                                f"({self.done[track]:.0f}s > {dur:.0f}s de audio)")
                    return None
        # Sin tomar _infer_lock aqui: _transcribe ya lo toma por ventana. Tenerlo
        # todo el avance dejaba sin Whisper a la transcripcion en vivo de la
        # reunion siguiente mientras duraba la cola de esta.
        self.advance(mic_path, loop_path, final=True, should_cancel=should_cancel)
        # Comprobación de que no se ha saltado ningún trozo.
        for track, path in (('mic', mic_path), ('loop', loop_path)):
            if path and Path(path).exists():
                dur = _frames(path) / SAMPLE_RATE
                if self.done[track] < dur - 0.1:
                    log.warning(f"Transcripción en vivo incompleta en '{track}' "
                                f"({self.done[track]:.0f}s de {dur:.0f}s)")
                    return None
        return self.render(), (self.lang or 'es')

    def render(self) -> str:
        mic, loop = self.segments['mic'], self.segments['loop']
        if not loop:
            rows = sorted(mic, key=lambda r: r[0])
            return '\n'.join(f"{tr._format_time(t)} {text}" for t, text in rows)
        me = f'[{ME_NAME}]' if ME_NAME else '[Grabador]'
        rows = [(t, me, x) for t, x in mic] + [(t, '[Otros]', x) for t, x in loop]
        rows.sort(key=lambda r: r[0])
        return '\n'.join(f"{tr._format_time(t)} {who}: {text}" for t, who, text in rows)

    # ── persistencia (sobrevive a un cierre del proceso) ─────────────────────

    def _save(self):
        data = {'lang': self.lang, 'done': self.done, 'hotwords': self.hotwords,
                'segments': {t: [[s, x] for s, x in v] for t, v in self.segments.items()}}
        tmp = self.state_path.with_name(self.state_path.name + '.tmp')
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            tmp.replace(self.state_path)
        except OSError as e:
            log.warning(f"No se pudo guardar {self.state_path.name}: {e}")

    @classmethod
    def load(cls, state_path: Path) -> 'LiveTranscript | None':
        try:
            data = json.loads(Path(state_path).read_text(encoding='utf-8'))
            live = cls(state_path)
            live.lang = data.get('lang')
            live.hotwords = data.get('hotwords')
            live.done = {t: float(data['done'].get(t, 0.0)) for t in _TRACKS}
            live.segments = {t: [(float(s), x) for s, x in data['segments'].get(t, [])]
                             for t in _TRACKS}
            return live
        except Exception as e:
            log.warning(f"Estado en vivo ilegible ({state_path.name}): {e}")
            return None


# ── registro de grabaciones en curso ────────────────────────────────────────

_active: dict[str, LiveTranscript] = {}
_active_lock = threading.Lock()


def state_path_for(wav_path: Path) -> Path:
    return Path(wav_path).with_suffix('.live')


def _hotwords_fn(wav_path: Path):
    def fn():
        import glossary
        return glossary.recording_context(wav_path)['hotwords']
    return fn


def start_for(wav_path: Path, mic_part: Path, loop_part: Path) -> LiveTranscript:
    state = state_path_for(wav_path)
    state.unlink(missing_ok=True)
    live = LiveTranscript(state, mic_part, loop_part)
    live.hotwords_fn = _hotwords_fn(wav_path)
    with _active_lock:
        _active[str(wav_path)] = live
    live.start()
    log.info(f"Transcripción en vivo activada ({LIVE_INTERVAL:.0f}s) → {state.name}")
    return live


def discard_for(wav_path: Path):
    """Grabación cancelada: se descarta también lo transcrito."""
    with _active_lock:
        live = _active.pop(str(wav_path), None)
    if live:
        live.stop()
    state_path_for(wav_path).unlink(missing_ok=True)


def finish_for(wav_path: Path, should_cancel=None) -> tuple[str, str] | None:
    """Resultado completo de la transcripción en vivo de esta grabación, o None
    si no hay (o no es fiable) y toca transcribir el WAV entero."""
    wav_path = Path(wav_path)
    state = state_path_for(wav_path)
    with _active_lock:
        live = _active.pop(str(wav_path), None)
    if live is None and state.exists():
        live = LiveTranscript.load(state)   # el proceso se reinició a mitad
        if live:
            live.hotwords_fn = _hotwords_fn(wav_path)
    if live is None:
        state.unlink(missing_ok=True)       # ilegible: no debe quedar huérfano
        return None

    res = None
    try:
        res = live.finish(wav_path.with_suffix('.mic.wav'),
                          wav_path.with_suffix('.loop.wav'),
                          should_cancel=should_cancel)
    except tr.TranscriptionCancelled:
        raise
    except Exception as e:
        log.warning(f"Transcripción en vivo falló al terminar: {e}", exc_info=True)

    state.unlink(missing_ok=True)
    if res is None:
        log.warning(f"Sin transcripción en vivo válida para {wav_path.name}: "
                    f"se transcribe el audio completo")
        return None
    for suffix in ('.mic.wav', '.loop.wav'):
        wav_path.with_suffix(suffix).unlink(missing_ok=True)
    log.info(f"Transcripción en vivo completada para {wav_path.name}")
    return res
