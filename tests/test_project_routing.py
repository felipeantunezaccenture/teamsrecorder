"""Archivado de una reunion bajo su proyecto.

INCIDENTE QUE ORIGINO ESTOS TESTS (15/09/2026)
7 de 10 reuniones estaban mal archivadas. La causa raiz: dos detectores de
proyecto que podian discrepar. `detect_project()` adivina por palabras clave
al vuelo; el `_actions.json` guarda la decision ya tomada (y editable a mano
desde el panel). El archivado seguia al primero, asi que una reunion
reasignada en el panel se quedaba archivada donde dijo la adivinanza.

La regla que fijan estos tests: el `_actions.json` MANDA SIEMPRE.
"""
import json

import pytest

import project_context as pc

pytestmark = pytest.mark.unit

STEM = '20260915_1000_Reunion_De_Prueba'


@pytest.fixture
def reunion(tr_dirs):
    """Una reunion de juguete con su projects.json y su _actions.json."""
    (tr_dirs / 'projects.json').write_text(json.dumps({'projects': [
        {'id': 'proj-a', 'name': 'Proyecto A'},
        {'id': 'proj-b', 'name': 'Proyecto B'},
    ]}), encoding='utf-8')

    md = pc.MINUTES_DIR / f'{STEM}.md'
    md.write_text('TITULO: Reunion De Prueba\n\n## Resumen Ejecutivo\n\n'
                  'Se hablo de cosas.\n', encoding='utf-8')
    actions = pc.MINUTES_DIR / f'{STEM}_actions.json'

    class Reunion:
        def __init__(self):
            self.md = md
            self.actions = actions

        def set_pid(self, pid):
            actions.write_text(json.dumps({'project_id': pid, 'actions': []}),
                               encoding='utf-8')

        def filed_in(self):
            """Proyectos bajo los que esta archivada. Es la senal que lee el
            consumidor externo: project_docs/<id>/meetings/<stem>.txt"""
            root = tr_dirs / 'project_docs'
            if not root.exists():
                return []
            return sorted(d.name for d in root.iterdir()
                          if d.is_dir() and (d / 'meetings' / f'{STEM}.txt').exists())

    return Reunion()


def test_devuelve_el_project_id_aplicado(reunion):
    reunion.set_pid('proj-a')
    assert pc.sync_meeting_summary(reunion.md) == 'proj-a'


def test_archiva_bajo_el_proyecto_indicado(reunion):
    reunion.set_pid('proj-a')
    pc.sync_meeting_summary(reunion.md)
    assert reunion.filed_in() == ['proj-a']


def test_el_resumen_archivado_lleva_titulo_y_fecha(reunion, tr_dirs):
    reunion.set_pid('proj-a')
    pc.sync_meeting_summary(reunion.md)
    txt = (tr_dirs / 'project_docs' / 'proj-a' / 'meetings' / f'{STEM}.txt').read_text(
        encoding='utf-8')
    assert 'Reunion De Prueba' in txt
    assert '2026-09-15' in txt


def test_el_resumen_archivado_incluye_el_resumen_ejecutivo(reunion, tr_dirs):
    reunion.set_pid('proj-a')
    pc.sync_meeting_summary(reunion.md)
    txt = (tr_dirs / 'project_docs' / 'proj-a' / 'meetings' / f'{STEM}.txt').read_text(
        encoding='utf-8')
    assert 'Se hablo de cosas' in txt


def test_archivar_es_idempotente(reunion):
    """El pipeline lo llama mas de una vez por reunion."""
    reunion.set_pid('proj-a')
    for _ in range(3):
        pc.sync_meeting_summary(reunion.md)
    assert reunion.filed_in() == ['proj-a']


def test_reasignar_mueve_la_reunion(reunion):
    """El bug del kickoff: reasignar desde el panel dejaba la copia vieja."""
    reunion.set_pid('proj-a')
    pc.sync_meeting_summary(reunion.md)

    reunion.set_pid('proj-b')
    pc.sync_meeting_summary(reunion.md)

    assert reunion.filed_in() == ['proj-b']


def test_reasignar_no_deja_copia_duplicada(reunion):
    reunion.set_pid('proj-a')
    pc.sync_meeting_summary(reunion.md)
    reunion.set_pid('proj-b')
    pc.sync_meeting_summary(reunion.md)
    assert 'proj-a' not in reunion.filed_in()


def test_desasignar_devuelve_none(reunion):
    reunion.set_pid('proj-a')
    pc.sync_meeting_summary(reunion.md)
    reunion.set_pid('none')
    assert pc.sync_meeting_summary(reunion.md) is None


def test_desasignar_retira_la_reunion_de_todos_los_proyectos(reunion):
    reunion.set_pid('proj-a')
    pc.sync_meeting_summary(reunion.md)
    reunion.set_pid('none')
    pc.sync_meeting_summary(reunion.md)
    assert reunion.filed_in() == []


def test_sin_actions_json_no_lanza_y_devuelve_none(reunion):
    """Pasa de verdad: el pipeline archiva antes de enriquecer las acciones."""
    assert pc.sync_meeting_summary(reunion.md) is None


def test_detect_project_por_palabras_clave_puede_discrepar(reunion):
    """Se fija que la adivinanza existe y puede apuntar a otro sitio: es la
    mitad de la causa raiz del incidente."""
    weak = pc.detect_project('reunion sobre el proyecto A', 'kickoff',
                             [{'id': 'proj-a', 'name': 'Proyecto A'},
                              {'id': 'proj-b', 'name': 'Proyecto B'}])
    assert weak is not None and weak['id'] == 'proj-a'


def test_se_archiva_donde_dice_el_actions_json_no_donde_adivina_el_detector(reunion):
    """LA REGLA. El _actions.json es la decision tomada (y editable por el
    usuario); detect_project es solo una adivinanza."""
    reunion.set_pid('proj-b')
    pc.sync_meeting_summary(reunion.md)
    assert reunion.filed_in() == ['proj-b']
