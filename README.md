# TeamsRecorder

Daemon de Windows que detecta automáticamente reuniones de Teams, graba el audio, transcribe con Whisper y genera minutas estructuradas usando Claude AI. Todo accesible desde un icono en la bandeja del sistema.

## Qué hace

- Detecta llamadas de Teams automáticamente
- Graba micrófono + audio del sistema (loopback)
- Transcribe con faster-whisper (modelo local, sin coste)
- Genera minutas con Claude: resumen, decisiones, acciones
- Extrae y enriquece action items (asignados a personas y proyectos)
- Exporta a HTML y a carpetas de proyecto (SharePoint, etc.)
- Chat con Claude sobre cualquier reunión usando el transcript completo
- Interfaz web local para ver y gestionar todas las notas

## Requisitos

- **Windows 10/11**
- **Python 3.11+** — [python.org](https://python.org)
- **Claude CLI** — instalar con `npm install -g @anthropic-ai/claude-code` y hacer `claude login`
- **Git** (para recibir actualizaciones)

## Instalación

### 1. Clonar el repositorio

Puedes clonarlo **donde quieras**: la app y sus scripts descubren la ruta solos, no hay ninguna carpeta obligatoria.

```bash
git clone https://github.com/inescgamez99/teamsrecorder.git TeamsRecorder
cd TeamsRecorder
```

### 2. Crear el entorno virtual e instalar dependencias

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

El `.venv` no es opcional del todo: si existe, todos los scripts del proyecto (watchdog, actualización) lo usan con preferencia sobre el Python del sistema. Si prefieres instalar en el Python global, simplemente no crees el `.venv` y usa `pip install -r requirements.txt`.

### 3. Iniciar sesión en Claude

La app usa Claude Code CLI para generar las minutas. Si no lo tienes instalado:

```powershell
npm install -g @anthropic-ai/claude-code
claude login
```

`claude login` abrirá el navegador para autenticarse con tu cuenta de Anthropic (la misma que usas en claude.ai). No necesitas ninguna API key en el `.env`.

### 4. Instalar el arranque automático con Windows

Ejecuta (doble clic o desde terminal):

```
install_autostart.bat
```

Esto crea un lanzador en la carpeta de Inicio de Windows (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`). A partir del siguiente arranque de sesión, el watchdog se levanta solo.

### 5. Instalar el hook de actualización

```powershell
Copy-Item hooks\post-merge .git\hooks\post-merge -Force
```

Con esto, cada `git pull` reinicia la app con los cambios nuevos automáticamente.

> El hook `hooks/pre-push` **no** debe instalarlo todo el mundo: envía el email de novedades a todo el equipo. Solo lo instala quien mantiene el repositorio.

### 6. Arrancar por primera vez (sin reiniciar Windows)

Doble clic en `start_watchdog.vbs`.

Arranca el watchdog, que es quien lanza y supervisa el daemon. **No lances `python main.py` por tu cuenta si el watchdog está corriendo**: acabarías con dos instancias peleándose por el mismo `.lock`.

El icono gris aparecerá en la bandeja del sistema (esquina inferior derecha). Si está oculto, búscalo en el menú de iconos ocultos (flechita ^).

## Uso

| Acción | Cómo |
|---|---|
| Ver minutas | Click en el icono → "Ver minutas y acciones" |
| Añadir contexto mientras grabas | Click derecho en el icono → "Añadir contexto a grabación" |
| Chat sobre una reunión | Abre la reunión → "Chat con Claude" |
| Regenerar minutas con foco | Abre la reunión → "Regenerar minutas" |
| Exportar a carpeta de proyecto | Abre la reunión → "Exportar a proyecto" |

## Recibir actualizaciones

**La forma recomendada** es abrir Claude Code y ejecutar:

```
/teamsrecorder
```

La skill localiza tu instalación (esté donde esté), comprueba que no haya una grabación en curso, actualiza y reinicia. Es el mismo comando tanto si ya lo tienes instalado como si no.

**A mano**, desde la carpeta del proyecto:

```bash
git pull --ff-only
```

Si tienes el hook `post-merge` instalado (paso 5 de la instalación), el `git pull` ya se encarga de todo: actualiza dependencias y reinicia la app. Si no lo tienes, reinicia después con:

```powershell
.\restart_after_update.ps1
```

Ese script es el procedimiento oficial de reinicio y es seguro ejecutarlo a mano: si detecta una grabación en curso, **no reinicia** y te avisa, para no perder la reunión que se está grabando.

> El watchdog se encarga además de reiniciar automáticamente el daemon si se cae por su cuenta.

## Configuración avanzada

### Cambiar el modelo Whisper

En la app → Ajustes → Grabación. Modelos disponibles: `tiny`, `base`, `small`, `medium` (por defecto), `large-v3`. Más grande = más preciso pero más lento.

### Configurar proyectos y carpetas de exportación

En la app → Ajustes → Proyectos. Puedes asociar un proyecto (ej: "MiProyecto") a una carpeta local (ej: ruta mapeada de SharePoint). Cada reunión detectada como de ese proyecto exportará automáticamente transcript, HTML y versión email a esa carpeta.

### Directorio de salida personalizado

En `.env`:

```env
OUTPUT_DIR=C:\ruta\donde\guardar\todo
```

## Estructura del proyecto

```
TeamsRecorder/
├── main.py                 # Entrada principal del daemon
├── tray_app.py             # Icono bandeja + pipeline de procesamiento
├── popup.py                # Popup de confirmación de grabación
├── audio_recorder.py       # Grabación mic + loopback
├── transcriber.py          # Transcripción con faster-whisper
├── minutes_generator.py    # Generación de minutas con Claude
├── actions_parser.py       # Extracción de action items
├── actions_enricher.py     # Enriquecimiento con Claude (proyecto, asignado)
├── project_exporter.py     # Exportación a carpetas de proyecto
├── html_exporter.py        # Exportación a HTML
├── app_window.py           # Interfaz web (pywebview + API Python)
├── web/                    # Frontend (HTML, JS, CSS)
├── storage.py              # Rutas y almacenamiento
├── config.py               # Configuración global
├── watchdog.ps1            # Supervisa el daemon y lo relanza si se cae
├── tr_env.ps1              # Funciones compartidas: ruta, intérprete, arranque/parada
├── restart_after_update.ps1 # Procedimiento único de reinicio tras actualizar
├── start_watchdog.vbs      # Lanzador silencioso del watchdog
├── install_autostart.bat   # Registra el arranque con Windows
├── hooks/                  # Hooks de git (se copian a .git/hooks manualmente)
└── requirements.txt
```

## Troubleshooting

**El icono no aparece**: Busca en los iconos ocultos (^). Si no está, ejecuta `start_watchdog.vbs`.

**El daemon no arranca / lock file**: el watchdog limpia solo los `.lock` huérfanos (los de un PID que ya no existe), así que normalmente basta con esperar unos segundos. Si persiste, bórralo desde la carpeta del proyecto:
```powershell
Remove-Item .lock -Force
```

**Hay dos iconos en la bandeja / dos instancias**: lo habitual es haber lanzado `python main.py` a mano teniendo el watchdog ya corriendo. Ciérralas todas y arranca solo con `start_watchdog.vbs`.

**Claude no genera minutas**: Asegúrate de que `claude` está en el PATH y has hecho `claude login`.

**No detecta Teams**: Teams debe estar ejecutándose con una llamada activa. La detección tarda ~6 segundos en confirmarse.
