"""Comprobación de extremo a extremo del glosario en Ajustes.

Abre web/index.html en Chromium y conecta la interfaz a la AppAPI REAL de Python
(no a un mock), con un glosario y unos proyectos temporales: no toca el
glossary.json ni el projects.json reales. Es una utilidad de desarrollo, no
forma parte de la suite de pytest.

    pip install playwright && python -m playwright install chromium
    python tools/check_glossary_ui.py [carpeta_para_capturas]

Sale con código 1 si falla alguna comprobación o hay errores de JavaScript.
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SP = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix='gl_shots_'))
SP.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))

import glossary as gl                      # noqa: E402
import app_window as aw                    # noqa: E402
from playwright.sync_api import sync_playwright   # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix='gl_e2e_'))
gl.GLOSSARY_FILE = tmp / 'glossary.json'
aw.PROJECT_DIR = tmp
(tmp / 'projects.json').write_text(json.dumps({'projects': [
    {'id': 'sandoz', 'name': 'Sandoz'}, {'id': 'inditex', 'name': 'Inditex'}]}), encoding='utf-8')
gl.save({
    'entries': [
        {'term': 'IBP', 'category': 'acronym', 'aliases': ['IVP'], 'meaning': 'Integrated Business Planning'},
        {'term': 'Kinaxis', 'category': 'term', 'aliases': ['Kinexis']},
        {'term': 'Inditex', 'category': 'company', 'aliases': ['Dinditex']},
    ],
    'suggestions': [
        {'term': 'Nurettin Ekizoglu', 'category': 'person', 'aliases': ['Norethin'],
         'meaning': '', 'source': '20261020_1400_IDP_Operating_Model_Weekly_Sync'},
        {'term': 'Stelios', 'category': 'person', 'aliases': ['Stereos'],
         'meaning': '', 'source': '20261019_0923_Sincronizacion_Equipo'},
    ],
})
api = object.__new__(aw.AppAPI)

PASS, FAIL = [], []


def check(name, cond, extra=''):
    (PASS if cond else FAIL).append(name)
    print(('  OK   ' if cond else '  FAIL ') + name + (f'  [{extra}]' if extra and not cond else ''))


def on_disk():
    return json.loads(gl.GLOSSARY_FILE.read_text(encoding='utf-8'))


INIT = """
(() => {
  const real = {
    get_glossary: () => window.__py_get_glossary(),
    save_glossary_entry: (e, o) => window.__py_save_glossary_entry(e, o),
    delete_glossary_entry: (t, p) => window.__py_delete_glossary_entry(t, p),
    resolve_glossary_suggestion: (t, a, e) => window.__py_resolve_glossary_suggestion(t, a, e),
    get_settings: async () => ({language: 'es', onboarding_completed: true, theme: 'light'}),
    get_pipeline_status: async () => ({}),
    get_navigate_request: async () => '',
    get_terminal_completions: async () => [],
  };
  window.pywebview = { api: new Proxy(real, {
    get: (t, k) => (k in t ? t[k] : async () => []) }) };
  window.addEventListener('DOMContentLoaded', () => setTimeout(
    () => window.dispatchEvent(new Event('pywebviewready')), 50));
})();
"""

errors = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={'width': 1280, 'height': 860})
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.expose_function('__py_get_glossary', lambda: api.get_glossary())
    page.expose_function('__py_save_glossary_entry', lambda e, o: api.save_glossary_entry(e, o))
    page.expose_function('__py_delete_glossary_entry', lambda t, pr: api.delete_glossary_entry(t, pr))
    page.expose_function('__py_resolve_glossary_suggestion', lambda t, a, e: api.resolve_glossary_suggestion(t, a, e))
    page.add_init_script(INIT)
    page.goto((ROOT / 'web' / 'index.html').as_uri())
    page.wait_for_timeout(1200)

    print('\n[1] Tarjeta en Ajustes y aviso')
    dot = page.locator('#settings-dot')
    check('punto de aviso visible (hay sugerencias)', dot.is_visible())
    page.click('#btn-settings')
    page.wait_for_timeout(500)
    summary = page.inner_text('#glossary-summary')
    check('resumen: 3 entradas y 2 sugerencias', '3 entradas' in summary and '2 sugerencias pendientes' in summary, summary)
    page.locator('#btn-glossary').scroll_into_view_if_needed()
    page.screenshot(path=str(SP / 'shot_1_card.png'))

    print('\n[2] Drawer')
    page.click('#btn-glossary')
    page.wait_for_timeout(500)
    check('drawer abierto', 'open' in page.get_attribute('#glossary-drawer', 'class'))
    check('bloque de sugerencias visible', page.locator('#gl-suggestions-block').is_visible())
    check('2 sugerencias listadas', page.locator('#gl-suggestions .gl-row').count() == 2)
    check('entradas agrupadas por categoria (siglas, empresas, terminos)',
          [x.strip().lower() for x in page.locator('#gl-entries .gl-group').all_inner_texts()] ==
          ['siglas', 'empresas', 'términos'], str(page.locator('#gl-entries .gl-group').all_inner_texts()))
    check('muestra el error que corrige', 'IVP' in page.inner_text('#gl-entries'))
    check('muestra de que reunion viene la sugerencia (sin fecha ni guiones)',
          'IDP Operating Model Weekly Sync' in page.inner_text('#gl-suggestions'))
    page.screenshot(path=str(SP / 'shot_2_drawer.png'))

    print('\n[3] Aprobar y descartar sugerencias')
    page.locator('#gl-suggestions [data-gl="approve"]').first.click()
    page.wait_for_timeout(400)
    d = on_disk()
    check('aprobada: entra en el fichero como entrada', any(e['term'] == 'Nurettin Ekizoglu' for e in d['entries']))
    check('aprobada: sale de pendientes', [s['term'] for s in d['suggestions']] == ['Stelios'])
    check('aprobada: aparece en el drawer bajo Personas', 'Nurettin Ekizoglu' in page.inner_text('#gl-entries'))
    check('sigue el aviso (queda 1)', dot.is_visible())
    page.locator('#gl-suggestions [data-gl="dismiss"]').first.click()
    page.wait_for_timeout(400)
    d = on_disk()
    check('descartada: no entra', not any(e['term'] == 'Stelios' for e in d['entries']))
    check('descartada: se recuerda para no volver a sugerirla', d['dismissed'] == ['stelios>stereos'])
    check('sin pendientes: el bloque se oculta', not page.locator('#gl-suggestions-block').is_visible())
    check('sin pendientes: desaparece el aviso', not dot.is_visible())

    print('\n[4] Añadir y editar')
    page.click('#glossary-drawer .gl-toolbar button')
    check('formulario visible', page.locator('#gl-form').is_visible())
    page.press('#gl-f-term', 'Enter')
    page.wait_for_timeout(200)
    check('sin termino: no guarda y avisa', len(on_disk()['entries']) == 4)
    page.fill('#gl-f-term', 'S&OP')
    page.select_option('#gl-f-category', 'acronym')
    page.fill('#gl-f-aliases', 'SNOP, S and OP')
    page.fill('#gl-f-meaning', 'Sales and Operations Planning')
    page.press('#gl-f-meaning', 'Enter')
    page.wait_for_timeout(400)
    e = next((x for x in on_disk()['entries'] if x['term'] == 'S&OP'), None)
    check('añadida con sus errores separados por comas', e and e['aliases'] == ['SNOP', 'S and OP'], str(e))
    check('añadida: formulario se cierra', not page.locator('#gl-form').is_visible())
    row = page.locator('#gl-entries .gl-row', has_text='S&OP')
    row.locator('[data-gl="edit"]').click()
    check('editar: formulario con los datos', page.input_value('#gl-f-term') == 'S&OP'
          and page.input_value('#gl-f-aliases') == 'SNOP, S and OP')
    page.fill('#gl-f-term', 'SOP')
    page.press('#gl-f-term', 'Enter')
    page.wait_for_timeout(400)
    terms = [x['term'] for x in on_disk()['entries']]
    check('editar el termino lo renombra sin duplicar', 'SOP' in terms and 'S&OP' not in terms, str(terms))
    page.click('#glossary-drawer .gl-toolbar button')
    page.press('#gl-f-term', 'Escape')
    check('Escape cancela el formulario', not page.locator('#gl-form').is_visible())

    print('\n[5] Ambito por proyecto')
    opts = page.locator('#gl-scope option').all_inner_texts()
    check('selector con global y los proyectos', opts == ['Global (todas las reuniones)', 'Sandoz', 'Inditex'], str(opts))
    page.select_option('#gl-scope', 'sandoz')
    page.wait_for_timeout(300)
    check('en un proyecto no se ven las globales', 'IBP' not in page.inner_text('#gl-entries'))
    check('pista del ambito de proyecto', 'proyecto' in page.inner_text('#gl-scope-hint'))
    page.click('#glossary-drawer .gl-toolbar button')
    page.fill('#gl-f-term', 'IDP')
    page.press('#gl-f-term', 'Enter')
    page.wait_for_timeout(400)
    e = next((x for x in on_disk()['entries'] if x['term'] == 'IDP'), None)
    check('la entrada nueva queda ligada al proyecto', e and e['project'] == 'sandoz', str(e))
    page.select_option('#gl-scope', '')
    page.wait_for_timeout(300)
    check('volviendo a global no aparece la del proyecto', 'IDP' not in page.inner_text('#gl-entries'))

    print('\n[6] Borrar (el modal debe quedar POR ENCIMA del drawer)')
    row = page.locator('#gl-entries .gl-row', has_text='Dinditex')
    row.locator('[data-gl="del"]').click()
    page.wait_for_timeout(300)
    modal_ok = page.locator('#confirm-modal-ok')
    check('modal de confirmacion visible', modal_ok.is_visible())
    box = modal_ok.bounding_box()
    top = page.evaluate("([x, y]) => document.elementFromPoint(x, y)?.id", [box['x'] + box['width'] / 2, box['y'] + box['height'] / 2])
    check('el boton Eliminar del modal no queda tapado por el drawer', top == 'confirm-modal-ok', str(top))
    page.screenshot(path=str(SP / 'shot_3_confirm.png'))
    modal_ok.click()
    page.wait_for_timeout(400)
    check('borrada del fichero', not any(x['term'] == 'Inditex' and x['project'] is None for x in on_disk()['entries']))

    print('\n[7] Idiomas')
    page.evaluate("applyLang('en')")
    page.wait_for_timeout(200)
    check('EN: resumen y textos', 'entries' in page.inner_text('#glossary-summary')
          and page.inner_text('#glossary-drawer .task-detail-header span') == 'Glossary')
    check('EN: categorias', 'acronyms' in page.inner_text('#gl-entries').lower())
    page.evaluate("applyLang('ca')")
    page.wait_for_timeout(200)
    check('CA: textos', page.inner_text('#glossary-drawer .task-detail-header span') == 'Glossari'
          and 'sigles' in page.inner_text('#gl-entries').lower())
    page.evaluate("applyLang('es')")
    page.wait_for_timeout(200)
    check('ES: de vuelta', page.inner_text('#glossary-drawer .task-detail-header span') == 'Glosario')
    page.screenshot(path=str(SP / 'shot_4_final.png'))

    print('\n[8] Cerrar')
    page.click('#glossary-drawer .runs-panel-close')
    page.wait_for_timeout(300)
    check('drawer cerrado', 'open' not in page.get_attribute('#glossary-drawer', 'class'))

    browser.close()

print('\nErrores JS de pagina:', errors or 'ninguno')
print(f'\nRESULTADO: {len(PASS)} OK, {len(FAIL)} FALLOS')
for f in FAIL:
    print('  -', f)
print('Capturas en:', SP)
sys.exit(1 if FAIL or errors else 0)
