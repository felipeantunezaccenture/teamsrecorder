"""Titulos con corchetes: "[EXT] Weekly sync" no debe romper las busquedas.

POR QUE EXISTE (revision independiente del 20/10/2026)
storage._slugify conserva [ y ], asi que una reunion de Teams "[EXT] Weekly
sync" deja el stem 2026-10-20_10-00_[EXT]_Weekly_sync. Path.glob lee "[EXT]"
como una clase de caracteres (una E, una X o una T) y no encuentra el fichero.
_discard_job no mandaba nada a la papelera, borraba igual la senal de
cancelacion, y al siguiente arranque _recover_pending volvia a encolar el WAV:
minutas de una reunion que el usuario habia descartado.

Los corchetes del NOMBRE del fichero no molestan; solo rompen si estan en la
parte literal del PATRON. Por eso las busquedas que montan el patron con los
digitos de la fecha ("2026-10-20_10-00*_transcript.txt") no necesitan nada.
"""
import queue

import pytest

import tray_app as ta

pytestmark = pytest.mark.unit

STEM = '2026-10-20_10-00_[EXT]_Weekly_sync'    # lo que deja _slugify('[EXT] Weekly sync')


@pytest.mark.parametrize('carpeta', ['recordings', 'processed'])
def test_descartar_manda_a_la_papelera_el_wav_y_el_transcript(tray, tr_dirs, wav_factory,
                                                               carpeta):
    folder = ta.RECORDINGS_DIR if carpeta == 'recordings' else ta.RECORDINGS_DIR / 'processed'
    wav = wav_factory(folder / f'{STEM}.wav', seconds=0.1)
    transcript = folder / f'{STEM}_transcript.txt'
    transcript.write_text('texto', encoding='utf-8')

    tray._discard_job(wav, 'test')

    assert not wav.exists() and not transcript.exists()
    destino = next((tr_dirs / 'trash').iterdir())
    assert {f'{STEM}.wav', f'{STEM}_transcript.txt'} <= {p.name for p in destino.iterdir()}


def test_una_reunion_descartada_no_vuelve_a_la_cola_al_arrancar(tray, wav_factory, monkeypatch):
    wav = wav_factory(ta.RECORDINGS_DIR / f'{STEM}.wav', seconds=0.1)
    tray._discard_job(wav, 'test')

    monkeypatch.setattr(ta.time, 'sleep', lambda _s: None)
    monkeypatch.setattr(tray, '_notify', lambda *_a: None)
    tray._pipeline_queue = queue.Queue()
    tray._recover_pending()
    assert list(tray._pipeline_queue.queue) == []


def test_get_transcript_text_encuentra_un_transcript_con_corchetes(tr_dirs):
    import app_window
    (ta.RECORDINGS_DIR / f'{STEM}.en.txt').write_text('hola', encoding='utf-8')
    assert app_window.AppAPI().get_transcript_text(str(ta.MINUTES_DIR / f'{STEM}.md')) == 'hola'
