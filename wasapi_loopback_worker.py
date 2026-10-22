"""
Subprocess aislado para captura WASAPI loopback.
Protocolo stdout: 4 bytes samplerate (uint32 LE) + 4 bytes channels (uint32 LE) + stream float32 raw.

El header se escribe SOLO despues de abrir el stream con exito: si se escribiera
antes, el padre daria el loopback por arrancado y se quedaria esperando un audio
que nunca llega, grabando solo el microfono sin avisar.

Todo fallo se explica por stderr. El padre lo registra en el log; sin esto, el
worker moria en silencio y no habia forma de saber por que una reunion se grabo
sin el audio de los demas.

MULTI-SALIDA
Windows solo marca UNA salida como "por defecto", pero Teams puede sonar por
otra (altavoz de conferencias, auriculares Bluetooth...). Capturar solo el
loopback de la salida por defecto grababa silencio en ese caso. Ahora se abre el
loopback de TODAS las salidas disponibles y se mezclan en un unico flujo mono:
una salida en silencio no aporta nada y la que use Teams si. Si solo se consigue
abrir un dispositivo se usa el camino directo de siempre (sin mezcla), y si no
se abre ninguno se falla como antes.
"""
import queue
import struct
import sys
import threading
import time

import numpy as np

MIX_SR = 48000          # frecuencia del flujo mezclado
MIX_TICK = 0.1          # segundos de audio emitidos por iteracion
MAX_BUFFER_S = 1.0      # tope por dispositivo antes de descartar lo mas antiguo


def fail(msg: str):
    sys.stderr.write(msg)
    sys.stderr.flush()
    sys.exit(1)


class Resampler:
    """Pasa bloques interleaved a mono y a `out_sr`, conservando cola y fase
    entre bloques para que encadenarlos no deje costuras audibles."""

    def __init__(self, in_sr: int, n_ch: int, out_sr: int = MIX_SR):
        self.in_sr = in_sr
        self.n_ch = max(1, n_ch)
        self.out_sr = out_sr
        self._carry = np.zeros(0, dtype=np.float32)
        self._phase = 0.0

    def process(self, arr: np.ndarray) -> np.ndarray:
        if self.n_ch > 1:
            rem = len(arr) % self.n_ch
            if rem:
                arr = arr[:-rem]
            if not len(arr):
                return np.zeros(0, dtype=np.float32)
            arr = arr.reshape(-1, self.n_ch).mean(axis=1).astype(np.float32)
        if self.in_sr == self.out_sr:
            return np.asarray(arr, dtype=np.float32)

        buf = np.concatenate((self._carry, arr)) if len(self._carry) else arr
        if len(buf) < 2:
            self._carry = np.asarray(buf, dtype=np.float32)
            return np.zeros(0, dtype=np.float32)
        step = self.in_sr / self.out_sr
        n = int(np.floor((len(buf) - 1 - self._phase) / step)) + 1
        if n <= 0:
            self._carry = np.asarray(buf, dtype=np.float32)
            return np.zeros(0, dtype=np.float32)

        idx = self._phase + np.arange(n) * step
        out = np.interp(idx, np.arange(len(buf)), buf).astype(np.float32)
        next_pos = self._phase + n * step
        keep_from = min(int(np.floor(next_pos)), len(buf) - 1)
        self._carry = np.asarray(buf[keep_from:], dtype=np.float32)
        self._phase = next_pos - keep_from
        return out


def mix_blocks(blocks, n: int) -> np.ndarray:
    """Suma `blocks` (arrays mono) en un bloque de `n` muestras. Los bloques
    cortos se rellenan con silencio y la suma se limita a [-1, 1] para no
    saturar cuando dos salidas suenan a la vez."""
    out = np.zeros(n, dtype=np.float32)
    for b in blocks:
        m = min(len(b), n)
        if m:
            out[:m] += b[:m]
    np.clip(out, -1.0, 1.0, out=out)
    return out


class DeviceBuffer:
    """Cola mono a MIX_SR alimentada por el callback de un dispositivo."""

    def __init__(self, name: str, in_sr: int, n_ch: int):
        self.name = name
        self.resampler = Resampler(in_sr, n_ch)
        self._lock = threading.Lock()
        self._data = np.zeros(0, dtype=np.float32)

    def push(self, raw: bytes):
        block = self.resampler.process(np.frombuffer(raw, dtype=np.float32))
        if not len(block):
            return
        with self._lock:
            self._data = np.concatenate((self._data, block))
            cap = int(MAX_BUFFER_S * MIX_SR)
            if len(self._data) > cap:
                self._data = self._data[-cap:]

    def pop(self, n: int) -> np.ndarray:
        with self._lock:
            out, self._data = self._data[:n], self._data[n:]
        return out


def _list_candidates(pa, pyaudio):
    """Loopbacks ordenados: el de la salida por defecto primero."""
    try:
        wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    except OSError as e:
        pa.terminate()
        fail(f"WASAPI no disponible en este equipo: {e}")

    try:
        default_speakers = pa.get_device_info_by_index(wasapi_info['defaultOutputDevice'])
    except Exception as e:
        pa.terminate()
        fail(f"no se puede leer el dispositivo de salida por defecto: {e}")

    candidates = []
    if default_speakers.get('isLoopbackDevice', False):
        candidates.append(default_speakers)
    else:
        others = []
        for i in range(pa.get_device_count()):
            try:
                info = pa.get_device_info_by_index(i)
            except Exception:
                continue
            if not info.get('isLoopbackDevice', False):
                continue
            if default_speakers['name'] in info['name']:
                candidates.append(info)
            else:
                others.append(info)
        candidates.extend(others)

    if not candidates:
        pa.terminate()
        fail(f"no hay ningun dispositivo loopback (salida por defecto: "
             f"'{default_speakers['name']}')")
    return candidates


def _emit_header(sr: int, n_ch: int):
    sys.stdout.buffer.write(struct.pack('<I', sr))
    sys.stdout.buffer.write(struct.pack('<I', n_ch))
    sys.stdout.buffer.flush()


def main():
    try:
        import pyaudiowpatch as pyaudio
    except ImportError as e:
        fail(f"pyaudiowpatch no disponible: {e}")

    pa = pyaudio.PyAudio()
    candidates = _list_candidates(pa, pyaudio)

    # Estado compartido con los callbacks. Los streams se abren SIN arrancar:
    # el header tiene que salir antes que cualquier muestra y solo se sabe como
    # emitir (directo o mezcla) cuando se conoce cuantos dispositivos abrieron.
    state = {'mode': None}
    opened = []   # (info, stream, DeviceBuffer, n_ch, sr)
    errors = []

    for dev in candidates:
        n_ch = min(int(dev['maxInputChannels']), 2)
        if n_ch < 1:
            errors.append(f"'{dev['name']}': sin canales de entrada")
            continue
        sr = int(dev['defaultSampleRate'])
        buf = DeviceBuffer(dev['name'], sr, n_ch)

        def make_callback(buf):
            def callback(in_data, frame_count, time_info, status):
                if state['mode'] == 'direct':
                    sys.stdout.buffer.write(in_data)
                    sys.stdout.buffer.flush()
                elif state['mode'] == 'mix':
                    buf.push(in_data)
                return (None, pyaudio.paContinue)
            return callback

        try:
            stream = pa.open(
                format=pyaudio.paFloat32,
                channels=n_ch,
                rate=sr,
                input=True,
                input_device_index=dev['index'],
                frames_per_buffer=int(sr * 0.1),
                stream_callback=make_callback(buf),
                start=False,
            )
        except Exception as e:
            errors.append(f"'{dev['name']}': {e}")
            continue
        opened.append((dev, stream, buf, n_ch, sr))

    if not opened:
        pa.terminate()
        fail("ningun dispositivo loopback se pudo abrir -> " + " | ".join(errors))

    if len(opened) == 1:
        # Camino directo, identico al de siempre.
        dev, stream, _buf, n_ch, sr = opened[0]
        state['mode'] = 'direct'
        _emit_header(sr, n_ch)
        stream.start_stream()
        sys.stderr.write(f"loopback activo: '{dev['name']}' ({sr}Hz, {n_ch}ch)")
        sys.stderr.flush()
        try:
            while stream.is_active():
                time.sleep(0.1)
        except Exception:
            pass
        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()
        return

    # Varios dispositivos: se mezclan en un flujo mono a MIX_SR.
    _emit_header(MIX_SR, 1)
    state['mode'] = 'mix'
    started = []
    for dev, stream, buf, n_ch, sr in opened:
        try:
            stream.start_stream()
            started.append((dev, stream, buf))
        except Exception as e:
            errors.append(f"'{dev['name']}': {e}")
    if not started:
        # El header ya salio: sin streams el padre solo vera silencio, pero que
        # quede dicho en stderr.
        pa.terminate()
        fail("ningun loopback arranco tras abrirse -> " + " | ".join(errors))

    names = ", ".join(f"'{d['name']}'" for d, _s, _b in started)
    sys.stderr.write(f"loopback activo (mezcla de {len(started)}): {names}")
    if errors:
        sys.stderr.write(" | omitidos: " + " | ".join(errors))
    sys.stderr.flush()

    # Se emite al ritmo del reloj, no al de los dispositivos: WASAPI no entrega
    # nada mientras una salida esta en silencio y esperar a todas los streams
    # bloquearia la mezcla.
    n = int(MIX_SR * MIX_TICK)
    t_next = time.monotonic()
    try:
        while any(s.is_active() for _d, s, _b in started):
            t_next += MIX_TICK
            wait = t_next - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            elif wait < -1.0:
                t_next = time.monotonic()
            block = mix_blocks([b.pop(n) for _d, _s, b in started], n)
            sys.stdout.buffer.write(block.tobytes())
            sys.stdout.buffer.flush()
    except Exception:
        pass
    finally:
        for _d, stream, _b in started:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        pa.terminate()


if __name__ == '__main__':
    main()
