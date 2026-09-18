"""Edicion de minutas y acciones desde el panel web.

Son las operaciones en las que el usuario escribe algo suyo. Un fallo aqui no
deja un fichero regenerable a medias: le borra texto que acaba de escribir.

El invariante que gobierna todo el fichero: renombrar o editar una reunion NO
renombra el .md. El titulo vive DENTRO del contenido, para no perder pins ni
romper los enlaces del consumidor externo.
"""
import json

import pytest

import app_window as aw

pytestmark = pytest.mark.unit

STEM = '20260917_1616_Comite_Semanal'

MINUTA = """TITULO: Comite Semanal

## Resumen Ejecutivo

Se hablo del deck.

## Acciones Pendientes

| Accion | Owner |
|---|---|
| Enviar la propuesta | Felipe |
"""


@pytest.fixture
def api():
    return object.__new__(aw.AppAPI)


@pytest.fixture
def minuta(tr_dirs):
    md = aw.MINUTES_DIR / f'{STEM}.md'
    md.write_text(MINUTA, encoding='utf-8')
    return md


@pytest.fixture
def sin_html(monkeypatch):
    """save_minutes_notes regenera el HTML; aqui no interesa."""
    import html_exporter
    monkeypatch.setattr(html_exporter, 'export_to_html', lambda *a, **k: None)


# ── Renombrar una reunion ────────────────────────────────────────────────

def test_renombrar_sustituye_el_titulo_existente(api, minuta):
    assert api.rename_meeting(str(minuta), 'Nombre Nuevo') is True

    texto = minuta.read_text(encoding='utf-8')
    assert texto.startswith('TITULO: Nombre Nuevo')
    assert 'Comite Semanal' not in texto


def test_renombrar_no_duplica_la_linea_de_titulo(api, minuta):
    api.rename_meeting(str(minuta), 'Uno')
    api.rename_meeting(str(minuta), 'Dos')

    assert minuta.read_text(encoding='utf-8').count('TITULO:') == 1


def test_renombrar_una_minuta_sin_titulo_lo_anade_delante(api, tr_dirs):
    md = aw.MINUTES_DIR / f'{STEM}.md'
    md.write_text('## Resumen\n\ncuerpo', encoding='utf-8')

    api.rename_meeting(str(md), 'Titulo Puesto')

    texto = md.read_text(encoding='utf-8')
    assert texto.startswith('TITULO: Titulo Puesto\n\n')
    assert '## Resumen' in texto


def test_renombrar_NO_renombra_el_fichero(api, minuta):
    """Es deliberado: el nombre del fichero es la clave del pin y lo que lee
    el consumidor externo. Cambiarlo rompe las dos cosas."""
    api.rename_meeting(str(minuta), 'Nombre Completamente Distinto')
    assert minuta.exists()
    assert minuta.stem == STEM


def test_renombrar_recorta_los_espacios(api, minuta):
    api.rename_meeting(str(minuta), '   Con Espacios   ')
    assert minuta.read_text(encoding='utf-8').startswith('TITULO: Con Espacios\n')


def test_renombrar_una_minuta_inexistente_devuelve_false(api, tr_dirs):
    assert api.rename_meeting(str(aw.MINUTES_DIR / 'no_existe.md'), 'X') is False


def test_el_resto_del_contenido_sobrevive_al_renombrado(api, minuta):
    api.rename_meeting(str(minuta), 'Otro')
    texto = minuta.read_text(encoding='utf-8')
    assert 'Se hablo del deck' in texto
    assert 'Enviar la propuesta' in texto


# ── Guardar las notas editadas ───────────────────────────────────────────

def test_guardar_notas_PRESERVA_la_seccion_de_acciones(api, minuta, sin_html):
    """EL INVARIANTE CRITICO DE ESTE FICHERO. El editor visual solo muestra las
    notas; la tabla de acciones vive en 'Gestionar acciones'. Si al guardar se
    perdiera, el usuario borraria las acciones de la reunion sin saberlo."""
    api.save_minutes_notes(str(minuta), 'TITULO: Comite\n\n## Resumen\n\nTexto nuevo.')

    texto = minuta.read_text(encoding='utf-8')
    assert 'Texto nuevo' in texto
    assert '## Acciones Pendientes' in texto
    assert 'Enviar la propuesta' in texto


def test_guardar_notas_reemplaza_el_cuerpo(api, minuta, sin_html):
    api.save_minutes_notes(str(minuta), 'solo esto')
    texto = minuta.read_text(encoding='utf-8')
    assert 'Se hablo del deck' not in texto
    assert 'solo esto' in texto


def test_si_no_habia_seccion_de_acciones_no_se_inventa(api, tr_dirs, sin_html):
    md = aw.MINUTES_DIR / f'{STEM}.md'
    md.write_text('TITULO: X\n\n## Resumen\n\ncuerpo', encoding='utf-8')

    api.save_minutes_notes(str(md), 'nuevo cuerpo')

    assert 'Acciones Pendientes' not in md.read_text(encoding='utf-8')


def test_la_seccion_de_acciones_en_ingles_tambien_se_preserva(api, tr_dirs, sin_html):
    md = aw.MINUTES_DIR / f'{STEM}.md'
    md.write_text('TITULO: X\n\n## Summary\n\ny\n\n## Pending Actions\n\n| A |\n',
                  encoding='utf-8')

    api.save_minutes_notes(str(md), 'nuevo cuerpo')

    assert '## Pending Actions' in md.read_text(encoding='utf-8')


def test_la_seccion_preservada_corta_en_la_siguiente_cabecera(api, tr_dirs, sin_html):
    """Si arrastrara todo hasta el final, duplicaria las secciones que vienen
    despues de las acciones."""
    md = aw.MINUTES_DIR / f'{STEM}.md'
    md.write_text('TITULO: X\n\n## Resumen\n\ny\n\n'
                  '## Acciones Pendientes\n\n| A |\n\n'
                  '## Notas Finales\n\nno deberia duplicarse\n', encoding='utf-8')

    api.save_minutes_notes(str(md), 'nuevo cuerpo')

    texto = md.read_text(encoding='utf-8')
    assert texto.count('no deberia duplicarse') == 0


def test_guardar_notas_regenera_el_html(api, minuta, monkeypatch):
    """Sin esto, el panel mostraria las notas nuevas y el HTML exportado
    seguiria con las viejas."""
    regenerados = []
    import html_exporter
    monkeypatch.setattr(html_exporter, 'export_to_html',
                        lambda p, t, **k: regenerados.append((p, t)))

    api.save_minutes_notes(str(minuta), 'texto nuevo')

    assert len(regenerados) == 1
    assert regenerados[0][1] == 'Comite Semanal', "deberia pasar el titulo del stem"


def test_si_la_regeneracion_del_html_falla_las_notas_se_guardan_igual(api, minuta,
                                                                      monkeypatch):
    """El texto del usuario es lo importante; el HTML se puede regenerar
    despues."""
    import html_exporter
    monkeypatch.setattr(html_exporter, 'export_to_html',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('boom')))

    assert api.save_minutes_notes(str(minuta), 'texto nuevo') is True
    assert 'texto nuevo' in minuta.read_text(encoding='utf-8')


def test_guardar_notas_sobre_un_fichero_nuevo_funciona(api, tr_dirs, sin_html):
    md = aw.MINUTES_DIR / f'{STEM}.md'
    assert api.save_minutes_notes(str(md), 'contenido nuevo') is True
    assert md.read_text(encoding='utf-8').startswith('contenido nuevo')


# ── El HTML que pinta el panel ───────────────────────────────────────────

def test_el_html_del_panel_quita_la_tabla_de_acciones(api, minuta):
    """La tabla se gestiona en su propia pantalla: duplicarla en las notas
    confunde sobre donde hay que editarla."""
    html = api.get_minutes_html(str(minuta))

    assert 'Se hablo del deck' in html
    assert 'Enviar la propuesta' not in html


def test_el_html_del_panel_quita_tambien_la_seccion_en_ingles(api, tr_dirs):
    md = aw.MINUTES_DIR / f'{STEM}.md'
    md.write_text('## Summary\n\nvisible\n\n## Pending Actions\n\n| oculto |\n',
                  encoding='utf-8')

    html = api.get_minutes_html(str(md))

    assert 'visible' in html
    assert 'oculto' not in html


def test_un_fichero_inexistente_devuelve_un_mensaje_y_no_lanza(api, tr_dirs):
    """Contrato con el JS: espera HTML, no una excepcion."""
    html = api.get_minutes_html(str(aw.MINUTES_DIR / 'no_existe.md'))
    assert 'no encontrado' in html.lower()


def test_el_html_del_panel_convierte_el_markdown(api, minuta):
    html = api.get_minutes_html(str(minuta))
    assert '<h2' in html


# ── Acciones: crear, actualizar, borrar ──────────────────────────────────

@pytest.fixture
def con_acciones(minuta):
    (aw.MINUTES_DIR / f'{STEM}_actions.json').write_text(json.dumps({
        'minutes': str(minuta), 'project_id': 'none',
        'actions': [
            {'index': 0, 'title': 'Primera', 'prompt_enriched': 'p0',
             'type': 'instruction', 'executed': False},
            {'index': 1, 'title': 'Segunda', 'prompt_enriched': 'p1',
             'type': 'human', 'executed': False},
        ],
    }), encoding='utf-8')
    return minuta


def _acciones(minuta):
    return json.loads(
        (aw.MINUTES_DIR / f'{STEM}_actions.json').read_text(encoding='utf-8'))['actions']


def test_crear_una_accion_la_anade_al_final(api, con_acciones):
    api.create_action(str(con_acciones), 'Tercera puesta a mano')

    titulos = [a['title'] for a in _acciones(con_acciones)]
    assert titulos[-1] == 'Tercera puesta a mano'


def test_no_hay_indices_repetidos_dentro_del_fichero(api, con_acciones):
    """El indice es la identidad de la accion: dos iguales harian que editar
    una editara la otra."""
    api.create_action(str(con_acciones), 'Tercera')

    indices = [a['index'] for a in _acciones(con_acciones)]
    assert len(indices) == len(set(indices)), f"indices repetidos: {indices}"


def test_DEFECTO_el_indice_del_ultimo_borrado_SI_se_reutiliza(api, con_acciones):
    """DEFECTO ENCONTRADO AL MEDIRLO (18/09/2026), y corrige lo que afirmaba el
    analisis previo.

    El siguiente indice es `max(index) + 1` sobre las acciones que QUEDAN, asi
    que borrar la ultima libera su numero y la siguiente accion lo reutiliza.
    Si el panel tenia una referencia a ese indice sin refrescar, apunta ahora a
    una accion distinta: el usuario edita o ejecuta la que no queria.

    Se fija el comportamiento actual."""
    api.delete_action(str(con_acciones), 1)
    api.create_action(str(con_acciones), 'Nueva')

    indices = [a['index'] for a in _acciones(con_acciones)]
    assert indices == [0, 1], f"si esto cambia, ya no se reutilizan: {indices}"
    assert _acciones(con_acciones)[1]['title'] == 'Nueva'


def test_actualizar_una_accion_recorta_los_espacios(api, con_acciones):
    """La firma es (path, index, title, assignee, deadline): campos sueltos, no
    un dict."""
    api.update_action(str(con_acciones), 0, '  Con Espacios  ', '  Felipe  ')

    accion = _acciones(con_acciones)[0]
    assert accion['title'] == 'Con Espacios'
    assert accion['assignee'] == 'Felipe'


def test_DEFECTO_actualizar_un_indice_inexistente_informa_exito(api, con_acciones):
    """DEFECTO menor: devuelve True aunque no haya encontrado la accion, asi
    que el panel cree que guardo el cambio cuando no cambio nada. Pasa si el
    panel tiene la lista sin refrescar y el indice ya no existe."""
    assert api.update_action(str(con_acciones), 99, 'X') is True
    assert [a['title'] for a in _acciones(con_acciones)] == ['Primera', 'Segunda']


def test_actualizar_sin_actions_json_devuelve_false(api, minuta):
    assert api.update_action(str(minuta), 0, 'X') is False


def test_borrar_una_accion_la_quita(api, con_acciones):
    api.delete_action(str(con_acciones), 0)
    assert [a['title'] for a in _acciones(con_acciones)] == ['Segunda']


def test_borrar_un_indice_inexistente_no_lanza(api, con_acciones):
    api.delete_action(str(con_acciones), 99)
    assert len(_acciones(con_acciones)) == 2


def test_las_acciones_de_una_minuta_sin_json_son_lista_vacia(api, minuta):
    assert api.get_actions(str(minuta)) == []


# ── Notas adhesivas ─────────────────────────────────────────────────────

def test_las_notas_adhesivas_hacen_round_trip(api, minuta):
    notas = [{'id': '1', 'text': 'acordarse de esto', 'x': 10, 'y': 20}]

    api.save_stickies(str(minuta), notas)

    assert api.get_stickies(str(minuta)) == notas


def test_sin_notas_adhesivas_devuelve_lista_vacia(api, minuta):
    assert api.get_stickies(str(minuta)) == []


def test_DEFECTO_guardar_notas_adhesivas_no_informa_de_errores(api, minuta,
                                                                monkeypatch):
    """DEFECTO: save_stickies se traga cualquier excepcion y devuelve None, asi
    que el JS nunca sabe si se guardo. El usuario cree que sus notas estan a
    salvo y puede que no."""
    def boom(*_a, **_k):
        raise OSError('disco lleno')

    monkeypatch.setattr(type(minuta), 'write_text', boom)

    resultado = api.save_stickies(str(minuta), [{'text': 'se perdera'}])

    assert resultado is None, "si devuelve un bool, el defecto se arreglo"
