"""Dos grabaciones en el mismo minuto no pueden pisarse.

INCIDENTE QUE ORIGINO ESTOS TESTS (2026-10-08)
Dos pruebas de "Grabar ahora" a las 14:05 (14:05:08-14:05:22 y 14:05:51-14:06:10)
recibieron el mismo nombre, 2026-10-08_14-05_manual.wav, porque el nombre solo
llega al minuto y get_recording_path no comprobaba si ya existia. La segunda
sobrescribio la primera: quedo un unico WAV de 17,9 s y se perdieron 14 s.

En uso real pasa con dos grabaciones manuales seguidas (todas se llaman
"manual") o con una reconexion a la misma reunion de Teams en el mismo minuto.
"""
from datetime import datetime
from pathlib import Path

import pytest

import storage

pytestmark = pytest.mark.unit

AHORA = datetime(2026, 10, 8, 14, 5, 30)


@pytest.fixture
def reloj(monkeypatch):
    """Congela el reloj de storage: dos llamadas reales pueden caer a ambos
    lados de un cambio de minuto y el test pasaria por suerte."""
    estado = {'ahora': AHORA}

    class Reloj(datetime):
        @classmethod
        def now(cls, tz=None):
            return estado['ahora']

    monkeypatch.setattr(storage, 'datetime', Reloj)
    return estado


def crear(ruta: Path, contenido: bytes = b'audio') -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(contenido)
    return ruta


# ── El incidente ──────────────────────────────────────────────────────────

def test_la_segunda_grabacion_del_minuto_no_pisa_la_primera(tr_dirs, reloj):
    """EL TEST CLAVE: lo que paso a las 14:05."""
    primera = crear(storage.get_recording_path('manual'), b'primera grabacion')
    segunda = storage.get_recording_path('manual')

    assert segunda != primera, "las dos grabaciones recibieron el mismo nombre"
    assert primera.read_bytes() == b'primera grabacion'


def test_el_sufijo_sigue_el_convenio_de_la_papelera(tr_dirs, reloj):
    crear(storage.get_recording_path('manual'))
    assert storage.get_recording_path('manual').name == '2026-10-08_14-05_manual__2.wav'


def test_la_tercera_recibe_el_3(tr_dirs, reloj):
    crear(storage.get_recording_path('manual'))
    crear(storage.get_recording_path('manual'))
    assert storage.get_recording_path('manual').stem.endswith('__3')


# ── Cuenta como ocupado cualquier fichero de esa grabacion ────────────────

@pytest.mark.parametrize('resto', ['.mic.part', '.loop.part'])
def test_mientras_graba_solo_existen_los_temporales_y_cuenta_igual(tr_dirs, reloj, resto):
    """Durante la grabacion el .wav final aun no existe: si solo se mirase ese,
    una segunda grabacion compartiria temporales con la primera."""
    stem = storage.get_recording_path('manual').stem
    crear(storage.RECORDINGS_DIR / f'{stem}{resto}')
    assert storage.get_recording_path('manual').stem == f'{stem}__2'


def test_una_grabacion_ya_procesada_cuenta_igual(tr_dirs, reloj):
    """Tras transcribir, el WAV se va a processed/. Con el mismo nombre, al
    procesar la segunda se sobrescribiria la copia archivada de la primera."""
    stem = storage.get_recording_path('manual').stem
    crear(storage.RECORDINGS_DIR / 'processed' / f'{stem}.wav')
    assert storage.get_recording_path('manual').stem == f'{stem}__2'


@pytest.mark.parametrize('resto', ['.mic.wav', '.loop.wav', '_transcript.txt', '_transcript.lang'])
def test_las_pistas_y_el_transcript_cuentan_igual(tr_dirs, reloj, resto):
    """Si quedase una .loop.wav vieja con el mismo nombre, la diarizacion de la
    grabacion nueva mezclaria la pista de los demas de OTRA reunion."""
    stem = storage.get_recording_path('manual').stem
    crear(storage.RECORDINGS_DIR / f'{stem}{resto}')
    assert storage.get_recording_path('manual').stem == f'{stem}__2'


# ── Y no se inventa colisiones donde no las hay ───────────────────────────

def test_sin_nada_previo_el_nombre_no_cambia(tr_dirs, reloj):
    assert storage.get_recording_path('manual').name == '2026-10-08_14-05_manual.wav'


def test_otro_nombre_en_el_mismo_minuto_no_lleva_sufijo(tr_dirs, reloj):
    crear(storage.get_recording_path('manual'))
    assert storage.get_recording_path('Daily').name == '2026-10-08_14-05_Daily.wav'


def test_el_mismo_nombre_en_otro_minuto_no_lleva_sufijo(tr_dirs, reloj):
    crear(storage.get_recording_path('manual'))
    reloj['ahora'] = datetime(2026, 10, 8, 14, 6, 1)
    assert storage.get_recording_path('manual').name == '2026-10-08_14-06_manual.wav'


def test_una_reunion_cuyo_nombre_empieza_igual_no_cuenta(tr_dirs, reloj):
    """'Daily' no colisiona con 'Daily_Standup': se compara el stem entero."""
    crear(storage.RECORDINGS_DIR / '2026-10-08_14-05_Daily_Standup.wav')
    assert storage.get_recording_path('Daily').name == '2026-10-08_14-05_Daily.wav'


def test_un_nombre_con_corchetes_no_engana_a_la_busqueda(tr_dirs, reloj):
    """Los corchetes son sintaxis de glob: sin escaparlos, '[1]' buscaria '1' y
    no veria el fichero que ya existe."""
    primera = crear(storage.get_recording_path('Sprint [1]'))
    assert storage.get_recording_path('Sprint [1]') != primera


# ── Las reconexiones se siguen agrupando ──────────────────────────────────

def test_una_parte_con_sufijo_sigue_casando_con_su_reunion(tray):
    """La app agrupa las reconexiones a una misma reunion de Teams. Si la parte
    nueva llega como ..._Daily__2, tiene que seguir reconociendose como 'Daily'."""
    tray._register_part_recorded(
        storage.RECORDINGS_DIR / '2026-10-08_14-05_Daily_Digital_Brain__2.wav')
    assert tray.has_pending_session('Daily Digital Brain')
