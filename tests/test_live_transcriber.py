"""Transcripción en vivo: se transcribe mientras se graba y al parar solo falta la cola.

Invariantes que protegen estos tests:
- NI HUECOS NI DUPLICADOS entre ventanas (cada pista lleva su puntero `done`).
- La salida es idéntica en formato a la de la diarización de transcriber.py.
- Cualquier duda (estado incoherente, trozo sin cubrir, fallo del hilo) devuelve
  None para que el pipeline transcriba el WAV completo como siempre.
"""
import numpy as np
import pytest
import soundfile as sf

import live_transcriber as lt
import transcriber as tr

pytestmark = pytest.mark.unit


class _Seg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class _Info:
    language = 'es'


class _GridModel:
    """Un segmento de 9 s cada 10 s de audio recibido, con tiempos relativos."""

    def __init__(self):
        self.calls = 0

    def transcribe(self, audio, **kwargs):
        self.calls += 1
        secs = len(audio) / 16000
        n = int(secs // 10) + (1 if secs % 10 else 0)
        segs = [_Seg(i * 10.0, min(i * 10.0 + 9.0, secs), f'seg{i}') for i in range(n)]
        return iter(segs), _Info()


@pytest.fixture
def modelo(monkeypatch):
    m = _GridModel()
    monkeypatch.setattr(tr, '_get_model', lambda name=None: m)
    monkeypatch.setattr(tr, '_available_mb', lambda: None)
    monkeypatch.setattr(lt, 'ME_NAME', '')
    return m


def _wav(path, seconds, amplitude=0.3):
    n = int(seconds * 16000)
    t = np.arange(n, dtype=np.float32) / 16000
    data = (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    sf.write(str(path), data, 16000, subtype='FLOAT')
    return path


def _starts(live, track='mic'):
    return [s for s, _ in live.segments[track]]


# ── ventanas ────────────────────────────────────────────────────────────────

def test_la_ventana_en_vivo_retiene_la_cola_sin_acabar(modelo, tr_dirs):
    mic = _wav(tr_dirs / 'm.wav', 130)
    live = lt.LiveTranscript(tr_dirs / 'a.live')

    live.advance(mic, None, final=False)

    # El segmento 120-129 acaba a menos de 3 s del final de la ventana (130):
    # puede estar cortado, se rehace en la siguiente.
    assert _starts(live) == [i * 10.0 for i in range(12)]
    assert live.done['mic'] == 120.0


def test_el_final_completa_la_cola_sin_huecos_ni_duplicados(modelo, tr_dirs):
    mic = _wav(tr_dirs / 'm.wav', 130)
    live = lt.LiveTranscript(tr_dirs / 'a.live')
    live.advance(mic, None, final=False)

    live.advance(mic, None, final=True)

    assert _starts(live) == [i * 10.0 for i in range(13)]
    assert live.done['mic'] == pytest.approx(130.0)


def test_sin_audio_nuevo_suficiente_no_se_arranca_whisper(modelo, tr_dirs):
    mic = _wav(tr_dirs / 'm.wav', 10)            # < 15 s
    live = lt.LiveTranscript(tr_dirs / 'a.live')

    live.advance(mic, None, final=False)

    assert modelo.calls == 0
    assert live.done['mic'] == 0.0


def test_el_silencio_avanza_el_puntero_sin_llamar_a_whisper(modelo, tr_dirs):
    """Sin la puerta de volumen, la normalización amplificaría el ruido de
    fondo hasta que Whisper lo transcribiera."""
    mic = _wav(tr_dirs / 'm.wav', 60, amplitude=0.001)
    live = lt.LiveTranscript(tr_dirs / 'a.live')

    live.advance(mic, None, final=False)

    assert modelo.calls == 0
    assert live.done['mic'] == pytest.approx(60.0)
    assert live.segments['mic'] == []


def test_con_atraso_se_procesa_por_ventanas_acotadas(modelo, tr_dirs, monkeypatch):
    """Si el hilo va por detrás, no se carga todo el audio en memoria de golpe."""
    monkeypatch.setattr(lt, '_MAX_WINDOW_SECS', 100.0)
    mic = _wav(tr_dirs / 'm.wav', 250)
    live = lt.LiveTranscript(tr_dirs / 'a.live')

    live.advance(mic, None, final=True)

    assert modelo.calls == 3
    assert _starts(live) == [i * 10.0 for i in range(25)]


# ── pistas y formato ────────────────────────────────────────────────────────

def test_dos_pistas_salen_etiquetadas_y_ordenadas_por_tiempo(modelo, tr_dirs):
    mic = _wav(tr_dirs / 'm.wav', 30)
    loop = _wav(tr_dirs / 'l.wav', 30)
    live = lt.LiveTranscript(tr_dirs / 'a.live')

    live.advance(mic, loop, final=True)
    lines = live.render().splitlines()

    assert lines[0] == '[00:00] [Grabador]: seg0'
    assert lines[1] == '[00:00] [Otros]: seg0'
    assert len(lines) == 6


def test_con_nombre_configurado_se_usa_en_lugar_de_grabador(modelo, tr_dirs, monkeypatch):
    monkeypatch.setattr(lt, 'ME_NAME', 'Inés')
    live = lt.LiveTranscript(tr_dirs / 'a.live')
    live.segments['mic'] = [(1.0, 'hola')]
    live.segments['loop'] = [(5.0, 'buenas')]

    assert live.render() == '[00:01] [Inés]: hola\n[00:05] [Otros]: buenas'


def test_sin_pista_del_sistema_no_hay_etiquetas(modelo, tr_dirs):
    """Igual que la transcripción sin diarizar: `[mm:ss] texto`."""
    live = lt.LiveTranscript(tr_dirs / 'a.live')
    live.segments['mic'] = [(65.0, 'solo yo')]

    assert live.render() == '[01:05] solo yo'


def test_el_idioma_se_fija_en_la_primera_ventana_con_voz(modelo, tr_dirs):
    mic = _wav(tr_dirs / 'm.wav', 60)
    live = lt.LiveTranscript(tr_dirs / 'a.live')

    live.advance(mic, None, final=False)

    assert live.lang == 'es'


# ── persistencia ────────────────────────────────────────────────────────────

def test_el_estado_sobrevive_a_un_reinicio(modelo, tr_dirs):
    mic = _wav(tr_dirs / 'm.wav', 130)
    a = lt.LiveTranscript(tr_dirs / 'a.live')
    a.advance(mic, None, final=False)

    b = lt.LiveTranscript.load(tr_dirs / 'a.live')

    assert b.done == a.done
    assert b.segments == a.segments
    assert b.lang == 'es'


def test_estado_corrupto_no_lanza(tr_dirs):
    p = tr_dirs / 'a.live'
    p.write_text('{no es json', encoding='utf-8')
    assert lt.LiveTranscript.load(p) is None


# ── cierre: finish / finish_for ─────────────────────────────────────────────

def test_finish_devuelve_texto_e_idioma(modelo, tr_dirs):
    mic = _wav(tr_dirs / 'm.wav', 130)
    live = lt.LiveTranscript(tr_dirs / 'a.live')
    live.advance(mic, None, final=False)

    text, lang = live.finish(mic, tr_dirs / 'no_hay_loop.wav')

    assert lang == 'es'
    assert len(text.splitlines()) == 13


def test_finish_informa_del_progreso_desde_lo_ya_hecho_hasta_el_final(modelo, tr_dirs):
    """Con casi todo transcrito en vivo, el % no puede quedarse en blanco ni
    empezar de cero: arranca donde lo dejo la grabacion y sube hasta 99."""
    mic = _wav(tr_dirs / 'm.wav', 130)
    live = lt.LiveTranscript(tr_dirs / 'a.live')
    live.advance(mic, None, final=False)
    vistos = []

    live.finish(mic, None, on_progress=vistos.append)

    assert vistos, "no se informo ningun porcentaje"
    assert vistos[0] > 80, "debe arrancar en lo ya hecho, no en 0"
    assert vistos == sorted(vistos) and vistos[-1] <= 99


def test_finish_sin_pista_final_pide_transcribir_entero(modelo, tr_dirs):
    live = lt.LiveTranscript(tr_dirs / 'a.live')
    assert live.finish(tr_dirs / 'no_existe.wav', None) is None


def test_finish_con_estado_mas_largo_que_el_audio_es_incoherente(modelo, tr_dirs):
    """Un .live de otra grabación, o un WAV truncado: no se fía de él."""
    mic = _wav(tr_dirs / 'm.wav', 30)
    live = lt.LiveTranscript(tr_dirs / 'a.live')
    live.done['mic'] = 500.0

    assert live.finish(mic, None) is None


def test_finish_con_fallo_del_hilo_pide_transcribir_entero(modelo, tr_dirs):
    mic = _wav(tr_dirs / 'm.wav', 30)
    live = lt.LiveTranscript(tr_dirs / 'a.live')
    live.failed = True

    assert live.finish(mic, None) is None


def test_finish_for_sin_estado_no_hace_nada(tr_dirs):
    assert lt.finish_for(tr_dirs / 'recordings' / 'x.wav') is None


def test_finish_for_consume_el_estado_y_borra_las_pistas(modelo, tr_dirs):
    rec = tr_dirs / 'recordings'
    wav = _wav(rec / 'x.wav', 60)
    _wav(rec / 'x.mic.wav', 60)
    live = lt.LiveTranscript(lt.state_path_for(wav))
    live.advance(rec / 'x.mic.wav', None, final=False)

    text, lang = lt.finish_for(wav)

    assert text
    assert not lt.state_path_for(wav).exists()
    assert not (rec / 'x.mic.wav').exists()
    assert wav.exists(), "el WAV mezclado no se toca"


def test_finish_for_retoma_un_estado_de_disco(modelo, tr_dirs):
    """El proceso se reinició a mitad de la reunión: queda el .live, no el hilo."""
    rec = tr_dirs / 'recordings'
    wav = _wav(rec / 'x.wav', 130)
    mic = _wav(rec / 'x.mic.wav', 130)
    lt.LiveTranscript(lt.state_path_for(wav)).advance(mic, None, final=False)

    text, _ = lt.finish_for(wav)

    assert len(text.splitlines()) == 13


def test_finish_for_con_estado_invalido_lo_borra_y_devuelve_none(modelo, tr_dirs):
    rec = tr_dirs / 'recordings'
    wav = _wav(rec / 'x.wav', 30)
    state = lt.state_path_for(wav)
    state.write_text('{no es json', encoding='utf-8')

    assert lt.finish_for(wav) is None
    assert not state.exists()


def test_descartar_borra_el_estado(tr_dirs):
    wav = tr_dirs / 'recordings' / 'x.wav'
    lt.state_path_for(wav).write_text('{}', encoding='utf-8')

    lt.discard_for(wav)

    assert not lt.state_path_for(wav).exists()


# ── hilo y lectura mientras se escribe ──────────────────────────────────────

def test_se_puede_leer_un_wav_que_aun_se_esta_escribiendo(modelo, tr_dirs):
    """El recorder hace flush en cada bloque: el hilo en vivo lee ese mismo
    fichero abierto por el escritor. En Windows esto es lo que podría fallar."""
    part = tr_dirs / 'm.part'
    data = (0.3 * np.sin(np.arange(16000 * 20) / 20)).astype(np.float32)
    with sf.SoundFile(str(part), mode='w', samplerate=16000, channels=1,
                      format='WAV', subtype='FLOAT') as w:
        w.write(data)
        w.flush()
        live = lt.LiveTranscript(tr_dirs / 'a.live')

        got = live._read_window('mic', part, final=False)

    assert got is not None
    audio, start = got
    assert start == 0.0
    assert len(audio) == len(data)


def test_tras_stop_el_hilo_no_abre_mas_ficheros(modelo, tr_dirs):
    mic = _wav(tr_dirs / 'm.wav', 60)
    live = lt.LiveTranscript(tr_dirs / 'a.live')
    live.stop()

    assert live._read_window('mic', mic, final=False) is None
    live.quiesce()                      # no se queda bloqueado


def test_un_fallo_en_el_hilo_lo_desactiva_y_borra_el_estado(modelo, tr_dirs, monkeypatch):
    """Si algo revienta en vivo, el pipeline debe transcribir el WAV entero."""
    monkeypatch.setattr(lt, 'LIVE_INTERVAL', 0.01)
    state = tr_dirs / 'a.live'
    state.write_text('{}', encoding='utf-8')
    live = lt.LiveTranscript(state, tr_dirs / 'no.part', None)

    def boom(*a, **k):
        raise MemoryError('sin memoria')
    monkeypatch.setattr(live, 'advance', boom)

    live.start()
    live.join(timeout=5)

    assert live.failed
    assert not state.exists()
