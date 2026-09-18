"""Deteccion de llamadas: fin de llamada y nombrado de la grabacion.

INCIDENTES QUE ORIGINARON ESTOS TESTS
- 07/09/2026: Teams deja abiertas ventanas de reuniones ya terminadas con el
  mismo formato exacto de titulo. Una de ellas mantuvo una grabacion en marcha
  indefinidamente, y otra dio su nombre a una grabacion distinta: una call se
  guardo con el nombre de una reunion de dos horas antes.
- 16/09/2026: el aviso de titulo residual se escribia en cada poll, 3,7 MB de
  log en dos dias.

NOTA SOBRE LOS TIEMPOS
Estos tests dependen de polls reales, asi que llevan el marker `slow`. Hay dos
formas de esperar y confundirlas es un bug con el que ya se tropezo:
  espera_polls()      -> con margen, para "esto YA deberia haber pasado"
  espera_como_maximo() -> sin margen, para "esto TODAVIA no debe pasar"
El margen aditivo son ~16 polls extra; usarlo en el segundo caso hace que el
bache de audio se pase del umbral y el test falle sin que el codigo tenga
nada mal.
"""
import time

import pytest

import teams_detector as td

pytestmark = [pytest.mark.unit, pytest.mark.slow]

POLL = 0.05


def espera_polls(n):
    """Espera n polls CON margen: para aserciones de 'ya deberia haber pasado'."""
    time.sleep(POLL * n + 0.8)


def espera_como_maximo(n):
    """Espera n polls SIN margen: para aserciones de 'todavia NO debe pasar'.

    El bucle del detector duerme POLL en cada iteracion, asi que dormir POLL*n
    garantiza como maximo n+1 polls.
    """
    time.sleep(POLL * n)


@pytest.fixture
def teams(monkeypatch):
    """Simula el entorno de Teams: titulos de ventana, audio y microfono."""
    estado = {'titles': ['Mi Reunion'], 'audio': True, 'mic': True}

    def titulos(_pids):
        cands = list(estado['titles'])
        if not cands:
            return (False, False, None, [])
        return (False, True, cands[0], cands)

    monkeypatch.setattr(td, '_teams_pids', lambda: [1234])
    monkeypatch.setattr(td, '_check_audio_session', lambda _p: estado['audio'])
    monkeypatch.setattr(td, '_check_mic_session', lambda _p: estado['mic'])
    monkeypatch.setattr(td, '_check_window_titles', titulos)
    return estado


@pytest.fixture
def detector_factory(teams):
    """Detectores con polls cortos, que se paran solos al terminar el test."""
    creados = []

    def make(stale_polls=5):
        det = td.TeamsCallDetector(poll_interval=POLL, required_confirmations=2)
        det._stale_title_polls = stale_polls
        det.started, det.ended = [], []
        det.on_call_started = lambda: det.started.append(1)
        det.on_call_ended = lambda: det.ended.append(1)
        creados.append(det)
        return det

    yield make
    for det in creados:
        det.stop()


# ── Una ventana residual no eterniza la grabacion ──────────────────────────

def test_una_ventana_residual_no_mantiene_la_grabacion_para_siempre(teams, detector_factory):
    """El titulo mantiene la llamada viva, pero no indefinidamente: sin audio
    ni microfono durante 45 s, la ventana es de una reunion ya terminada."""
    teams.update(titles=['Mi Reunion'], audio=True, mic=True)
    det = detector_factory()
    det.start()
    espera_polls(6)
    assert len(det.started) == 1

    teams.update(audio=False, mic=False)            # se cuelga, la ventana queda
    espera_polls(det._required_end_silent + det._required_end_fast + 4)

    assert len(det.ended) == 1, f"ended={len(det.ended)}, in_call={det.in_call}"


def test_un_bache_de_audio_no_corta_la_grabacion(teams, detector_factory):
    """WebRTC pierde la sesion de audio momentaneamente. Cortar ahi partiria
    la reunion en dos ficheros."""
    teams.update(titles=['Mi Reunion'], audio=True, mic=True)
    det = detector_factory()
    det.start()
    espera_polls(6)

    teams.update(audio=False, mic=False)
    espera_como_maximo(det._required_end_silent - 5)   # bache por debajo del umbral
    teams.update(audio=True, mic=True)
    espera_polls(6)

    assert len(det.ended) == 0
    assert det.in_call


# ── Un titulo residual no da nombre a la grabacion ────────────────────────

def test_una_ventana_sin_audio_no_arranca_una_grabacion(teams, detector_factory):
    teams.update(titles=['Reunion De Esta Manana'], audio=False, mic=False)
    det = detector_factory(stale_polls=5)
    det.start()
    espera_polls(10)
    assert len(det.started) == 0


def test_un_titulo_visto_mucho_sin_llamada_queda_marcado_residual(teams, detector_factory):
    teams.update(titles=['Reunion De Esta Manana'], audio=False, mic=False)
    det = detector_factory(stale_polls=5)
    det.start()
    espera_polls(10)
    assert 'Reunion De Esta Manana' in det._stale_titles


def test_tras_marcar_el_titulo_residual_sigue_detectando_llamadas(teams, detector_factory):
    teams.update(titles=['Reunion De Esta Manana'], audio=False, mic=False)
    det = detector_factory(stale_polls=5)
    det.start()
    espera_polls(10)

    teams.update(audio=True, mic=True)              # empieza una llamada de verdad
    espera_polls(8)

    assert len(det.started) == 1


def test_no_usa_el_nombre_de_la_ventana_vieja(teams, detector_factory):
    """El bug del 07/09: una call se guardo con el nombre de una reunion de dos
    horas antes. Mejor sin nombre que con el equivocado."""
    teams.update(titles=['Reunion De Esta Manana'], audio=False, mic=False)
    det = detector_factory(stale_polls=5)
    det.start()
    espera_polls(10)
    teams.update(audio=True, mic=True)
    espera_polls(8)

    assert det._current_meeting_name != 'Reunion De Esta Manana'


# ── Abrir la reunion y unirse SI da nombre ────────────────────────────────

def test_una_ventana_recien_abierta_si_da_nombre(teams, detector_factory):
    """El caso normal: abres la reunion y te unes acto seguido. El margen de
    20 polls antes de marcar un titulo como residual existe para esto."""
    teams.update(titles=[], audio=False, mic=False)
    det = detector_factory(stale_polls=5)
    det.start()
    espera_polls(8)

    teams.update(titles=['Reunion Nueva'], audio=True, mic=True)
    espera_polls(8)

    assert len(det.started) == 1
    assert det._current_meeting_name == 'Reunion Nueva'


def test_con_una_residual_y_una_nueva_gana_la_nueva(teams, detector_factory):
    teams.update(titles=['Vieja Residual'], audio=False, mic=False)
    det = detector_factory(stale_polls=5)
    det.start()
    espera_polls(10)
    assert 'Vieja Residual' in det._stale_titles

    teams.update(titles=['Vieja Residual', 'Reunion Real'], audio=True, mic=True)
    espera_polls(8)

    assert len(det.started) == 1
    assert det._current_meeting_name == 'Reunion Real'


# ── El aviso de titulo residual no inunda el log ──────────────────────────

def test_avisa_una_vez_por_titulo_no_en_cada_poll(teams, detector_factory, monkeypatch):
    """Una ventana abierta toda la tarde escribia una linea cada 3,2 s: 3,7 MB
    de log que tapaban cualquier otra cosa util."""
    avisos = []
    original = td.log.info

    def espia(msg, *a, **k):
        if 'residual' in str(msg):
            avisos.append(msg)
        return original(msg, *a, **k)

    monkeypatch.setattr(td.log, 'info', espia)

    teams.update(titles=['Ventana Vieja Abierta'], audio=False, mic=False)
    det = detector_factory(stale_polls=5)
    det.start()
    espera_polls(30)

    assert len(avisos) == 1, f"{len(avisos)} avisos en 30 polls"
    assert 'Ventana Vieja Abierta' in det._stale_titles


# ── Umbrales: si alguien los cambia, que se entere ────────────────────────

def test_los_umbrales_mantienen_su_relacion():
    """Detectar tiene que ser mucho mas rapido que marcar un titulo como
    residual, o una reunion recien abierta nunca daria nombre."""
    det = td.TeamsCallDetector()
    assert det._required < det._stale_title_polls
    assert det._required_end_fast < det._required_end_silent
