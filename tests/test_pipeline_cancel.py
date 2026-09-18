"""Descartar una reunion ya grabada: cancelacion del transcriptor y papelera.

POR QUE EXISTE ESTA FUNCIONALIDAD (pedida el 16/09/2026)
Antes solo se podia cancelar mientras grababa. Si te dabas cuenta a mitad de
que la reunion no tenia sentido, el audio se transcribia y generaba minutas
igual: minutos de Whisper, tokens de Claude y un WAV en disco, sin forma de
abortar. Ahora se descarta en cola o transcribiendo, y todo va a la PAPELERA,
no se borra.
"""
import json

import pytest

import tray_app as ta
import transcriber as tr

pytestmark = pytest.mark.unit

STEM = '2026-09-16_12-00_Reunion_Descartable'      # formato real: YYYY-MM-DD_HH-MM_nombre
MD_NAME = '20260916_1200_Reunion_Descartable.md'   # formato real de minutas


# ── El transcriptor corta de verdad y no cae a la API de pago ──────────────

@pytest.fixture
def modelo_largo(fake_whisper_model):
    """50 segmentos: suficientes para distinguir 'corto pronto' de 'recorrio
    todo y luego descarto el resultado'."""
    return fake_whisper_model(
        segments=[(i * 5.0, i * 5.0 + 4.0, f'frase {i}') for i in range(50)],
        duration=100.0,
    )


def test_sin_cancelar_devuelve_todas_las_frases(modelo_largo, wav_factory, tr_dirs):
    wav = wav_factory(tr_dirs / 'recordings' / 'x.wav', seconds=0.1)
    text, _ = tr._transcribe_whole(modelo_largo, wav, should_cancel=lambda: False)
    assert text.count('frase') == 50


def test_cancelar_lanza_la_excepcion_dedicada(modelo_largo, wav_factory, tr_dirs):
    wav = wav_factory(tr_dirs / 'recordings' / 'x.wav', seconds=0.1)
    with pytest.raises(tr.TranscriptionCancelled):
        tr._transcribe_whole(modelo_largo, wav, should_cancel=lambda: True)


def test_cancelar_corta_pronto_y_no_recorre_todos_los_segmentos(modelo_largo, wav_factory,
                                                                tr_dirs):
    """faster-whisper devuelve un generador perezoso: salir del bucle corta el
    trabajo de verdad, no solo descarta el resultado."""
    wav = wav_factory(tr_dirs / 'recordings' / 'x.wav', seconds=0.1)
    vistas = {'n': 0}

    def cancelar_a_la_tercera():
        vistas['n'] += 1
        return vistas['n'] > 3

    with pytest.raises(tr.TranscriptionCancelled):
        tr._transcribe_whole(modelo_largo, wav, should_cancel=cancelar_a_la_tercera)

    assert vistas['n'] <= 6, f"recorrio {vistas['n']} segmentos antes de parar"


def test_no_prueba_modelos_mas_pequenos_al_cancelar(monkeypatch, wav_factory, tr_dirs,
                                                    fake_whisper_model):
    """Cancelar no es un fallo de memoria: no debe degradar de modelo."""
    wav = wav_factory(tr_dirs / 'recordings' / 'x.wav', seconds=0.1)
    cargas = {'n': 0}

    def contar(name=None):
        cargas['n'] += 1
        return fake_whisper_model()

    monkeypatch.setattr(tr, '_get_model', contar)

    with pytest.raises(tr.TranscriptionCancelled):
        tr._transcribe_local(wav, should_cancel=lambda: True)

    assert cargas['n'] == 0


def test_no_llama_a_la_api_de_pago_al_cancelar(monkeypatch, wav_factory, tr_dirs):
    """El fallback a OpenAI cuesta dinero: cancelar no debe dispararlo."""
    wav = wav_factory(tr_dirs / 'recordings' / 'x.wav', seconds=0.1)
    llamadas = {'n': 0}

    def fake_openai(_p):
        llamadas['n'] += 1
        return ('', 'es')

    monkeypatch.setattr(tr, '_transcribe_openai', fake_openai)

    with pytest.raises(tr.TranscriptionCancelled):
        tr.transcribe(wav, should_cancel=lambda: True)

    assert llamadas['n'] == 0


def test_la_cancelacion_se_propaga_desde_transcribe(monkeypatch, wav_factory, tr_dirs):
    wav = wav_factory(tr_dirs / 'recordings' / 'x.wav', seconds=0.1)
    monkeypatch.setattr(tr, '_transcribe_openai', lambda _p: ('', 'es'))
    with pytest.raises(tr.TranscriptionCancelled):
        tr.transcribe(wav, should_cancel=lambda: True)


# ── Registro de la cancelacion y senal entre procesos ──────────────────────

def test_un_trabajo_nuevo_no_esta_cancelado(tray):
    assert not tray._is_cancelled(STEM)


def test_cancel_job_confirma_y_deja_marca_en_memoria(tray):
    tray._pipeline_queued = [STEM, 'otro']
    assert tray.cancel_job(STEM) is True
    assert tray._is_cancelled(STEM)


def test_cancel_job_escribe_la_senal_en_fichero(tray):
    tray._pipeline_queued = [STEM]
    tray.cancel_job(STEM)
    assert ta._CANCEL_FILE.exists()
    assert STEM in ta._read_cancel_signals()


def test_el_trabajo_cancelado_sale_de_la_cola_visible(tray):
    tray._pipeline_queued = [STEM, 'otro']
    tray.cancel_job(STEM)
    assert tray._pipeline_queued == ['otro']


def test_otra_instancia_ve_la_cancelacion_por_fichero(tray_factory):
    """La ventana web corre en OTRO proceso: sin el fichero, el daemon nunca
    se enteraria de que el usuario pulso descartar."""
    daemon = tray_factory()
    ventana_web = tray_factory()

    ventana_web._pipeline_queued = [STEM]
    ventana_web.cancel_job(STEM)

    assert daemon._is_cancelled(STEM), "el daemon no vio la senal del otro proceso"


def test_olvidar_la_cancelacion_limpia_memoria_y_fichero(tray):
    tray._pipeline_queued = [STEM]
    tray.cancel_job(STEM)
    tray._forget_cancelled(STEM)
    assert not tray._is_cancelled(STEM)
    assert STEM not in ta._read_cancel_signals()


def test_cancelar_dos_veces_no_duplica_la_linea_en_el_fichero(tray):
    """El fichero de senales es de tipo append: sin deduplicar, pulsar
    descartar dos veces lo hace crecer con la misma linea repetida."""
    tray._pipeline_queued = [STEM]
    tray.cancel_job(STEM)
    tray.cancel_job(STEM)

    lineas = [ln for ln in ta._CANCEL_FILE.read_text(encoding='utf-8').splitlines() if ln.strip()]
    assert lineas.count(STEM) == 1, lineas


# ── El descarte manda todo a la papelera ───────────────────────────────────

@pytest.fixture
def reunion_completa(tr_dirs, wav_factory):
    """Los cuatro artefactos de una reunion ya procesada, mas un .partial."""
    wav = wav_factory(ta.RECORDINGS_DIR / f'{STEM}.wav', seconds=0.2)
    (ta.RECORDINGS_DIR / f'{STEM}.partial').write_text('parcial', encoding='utf-8')
    (ta.RECORDINGS_DIR / f'{STEM}_transcript.txt').write_text('texto', encoding='utf-8')
    md = ta.MINUTES_DIR / MD_NAME
    md.write_text('TITULO: Reunion Descartable\n\ncontenido', encoding='utf-8')
    md.with_suffix('.html').write_text('<html></html>', encoding='utf-8')
    return {'wav': wav, 'md': md, 'trash': tr_dirs / 'trash'}


def test_descartar_crea_una_sola_carpeta_en_la_papelera(tray, reunion_completa):
    tray._discard_job(reunion_completa['wav'], 'test', reunion_completa['md'])
    carpetas = list(reunion_completa['trash'].iterdir())
    assert len(carpetas) == 1, [d.name for d in carpetas]


@pytest.mark.parametrize('nombre', [
    f'{STEM}.wav',
    f'{STEM}.partial',
    f'{STEM}_transcript.txt',
    MD_NAME,
    '20260916_1200_Reunion_Descartable.html',
])
def test_cada_artefacto_acaba_en_la_papelera(tray, reunion_completa, nombre):
    tray._discard_job(reunion_completa['wav'], 'test', reunion_completa['md'])
    destino = next(reunion_completa['trash'].iterdir())
    assert nombre in {p.name for p in destino.iterdir()}


def test_no_queda_nada_en_recordings_ni_en_minutes(tray, reunion_completa):
    tray._discard_job(reunion_completa['wav'], 'test', reunion_completa['md'])
    assert not reunion_completa['wav'].exists()
    assert not (ta.RECORDINGS_DIR / f'{STEM}.partial').exists()
    assert not reunion_completa['md'].exists()


def test_se_escribe_el_meta_de_la_papelera(tray, reunion_completa):
    tray._discard_job(reunion_completa['wav'], 'test', reunion_completa['md'])
    meta_f = next(reunion_completa['trash'].iterdir()) / '_trash_meta.json'
    assert meta_f.exists()


@pytest.mark.parametrize('clave', ['stem', 'title', 'date', 'time', 'deleted_at', 'files'])
def test_el_meta_tiene_las_claves_que_espera_list_trash(tray, reunion_completa, clave):
    """Contrato con app_window.list_trash y recover_meeting: si falta una clave,
    la reunion descartada no se puede recuperar desde el panel."""
    tray._discard_job(reunion_completa['wav'], 'test', reunion_completa['md'])
    meta_f = next(reunion_completa['trash'].iterdir()) / '_trash_meta.json'
    assert clave in json.loads(meta_f.read_text(encoding='utf-8'))


def test_cada_fichero_registra_su_carpeta_de_origen(tray, reunion_completa):
    """orig_dir es lo UNICO que permite a recover_meeting deshacer el
    movimiento: sin el, el fichero no sabe volver a su sitio."""
    tray._discard_job(reunion_completa['wav'], 'test', reunion_completa['md'])
    meta_f = next(reunion_completa['trash'].iterdir()) / '_trash_meta.json'
    meta = json.loads(meta_f.read_text(encoding='utf-8'))
    assert all('name' in f and 'orig_dir' in f for f in meta['files'])


def test_el_meta_marca_que_fue_una_cancelacion(tray, reunion_completa):
    tray._discard_job(reunion_completa['wav'], 'descartada en cola', reunion_completa['md'])
    meta_f = next(reunion_completa['trash'].iterdir()) / '_trash_meta.json'
    meta = json.loads(meta_f.read_text(encoding='utf-8'))
    assert meta.get('cancelled') is True
    assert meta.get('reason') == 'descartada en cola'


def test_la_fecha_y_hora_se_derivan_del_stem(tray, reunion_completa):
    tray._discard_job(reunion_completa['wav'], 'test', reunion_completa['md'])
    meta_f = next(reunion_completa['trash'].iterdir()) / '_trash_meta.json'
    meta = json.loads(meta_f.read_text(encoding='utf-8'))
    assert (meta['date'], meta['time']) == ('2026-09-16', '12:00')


def test_descartar_dos_veces_el_mismo_stem_no_colisiona(tray, reunion_completa, wav_factory):
    tray._discard_job(reunion_completa['wav'], 'primera', reunion_completa['md'])
    otra_vez = wav_factory(ta.RECORDINGS_DIR / f'{STEM}.wav', seconds=0.2)
    tray._discard_job(otra_vez, 'segunda')

    nombres = sorted(d.name for d in reunion_completa['trash'].iterdir())
    assert len(nombres) == 2, nombres
    assert nombres[1].endswith('__2')


def test_descartar_sin_minutas_no_lanza(tray, tr_dirs, wav_factory):
    """El caso normal al cancelar en cola: aun no hay minutas."""
    wav = wav_factory(ta.RECORDINGS_DIR / f'{STEM}.wav', seconds=0.2)
    tray._discard_job(wav, 'descartada en cola')
    assert not wav.exists()
    assert (tr_dirs / 'trash').is_dir()


def test_la_senal_se_limpia_tras_descartar(tray, reunion_completa):
    """Si no se limpiara, el siguiente trabajo con el mismo stem naceria ya
    cancelado."""
    tray._pipeline_queued = [STEM]
    tray.cancel_job(STEM)
    tray._discard_job(reunion_completa['wav'], 'test', reunion_completa['md'])
    assert not tray._is_cancelled(STEM)


def test_tras_descartar_no_queda_trabajo_activo(tray, reunion_completa):
    tray._current_job = {'stem': STEM}
    tray._discard_job(reunion_completa['wav'], 'test', reunion_completa['md'])
    assert tray._active_job_stem() == ''


# ── Que trabajo descarta el menu ───────────────────────────────────────────

def test_sin_trabajos_no_hay_nada_que_descartar(tray):
    assert tray._active_job_stem() == ''


def test_con_cola_se_descarta_el_primero(tray):
    tray._pipeline_queued = ['en-cola-1', 'en-cola-2']
    assert tray._active_job_stem() == 'en-cola-1'


def test_el_que_se_procesa_tiene_prioridad_sobre_la_cola(tray):
    tray._pipeline_queued = ['en-cola-1']
    tray._current_job = {'stem': 'en-proceso'}
    assert tray._active_job_stem() == 'en-proceso'


def test_un_current_job_sin_stem_no_rompe(tray):
    tray._current_job = {'title': 'sin stem'}
    tray._pipeline_queued = ['en-cola-1']
    assert tray._active_job_stem() == 'en-cola-1'


def test_active_job_stem_nunca_devuelve_none(tray):
    tray._current_job = None
    tray._pipeline_queued = []
    assert tray._active_job_stem() == ''
