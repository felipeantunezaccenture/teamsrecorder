"""Modal de renombrar reunión (web/app.js).

REGRESION (20/10/2026): al pulsar "Guardar" el modal se cerraba pero el nombre
no cambiaba. confirmRenameModal() llamaba a closeRenameModal(), que pone
_renameCallback a null, y DESPUES comprobaba `if (_renameCallback)`: el callback
que invoca rename_meeting() no se ejecutaba nunca. El backend estaba bien
(tests/test_app_window_edits.py); el fallo era solo del front.

No hay framework de tests de JS en el repo: se ejecutan las funciones reales
extraídas de app.js con un DOM mínimo, y el test se salta si no hay Node.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

APP_JS = Path(__file__).resolve().parent.parent / 'web' / 'app.js'
NODE = shutil.which('node')

_HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const code = src.slice(src.indexOf('let _renameCallback = null;'),
                       src.indexOf('// ── Modal de confirmación genérico'));
const els = {
  'rename-modal-input': { value: '', select() {} },
  'rename-modal': { classList: { remove() {}, add() {} } },
};
const fn = new Function('document', 'setTimeout', code +
  '; return {openRenameModal, confirmRenameModal};')({ getElementById: id => els[id] }, () => {});
const scenario = process.argv[2];
let called = [];
fn.openRenameModal('Viejo', async (v) => { called.push(v); });
els['rename-modal-input'].value = scenario === 'vacio' ? '   ' : '  Nombre Nuevo ';
fn.confirmRenameModal().then(() => console.log(JSON.stringify(called)));
"""


def _run(scenario: str) -> str:
    r = subprocess.run([NODE, '-e', _HARNESS, str(APP_JS), scenario],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.skipif(NODE is None, reason='Node no está instalado')
def test_guardar_ejecuta_el_callback_con_el_nombre_recortado():
    assert _run('normal') == '["Nombre Nuevo"]'


@pytest.mark.skipif(NODE is None, reason='Node no está instalado')
def test_un_nombre_vacio_no_ejecuta_el_callback():
    assert _run('vacio') == '[]'
