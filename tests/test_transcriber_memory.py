"""El modelo de Whisper no se queda cargado entre reuniones.

POR QUE EXISTE ESTE TEST (14/10/2026)
El daemon vivia en ~4,9 GB de memoria durante dias, medido seis veces entre el
02/10 y el 14/10. No era una fuga que creciera: era el modelo. 'medium' en int8
con cpu_threads=12 reserva 4,7 GB al cargarse (solo toca 0,9 GB), y transcriber
lo guardaba en una global para siempre. Esa reserva cuenta contra el limite de
memoria de Windows, que agrandaba el pagefile hasta dejar el disco sin espacio:
cuatro veces en dos semanas el portatil se quedo con menos de 1 GB libre.
Medido aparte: tras liberar el modelo la reserva baja de 4.872 a 671 MB.

Lo que se protege: que transcribe() suelte el modelo por cualquier camino de
salida, y que la reunion siguiente lo vuelva a cargar y transcriba igual.
"""
import sys
import types

import pytest

import transcriber as tr

pytestmark = pytest.mark.unit


@pytest.fixture
def modelo_cargado(monkeypatch):
    """Simula el estado del daemon tras una reunion: modelo en la global."""
    monkeypatch.setattr(tr, '_model', object())
    monkeypatch.setattr(tr, '_model_name', 'medium')


def test_se_libera_al_terminar_bien(monkeypatch, modelo_cargado, wav_factory, tr_dirs):
    wav = wav_factory(tr_dirs / 'recordings' / 'x.wav', seconds=0.1)
    monkeypatch.setattr(tr, '_transcribe_local', lambda *a, **k: ('[00:00] hola', 'es'))

    assert tr.transcribe(wav) == ('[00:00] hola', 'es')

    assert tr._model is None
    assert tr._model_name is None


def test_se_libera_aunque_falle_whisper_y_la_api(monkeypatch, modelo_cargado, wav_factory,
                                               tr_dirs):
    """El camino de error tambien: una reunion que no se pudo transcribir no
    puede dejar 4,7 GB reservados hasta el siguiente reinicio."""
    wav = wav_factory(tr_dirs / 'recordings' / 'x.wav', seconds=0.1)

    def falla(*a, **k):
        raise RuntimeError('fallo que no es de memoria')

    def api_falla(_p):
        raise RuntimeError('OPENAI_API_KEY no configurada')

    monkeypatch.setattr(tr, '_transcribe_local', falla)
    monkeypatch.setattr(tr, '_transcribe_openai', api_falla)

    assert tr.transcribe(wav) is None
    assert tr._model is None


def test_se_libera_al_cancelar(monkeypatch, modelo_cargado, wav_factory, tr_dirs):
    wav = wav_factory(tr_dirs / 'recordings' / 'x.wav', seconds=0.1)

    def cancelada(*a, **k):
        raise tr.TranscriptionCancelled()

    monkeypatch.setattr(tr, '_transcribe_local', cancelada)

    with pytest.raises(tr.TranscriptionCancelled):
        tr.transcribe(wav)
    assert tr._model is None


def test_se_libera_tambien_con_on_complete(monkeypatch, modelo_cargado, wav_factory, tr_dirs):
    """Con on_complete transcribe() corre en un hilo aparte; el modelo debe
    estar suelto cuando se avisa de que termino."""
    import threading
    wav = wav_factory(tr_dirs / 'recordings' / 'x.wav', seconds=0.1)
    monkeypatch.setattr(tr, '_transcribe_local', lambda *a, **k: ('[00:00] hola', 'es'))
    visto = {}
    hecho = threading.Event()

    def on_complete(text, lang):
        visto['modelo'] = tr._model
        visto['texto'] = text
        hecho.set()

    tr.transcribe(wav, on_complete=on_complete)

    assert hecho.wait(5), "on_complete no llego a llamarse"
    assert visto['texto'] == '[00:00] hola'
    assert visto['modelo'] is None


def test_la_cache_de_buferes_de_mkl_queda_desactivada():
    """Sin esto, soltar el modelo tras cada reunion seguia dejando ~90 MB por
    transcripcion que MKL guardaba para si (medido: con ella, ~5 MB)."""
    import os
    assert os.environ.get('MKL_DISABLE_FAST_MM') == '1'


def test_antes_de_cargar_whisper_el_proceso_sale_del_modo_eficiencia(monkeypatch):
    """Reunion real del 20/10/2026: el daemon transcribia con 2,4 de 14 nucleos
    (Windows lo frenaba por no tener ventana); fuera de ese modo, 7,7."""
    import win_power
    orden = []
    monkeypatch.setattr(win_power, 'opt_out_of_efficiency_mode', lambda: orden.append('eficiencia') or True)
    monkeypatch.setitem(sys.modules, 'faster_whisper', types.SimpleNamespace(
        WhisperModel=lambda name, **k: orden.append('carga') or object()))
    monkeypatch.setattr(tr, '_model', None)
    monkeypatch.setattr(tr, '_model_name', None)
    tr._get_model('tiny')
    assert orden == ['eficiencia', 'carga']


def test_la_reunion_siguiente_vuelve_a_cargar_el_modelo_y_transcribe(
        monkeypatch, wav_factory, tr_dirs, fake_whisper_model):
    """Lo que de verdad podria romper este cambio: que tras soltar el modelo la
    siguiente reunion no transcribiera. Recorre el camino real de _get_model y
    _transcribe_local; solo se sustituye la clase de faster-whisper."""
    cargas = []

    def whisper_model_falso(name, **kwargs):
        cargas.append(name)
        return fake_whisper_model(segments=[(0.0, 4.0, f'reunion {len(cargas)}')])

    monkeypatch.setitem(sys.modules, 'faster_whisper',
                        types.SimpleNamespace(WhisperModel=whisper_model_falso))
    monkeypatch.setattr(tr, '_model', None)
    monkeypatch.setattr(tr, '_model_name', None)
    monkeypatch.setattr(tr, 'WHISPER_MODEL', 'medium')
    monkeypatch.setattr(tr, '_available_mb', lambda: None)   # sin descartes por RAM

    primera = tr.transcribe(wav_factory(tr_dirs / 'recordings' / 'a.wav', seconds=0.1))
    assert tr._model is None
    segunda = tr.transcribe(wav_factory(tr_dirs / 'recordings' / 'b.wav', seconds=0.1))
    assert tr._model is None

    assert cargas == ['medium', 'medium']
    assert primera is not None and 'reunion 1' in primera[0]
    assert segunda is not None and 'reunion 2' in segunda[0]
