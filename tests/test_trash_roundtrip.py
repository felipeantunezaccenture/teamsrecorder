"""Papelera de punta a punta: descartar -> ver en el panel -> recuperar.

Dos rutas distintas escriben en `trash/` y la web tiene que entender las dos:

  tray_app._discard_job    -> stem del WAV     (2026-09-16_12-00_Reunion)
  app_window.delete_meeting -> stem de minutas (20260916_1200_Reunion)

Si `list_trash` no sabe leer una de las dos, esa reunion aparece sin fecha ni
titulo en el panel y el usuario no puede recuperarla.
"""
import json

import pytest

import app_window as aw
import tray_app as ta

pytestmark = pytest.mark.unit

WAV_STEM = '2026-09-16_12-00_Reunion_Descartable'
MD_STEM = '20260916_1200_Reunion_Descartable'


@pytest.fixture
def api():
    """La clase API de pywebview, pero sin pywebview: sus metodos de papelera
    no lo tocan, solo mueven ficheros."""
    return object.__new__(aw.AppAPI)


@pytest.fixture
def reunion(tr_dirs, wav_factory):
    """Los cuatro artefactos de una reunion procesada."""
    wav = wav_factory(ta.RECORDINGS_DIR / f'{WAV_STEM}.wav', seconds=0.2)
    (ta.RECORDINGS_DIR / f'{WAV_STEM}_transcript.txt').write_text('t', encoding='utf-8')
    md = ta.MINUTES_DIR / f'{MD_STEM}.md'
    md.write_text('TITULO: Reunion Descartable\n\ncontenido', encoding='utf-8')
    md.with_suffix('.html').write_text('<html></html>', encoding='utf-8')
    (ta.MINUTES_DIR / f'{MD_STEM}_actions.json').write_text(
        json.dumps({'project_id': 'none', 'actions': []}), encoding='utf-8')
    (ta.MINUTES_DIR / f'{MD_STEM}_transcript.txt').write_text('t', encoding='utf-8')
    return {'wav': wav, 'md': md, 'trash': tr_dirs / 'trash'}


# ── La papelera vacia ─────────────────────────────────────────────────────

def test_sin_papelera_la_lista_esta_vacia(api, tr_dirs):
    assert api.list_trash() == []


def test_una_carpeta_sin_meta_se_ignora(api, tr_dirs):
    """Basura en trash/ no debe romper el panel."""
    (tr_dirs / 'trash' / 'suelta').mkdir(parents=True)
    assert api.list_trash() == []


# ── Ruta 1: descartar desde el pipeline (stem del WAV) ───────────────────

def test_lo_descartado_por_el_pipeline_aparece_en_el_panel(tray, api, reunion):
    tray._discard_job(reunion['wav'], 'descartada en cola', reunion['md'])

    items = api.list_trash()
    assert len(items) == 1
    assert items[0]['cancelled'] is True
    assert items[0]['reason'] == 'descartada en cola'


def test_el_panel_lee_bien_la_fecha_del_stem_del_wav(tray, api, reunion):
    tray._discard_job(reunion['wav'], 'test', reunion['md'])
    item = api.list_trash()[0]
    assert (item['date'], item['time']) == ('2026-09-16', '12:00')


def test_list_trash_anade_id_y_numero_de_ficheros(tray, api, reunion):
    """Las dos claves que el JS necesita y que no estan en el meta escrito."""
    tray._discard_job(reunion['wav'], 'test', reunion['md'])
    item = api.list_trash()[0]
    assert item['id']
    assert item['file_count'] >= 4


# ── Ruta 2: borrar desde el panel (stem de minutas) ──────────────────────

def test_lo_borrado_desde_el_panel_aparece_en_el_panel(api, reunion):
    assert api.delete_meeting(str(reunion['md'])) is True

    items = api.list_trash()
    assert len(items) == 1
    assert items[0]['title'] == 'Reunion Descartable'


def test_el_panel_lee_bien_la_fecha_del_stem_de_minutas(api, reunion):
    api.delete_meeting(str(reunion['md']))
    item = api.list_trash()[0]
    assert (item['date'], item['time']) == ('2026-09-16', '12:00')


def test_borrar_desde_el_panel_se_lleva_tambien_el_wav(api, reunion):
    """El stem de minutas no casa con el del WAV: hay que reformatear la fecha
    para encontrarlo. Si eso se rompe, el audio se queda suelto en disco."""
    api.delete_meeting(str(reunion['md']))

    assert not reunion['wav'].exists()
    nombres = {f['name'] for f in api.list_trash()[0]['files']}
    assert f'{WAV_STEM}.wav' in nombres


def test_borrar_desde_el_panel_no_marca_cancelled(api, reunion):
    """Diferencia real entre las dos rutas: borrar a mano no es cancelar un
    trabajo, y el panel lo distingue."""
    api.delete_meeting(str(reunion['md']))
    assert 'cancelled' not in api.list_trash()[0]


# ── El round-trip completo ────────────────────────────────────────────────

def test_recuperar_devuelve_cada_fichero_a_su_carpeta(api, reunion):
    """El test estrella: sin esto, 'recuperar' es un boton que promete algo
    que no cumple."""
    api.delete_meeting(str(reunion['md']))
    item_id = api.list_trash()[0]['id']

    assert api.recover_meeting(item_id) is True

    assert reunion['md'].exists()
    assert reunion['md'].with_suffix('.html').exists()
    assert (ta.MINUTES_DIR / f'{MD_STEM}_actions.json').exists()
    assert reunion['wav'].exists()
    assert api.list_trash() == []


def test_recuperar_dos_veces_devuelve_false(api, reunion):
    api.delete_meeting(str(reunion['md']))
    item_id = api.list_trash()[0]['id']
    api.recover_meeting(item_id)
    assert api.recover_meeting(item_id) is False


def test_recuperar_algo_inexistente_devuelve_false(api, tr_dirs):
    assert api.recover_meeting('no-existe') is False


def test_purgar_borra_definitivamente(api, reunion):
    api.delete_meeting(str(reunion['md']))
    item_id = api.list_trash()[0]['id']

    assert api.purge_trash_meeting(item_id) is True
    assert api.list_trash() == []
    assert not reunion['md'].exists(), "purgar no debe restaurar nada"


def test_purgar_dos_veces_devuelve_false(api, reunion):
    api.delete_meeting(str(reunion['md']))
    item_id = api.list_trash()[0]['id']
    api.purge_trash_meeting(item_id)
    assert api.purge_trash_meeting(item_id) is False


# ── Orden y colisiones ────────────────────────────────────────────────────

def test_las_mas_recientes_primero(api, tray, reunion, wav_factory, tr_dirs):
    tray._discard_job(reunion['wav'], 'primera', reunion['md'])
    otro_wav = wav_factory(ta.RECORDINGS_DIR / '2026-09-17_09-00_Otra.wav', seconds=0.1)
    tray._discard_job(otro_wav, 'segunda')

    fechas = [i['deleted_at'] for i in api.list_trash()]
    assert fechas == sorted(fechas, reverse=True)


def test_descartar_el_mismo_stem_dos_veces_crea_dos_entradas(api, tray, reunion,
                                                             wav_factory):
    tray._discard_job(reunion['wav'], 'primera', reunion['md'])
    otra_vez = wav_factory(ta.RECORDINGS_DIR / f'{WAV_STEM}.wav', seconds=0.1)
    tray._discard_job(otra_vez, 'segunda')

    ids = sorted(i['id'] for i in api.list_trash())
    assert len(ids) == 2
    assert ids[1].endswith('__2'), f"anticolision roto: {ids}"


# ── Defectos conocidos, fijados para poder arreglarlos ───────────────────

def test_DEFECTO_un_orig_dir_vacio_restaura_en_el_directorio_de_trabajo(api, tr_dirs,
                                                                        monkeypatch):
    """DEFECTO (app_window.py:820): la guarda `if not str(dst_dir)` es una rama
    MUERTA, porque str(Path('')) es '.' y siempre es truthy. Con un orig_dir
    vacio el fichero se restaura en el cwd del proceso en vez de saltarse.

    Se ejecuta con el cwd apuntando a tmp_path para no ensuciar nada."""
    trash_dir = tr_dirs / 'trash' / 'manipulada'
    trash_dir.mkdir(parents=True)
    (trash_dir / 'suelto.md').write_text('x', encoding='utf-8')
    (trash_dir / '_trash_meta.json').write_text(json.dumps({
        'stem': 'x', 'title': 'X', 'date': '', 'time': '', 'deleted_at': '2026-01-01',
        'files': [{'name': 'suelto.md', 'orig_dir': ''}],
    }), encoding='utf-8')

    destino_falso = tr_dirs / 'cwd'
    destino_falso.mkdir()
    monkeypatch.chdir(destino_falso)

    assert api.recover_meeting('manipulada') is True
    assert (destino_falso / 'suelto.md').exists(), \
        "si esto cambia, la rama muerta se arreglo"


def test_DEFECTO_recuperar_borra_la_papelera_aunque_falle_un_movimiento(api, tr_dirs,
                                                                        monkeypatch):
    """DEFECTO (app_window.py:829): el rmtree corre INCONDICIONALMENTE tras el
    bucle. Si un move falla, el error se traga, la carpeta de papelera se borra
    igual y el fichero se pierde en silencio. Y encima devuelve True."""
    trash_dir = tr_dirs / 'trash' / 'fallona'
    trash_dir.mkdir(parents=True)
    (trash_dir / 'a.md').write_text('x', encoding='utf-8')
    destino = tr_dirs / 'minutes'
    (trash_dir / '_trash_meta.json').write_text(json.dumps({
        'stem': 'a', 'title': 'A', 'date': '', 'time': '', 'deleted_at': '2026-01-01',
        'files': [{'name': 'a.md', 'orig_dir': str(destino)}],
    }), encoding='utf-8')

    def move_que_falla(*_a, **_k):
        raise OSError('destino ocupado')

    monkeypatch.setattr(aw.shutil, 'move', move_que_falla)

    resultado = api.recover_meeting('fallona')

    assert resultado is True, "informa exito pese al fallo"
    assert not trash_dir.exists(), "y borra la papelera, perdiendo el fichero"
    assert not (destino / 'a.md').exists()
