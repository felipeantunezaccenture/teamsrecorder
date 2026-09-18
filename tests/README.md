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
