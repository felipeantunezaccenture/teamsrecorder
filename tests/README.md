# Tests de TeamsRecorder

## Por qué existe esta carpeta

El repo llegó a 9.444 líneas y 390 funciones con **cero tests y cero CI**. No era
un detalle de estilo: la app se rompió cuatro veces en una semana y en dos de
ellas se perdieron minutos de reunión reales.

| Fecha | Qué pasó | Coste |
|---|---|---|
| 16/09/2026 | Un reintento del loopback dejó un writer colgado y el temporal sin borrar | 99 MB + un hilo colgado por grabación |
| 16/09/2026 | El aviso de título residual se escribía en cada poll | 3,7 MB de log que tapaban todo lo demás |
| 17/09/2026 | Sin memoria al 55% → crash nativo `0xC0000409` → el watchdog relanzó y la transcripción **empezó de cero** | 1 hora de reproceso |
| 17/09/2026 | El detector no disparó aunque Windows registraba el micrófono de Teams | **9 minutos de reunión perdidos** |

Cada test de esta carpeta lleva en su docstring el incidente que lo originó. No
es decoración: cuando uno falle, ahí está lo que se rompió la última vez.

## Cómo se corren

```bash
pip install -r requirements-dev.txt

pytest -m unit          # rápido, sin dispositivos. Es lo que corre en CI
pytest -m integration   # usa el MICRÓFONO REAL de la máquina. Solo en local
pytest                  # todo
pytest -m "not slow"    # sin los del detector, que dependen de polls reales
```

### Los tres markers

| Marker | Qué significa |
|---|---|
| `unit` | Sin dispositivos, sin red, sin subprocess, sin COM. Corre en CI |
| `integration` | Necesita micrófono real, el CLI de `claude` o Whisper. Solo local |
| `slow` | Depende de polls reales del detector (~20 s en total) |

## La regla: todo cambio de código lleva su test

**No es una recomendación, es un mecanismo.** Hay tres capas, y ninguna sola
alcanza:

| Capa | Qué frena | Cómo se salta |
|---|---|---|
| Hook `Stop` de Claude Code (`.claude/hooks/require_tests.py`) | Que Claude termine un turno tras cambiar un `.py` sin tocar `tests/` | No se salta con ningún modo de permisos, pero **solo ve lo que hace Claude** |
| `pre-commit` (`.pre-commit-config.yaml`) | Cualquier commit en local: ruff + `pytest -m unit` + la misma regla sobre lo que hay en staging | `git commit --no-verify` |
| GitHub Actions (`.github/workflows/tests.yml`) | Cualquier push o PR: ruff, tests y **`diff-cover` sobre las líneas cambiadas** | Nada. Es la capa que de verdad obliga |

Para que la tercera **bloquee** un merge y no solo avise, el dueño del repo
tiene que marcar el check `tests` como *required* en
Settings → Branches → Branch protection rules.

Y una nota sobre por qué el gate es `diff-cover` y no "¿has tocado algún
fichero de tests?": esa regla se engaña con una línea en blanco. Exigir
cobertura sobre las líneas **nuevas**, no.

### Para activar `pre-commit` en tu máquina (una vez)

```bash
pip install -r requirements-dev.txt
pre-commit install
```

## Un test que pasa con y sin el arreglo no vale

Antes de dar por bueno un test de regresión, **anula el arreglo y comprueba que
falla**. Si pasa en los dos casos no está probando nada. Ejemplo real: el test
de la fuga del temporal del loopback, sin el arreglo, deja 2 writers vivos y un
`.part` huérfano; el del spam del log pasa de 1 aviso a 35.

## Aislamiento: lo más importante del `conftest.py`

Sin aislamiento estos tests no son una red de seguridad, **son un peligro**:

1. `TrayApp.__init__` arranca tres hilos. Uno puede **mandar un correo real al
   equipo** por Outlook; otro hace glob sobre `recordings/` y **encola las
   grabaciones reales** para transcribir. Por eso `TrayApp` se construye con
   `object.__new__` (fixture `tray_factory`).
2. `tasks_store.TASKS_FILE` y `buckets_store.BUCKETS_FILE` se resuelven en
   tiempo de import contra la raíz del repo: un test que llame `create_task()`
   sin redirigir **escribe en el tablero real**.
3. `main.py` y `cli.py` adjuntan un `FileHandler` sobre `teamsrecorder.log` al
   importarse. El `conftest` lo quita: la suite no debe ensuciar el log que se
   usa para diagnosticar crashes.

La fixture `tr_dirs` es **`autouse`**, sin excepciones, y hay un guardián de
sesión que compara una huella de tus datos antes y después: si la suite toca
`tasks.json`, `buckets.json`, `settings.json`, `projects.json`, `pins.json`,
`.cancelled_jobs.txt`, `recordings/` o `minutes/`, **falla**.

### Trampa: los puntos de inyección no son uniformes

Los módulos hacen `from config import PROJECT_DIR`, o sea que se quedan con una
**copia**. Parchear `config.PROJECT_DIR` no afecta a `tray_app.PROJECT_DIR`.
Y varias funciones re-importan su configuración *dentro del cuerpo*:

| Función | Hay que parchear |
|---|---|
| `tray_app._write_status` | `config.PROJECT_DIR` |
| `app_window.get_transcript_text` | `config.RECORDINGS_DIR` |
| `tasks_store.migrate_panel_actions` | `config.MINUTES_DIR` |
| `project_exporter.get_meeting_project` | `config.PROJECT_DIR` |
| `teams_chat.send_recording_notice` | `config.PROJECT_DIR` |

Y un caso **sin costura**: `outlook_sender._build_email_html` lee
`settings.json` vía `Path(__file__).parent`, así que no se puede redirigir a
`tmp_path`. No hagas aserciones sobre `user_name`.

## Fixtures disponibles

| Fixture | Para qué |
|---|---|
| `tr_dirs` (autouse) | Redirige todas las rutas de la app a `tmp_path` y devuelve la raíz |
| `tray_factory` / `tray` | `TrayApp` sin arrancar hilos |
| `wav_factory` | WAV sintético de la duración y sample rate que pidas |
| `fake_whisper_model` | Modelo falso con la forma de faster-whisper (generador perezoso) |
| `fake_outlook` | Sustituye el único seam COM del repo. Su `Send()` lanza a propósito: la app solo crea borradores |

## Qué NO se testea, a propósito

`popup.py`, `tk_thread.py` y `actions_window.py` son GUI pura: un test ahí
probaría tkinter, no la app. Igual las ~42 funciones de pegamento de
`app_window.py` que solo lanzan `subprocess.Popen`, `webbrowser.open` o
diálogos nativos. Y los `check_*.py` y `diagnostico.py`, que son scripts de
diagnóstico manual.

Prefiero 30 tests que importan a 300 que prueban el framework.

## Nota: los WAV no se versionan

`.gitignore` ignora `*.wav`, así que los tests **fabrican** su audio en cada
ejecución con `wav_factory`. No metas binarios en el repo.

---

## Defectos encontrados al escribir la suite

Nueve, todos **fijados con el comportamiento actual** en lugar de arreglados: la
regla acordada era no tocar la app hasta tener la red puesta. Cada test lleva
`DEFECTO` en el nombre y explica la consecuencia, así que arreglarlos es cuestión
de invertir la aserción y ver el test fallar primero.

Ordenados por lo que cuestan si se manifiestan:

| # | Dónde | Qué pasa |
|---|---|---|
| 1 | `inbox_watcher._process:75` | Con ffmpeg disponible (el caso normal), un `.wav` del inbox se convierte a 16 kHz y **el original sobrescribe la conversión**. Nunca se remuestrea |
| 2 | `app_window.recover_meeting:820` | `if not str(dst_dir)` es rama **muerta** (`str(Path(''))` es `'.'`). Un `orig_dir` vacío restaura el fichero en el directorio de trabajo del proceso |
| 3 | `app_window.recover_meeting:829` | El `rmtree` corre **incondicionalmente**: si un `move` falla, se borra la papelera, se pierde el fichero y devuelve `True` |
| 4 | `outlook_sender._name_score` | `'Comite de la Direccion'` vs `'Retro de la Semana'` puntúa **exactamente 0.5**, y el umbral es `>= 0.5`. Los destinatarios pueden venir de otra reunión |
| 5 | `tray_app.has_pending_session` | Contención bidireccional de subcadenas: una reunión `'AI'` casa con `'Mail Review'` y **las dos se fusionan en una minuta** |
| 6 | `tray_app._recover_pending:778` | Encola en la `Queue` pero no en `_pipeline_queued`: los trabajos recuperados al arrancar no se pueden descartar ni salen como `queued` |
| 7 | `tasks_store.create_task:106` | El bucket por defecto es `'pendiente'`, que **no existe** en `buckets.json`. Tareas en un bucket fantasma |
| 8 | `tasks_store.update_task` | `'deadline'` está en la whitelist pero se persiste `'end_date'`: editar por la clave antigua crea un campo huérfano que la UI no lee |
| 9 | `tasks_store.delete_task` | Solo baja un nivel: borrar un abuelo deja al nieto apuntando a un `parent_id` inexistente |

Otros dos menores, también fijados: el cuerpo del HTML no se escapa (el título
sí), y el relleno de celdas de `_md_table_to_html` no funciona nunca porque su
guarda es siempre cierta.

### Y un riesgo que no es un defecto, es una cuenta atrás

`sounddevice` 0.5.6 hace `data.shape = -1, channels` en el callback de PortAudio,
y **numpy 2.5 lo deprecó**. Hoy solo avisa. Cuando numpy lo elimine, el callback
lanzará en cada bloque de audio y **la grabación dejará de capturar**. El filtro
de avisos de `pyproject.toml` lo ignora explícitamente y explica por qué: hay que
vigilar la versión de `sounddevice`, no taparlo y olvidarlo.

## Tres cosas que aprendí escribiéndola

**Un test que pasa con y sin el arreglo no vale.** Ya está arriba, pero se repite
porque es la única regla que separa una suite útil de una decorativa.

**El análisis previo se equivocó cinco veces, y cuatro fueron a favor del
código.** `extract_title_from_minutes` sí tolera espacios iniciales; el regex del
porcentaje sí exige el signo `%`; `shutil.move` no falla con el destino ocupado
(sobrescribe); el consumidor externo no depende de `projects.json`. Medir antes de
escribir la aserción, siempre.

**La mitad de los fallos iniciales fueron de los tests, no del código.** Fixtures
con la palabra `Tarea` que el parser descarta, un `basetemp` dentro del repo que
invalidaba mi propia comprobación de aislamiento, un `filterwarnings` que mataba
el callback de audio. Cuando un test nuevo falla, el sospechoso número uno es el
test.
