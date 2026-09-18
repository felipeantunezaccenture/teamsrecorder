"""La unica funcion testeable de actions_ui.

`actions_ui.py` son 595 lineas de tkinter, salvo `_md_to_html`, que es una
funcion de modulo y no necesita ventana. Genera el HTML que se muestra al
previsualizar una accion en la ventana de acciones.

El resto del modulo queda fuera a proposito: un test ahi probaria tkinter.
"""
import sys

import pytest

import actions_ui

pytestmark = pytest.mark.unit


def test_devuelve_un_documento_html_completo():
    """Se carga en un visor embebido: necesita su propio <html> y sus estilos,
    no hay hoja externa que aplicar."""
    html = actions_ui._md_to_html('## Titulo\n\ntexto')

    assert html.lstrip().startswith('<html>')
    assert '</html>' in html
    assert '<style>' in html


def test_el_markdown_se_convierte():
    html = actions_ui._md_to_html('## Titulo\n\n- uno\n- dos\n')
    assert '<h2>' in html
    assert html.count('<li>') == 2


def test_las_tablas_se_convierten():
    html = actions_ui._md_to_html('| A | B |\n|---|---|\n| 1 | 2 |\n')
    assert '<table>' in html


def test_los_bloques_de_codigo_se_convierten():
    html = actions_ui._md_to_html('```python\nprint(1)\n```')
    assert '<code' in html or '<pre' in html


def test_sin_la_libreria_markdown_el_texto_no_se_pierde(monkeypatch):
    """Degradacion elegante: markdown es opcional. Mejor el texto crudo dentro
    de un <pre> que una ventana en blanco."""
    monkeypatch.setitem(sys.modules, 'markdown', None)

    html = actions_ui._md_to_html('## Titulo\n\ntexto importante')

    assert 'texto importante' in html
    assert '<pre>' in html


def test_un_markdown_vacio_no_revienta():
    assert '<html>' in actions_ui._md_to_html('')
