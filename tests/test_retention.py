"""La retencion de audio no puede borrar una reunion que todavia no se proceso.

cleanup_old_recordings corre al arrancar (main.py), justo antes de que
_recover_pending reencole los WAV de recordings/ que no tienen minutas. Con la
retencion en 10 dias (8b8822e), una reunion que fallo o que se grabo con el
portatil a punto de apagarse y no se abrio en 10 dias se borraba sin haberse
transcrito.
"""
import os
import time

import pytest

import storage

pytestmark = pytest.mark.unit

STEM = '2026-10-01_10-00_Comite'
VIEJO = time.time() - 30 * 86400


def envejecer(path):
    path.write_bytes(b'RIFF')
    os.utime(path, (VIEJO, VIEJO))
    return path


def test_un_audio_viejo_sin_minutas_no_se_borra(tr_dirs):
    wav = envejecer(storage.RECORDINGS_DIR / f'{STEM}.wav')
    pistas = [envejecer(storage.RECORDINGS_DIR / f'{STEM}.{t}.wav') for t in ('mic', 'loop')]
    storage.cleanup_old_recordings()
    assert wav.exists() and all(p.exists() for p in pistas)


def test_un_audio_viejo_con_minutas_si_se_borra(tr_dirs):
    wav = envejecer(storage.RECORDINGS_DIR / f'{STEM}.wav')
    storage.MINUTES_DIR.mkdir(parents=True, exist_ok=True)
    (storage.MINUTES_DIR / '20261001_1000_Comite.md').write_text('# Comite', encoding='utf-8')
    storage.cleanup_old_recordings()
    assert not wav.exists()


def test_lo_procesado_se_borra_por_antiguo(tr_dirs):
    processed = storage.RECORDINGS_DIR / 'processed'
    processed.mkdir(parents=True, exist_ok=True)
    wav = envejecer(processed / f'{STEM}.wav')
    storage.cleanup_old_recordings()
    assert not wav.exists()


def test_los_auxiliares_viejos_se_borran_aunque_no_haya_minutas(tr_dirs):
    aux = envejecer(storage.RECORDINGS_DIR / f'{STEM}.lang')
    storage.cleanup_old_recordings()
    assert not aux.exists()


def test_lo_reciente_no_se_toca(tr_dirs):
    wav = storage.RECORDINGS_DIR / f'{STEM}.wav'
    wav.write_bytes(b'RIFF')
    storage.MINUTES_DIR.mkdir(parents=True, exist_ok=True)
    (storage.MINUTES_DIR / '20261001_1000_Comite.md').write_text('# Comite', encoding='utf-8')
    storage.cleanup_old_recordings()
    assert wav.exists()
