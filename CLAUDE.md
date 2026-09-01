# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This App Does

**TeamsRecorder** is a Windows desktop daemon that auto-detects Teams calls, records audio (mic + system loopback), transcribes via Whisper, generates structured meeting minutes using Claude, extracts and enriches action items, and surfaces everything through a system tray icon + pywebview web UI.

## Entry Points & Commands

```bash
# Start the daemon (tray app + all background services)
python main.py

# CLI control of a running daemon (writes to .cli_command file for IPC)
python cli.py start
python cli.py stop

# Standalone blocking recording (no daemon needed)
python cli.py record --name "Meeting Name" [--no-minutes]

# Process existing files
python cli.py transcribe path/to/audio.wav
python cli.py minutes path/to/transcript.txt
```

**Windows setup:**
```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
install_autostart.bat   # creates a launcher in the Startup folder (NOT Task Scheduler)
Copy-Item hooks\post-merge .git\hooks\post-merge -Force   # auto-restart on git pull
# to run without console: start_watchdog.vbs
```

The repo may be cloned anywhere. Never hardcode an install path — see
"Install Path, Interpreter & Daemon Lifecycle" below.

**Diagnostic scripts** (not production, just dev helpers): `check_audio.py`, `check_teams.py`, `check_onenote.py`

## Configuration

`.env` file in project root:
```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...          # optional, Whisper fallback only
WHISPER_MODEL=medium           # tiny|base|small|medium|large
WHISPER_LANGUAGE=es            # omit for auto-detect
OUTPUT_DIR=C:\path\to\output   # default: project root
```

Key constants in `config.py`: `TEAMS_POLL_INTERVAL=3.0s`, `TEAMS_REQUIRED_CONFIRMATIONS=2`, `CLAUDE_MODEL=claude-sonnet-4-6`, `SAMPLE_RATE=16000Hz`.

## Pipeline & Data Flow

```
teams_detector.py  ──call detected──►  tray_app.py (pipeline queue)
                                              │
                                        popup.py (30s confirmation)
                                              │
                                   audio_recorder.py
                                   (mic + loopback mix → WAV)
                                              │
                                    transcriber.py
                                   (faster-whisper, OpenAI fallback)
                                              │
                                  minutes_generator.py
                                   (claude -p CLI invocation)
                                              │
                                   actions_parser.py
                                   (regex → action blocks)
                                              │
                                  actions_enricher.py
                                   (claude -p → project/assignee map)
                                              │
                              html_exporter.py + outlook_sender.py
                                              │
                                     app_window.py / web UI
```

**Storage tiers:**
- `recordings/` — raw WAVs + paired transcript `.txt`
- `recordings/processed/` — moved here after full pipeline completes
- `minutes/` — `<slug>.md` + `<slug>.html` + `<slug>.actions.json`
- `inbox/` — drop zone; `inbox_watcher.py` converts any audio/video via ffmpeg and feeds to pipeline

## Key Architecture Decisions

### Claude CLI Bridge
Minutes generation and action enrichment both call `claude -p` as a subprocess (not the SDK). The binary is expected in PATH. Calls use cleaned environment (no API key forwarded). If `claude` is missing, the feature degrades gracefully.

### Dual Audio Recording
`audio_recorder.py` records two streams concurrently:
1. **Mic** — via `sounddevice.InputStream` at 16 kHz mono
2. **System loopback** — via `wasapi_loopback_worker.py` subprocess communicating over stdout (8-byte header: sample_rate + channels, then raw float32 PCM)

At stop, streams are mixed with peak normalization. Loopback is discarded if peak < 0.02 or duration is too short.

### Teams Detection
`teams_detector.py` polls every 3 seconds using psutil (process names), window title keywords (`in a call`, `teams call`, etc.), and pycaw audio session state. Requires 2 consecutive confirmations before emitting a state-change callback.

### Minutes & Action Format
`minutes_generator.py` enforces a structured prompt. The output must begin with:
```
TITULO: [3–5 word title in meeting language]
```
Action blocks embedded in markdown:
- ` ~~~instruction-for-claude\n...\n~~~ ` — direct Claude Code instruction
- ` ~~~code_change\n// ARCHIVO:\n// CONTEXTO:\n// INSTRUCCION PARA CLAUDE CODE:\n~~~ `
- ` ~~~document-change\n// ARCHIVO:\n// CONTEXTO:\n// INSTRUCCION:\n~~~ `

`actions_parser.py` uses regex to extract these; `actions_enricher.py` calls Claude to assign each to a project (guessed from directory names) and an assignee.

### Web UI
`app_window.py` wraps pywebview as a singleton and exposes an `AppAPI` class (Python methods callable from JS via `window.pywebview.api.*`). Key methods: `get_meetings()`, `get_actions()`, `execute_action()`, `enrich_actions()`. The web UI (`web/index.html` + `app.js` + `app.css`) is a single-page app with a meetings sidebar and an actions manager view.

### Single Instance
`main.py` writes a `.lock` file with the current PID. On startup it checks if the PID is still alive; if so, it exits. CLI commands communicate with the running daemon by writing to a `.cli_command` file polled by a listener thread in `main.py`.

### Install Path, Interpreter & Daemon Lifecycle

`tr_env.ps1` is the single source of truth for three questions that used to be
answered separately (and inconsistently) by the watchdog, the git hook and the
install skill. Anything that needs to locate, stop or start the app must dot-source
it rather than reimplement the logic:

```powershell
. (Join-Path $dir "tr_env.ps1")
```

| Function | Contract |
|---|---|
| `Get-TRRoot` | Install path, or `$null`. Order: `$env:TEAMSRECORDER_HOME` → saved config in `%LOCALAPPDATA%\TeamsRecorder` → current git repo → common locations. Callers must **ask the user** when it returns `$null`. |
| `Test-TRRoot -Path` | `$true` if the path is a valid clone (`.git` + `main.py` + `tray_app.py`). Deliberately does not check the remote URL, so forks work. |
| `Get-TRPython -Root` | `@{ Python; Pythonw; Source }`. Prefers the repo's `.venv`, then PATH (skipping the WindowsApps alias), then per-user installs. |
| `Test-TRRecording -Root` | `$true` if `.pipeline_status.json` has a job in `recording`. |
| `Get-TRDaemonProcess -Root` | Daemon processes of **this** install, matched by command line. |
| `Stop-TRDaemon -Root` | Stops them, children before parents. |
| `Start-TRDaemon -Root` | Ensures the watchdog is up and waits for the daemon to appear. |
| `Update-TRDependencies -Root` | `pip install -r requirements.txt` using the resolved interpreter. |

**Invariants — breaking any of these has caused a real bug:**

1. **Never hardcode an install path.** Users clone wherever they want. A hardcoded
   `~\Documents\TeamsRecorder` made the install skill believe the app was missing
   and clone a *second* copy, leaving two daemons fighting over one `.lock`.
2. **Never select processes by name.** `Get-Process python | Stop-Process -Force`
   kills every Python process on the machine (other projects included) and still
   misses the daemon, which runs as `pythonw.exe`. Filter on `CommandLine`.
3. **The watchdog is the only thing that launches `main.py`.** It relaunches the
   daemon 5 s after it dies, so anything else that starts `main.py` creates a
   duplicate instance.
4. **Stop the watchdog before the daemon** when updating, or it relaunches the app
   mid-`pip install`.
5. **Never restart while `Test-TRRecording` is true.** That is a live meeting.
6. **One restart procedure: `restart_after_update.ps1`.** Both `hooks/post-merge`
   and the install skill call it. When the hook is installed it already runs during
   `git pull`, so the skill must not repeat the work.
7. **`hooks/*` must stay LF** (enforced by `.gitattributes`). CRLF makes `sh` fail
   with `bad interpreter: /bin/sh^M`.
8. **`hooks/pre-push` is maintainer-only.** It triggers the update email to everyone
   in `team/recipients.txt`.

## Where to Make Changes

| What you want to change | Where |
|---|---|
| Audio mix levels, loopback threshold | `audio_recorder.py` |
| Whisper model / language remapping (Galician→Spanish etc.) | `transcriber.py`, `config.py` |
| Minutes system prompt / action block format | `minutes_generator.py` |
| Action parsing regex | `actions_parser.py` |
| Project/assignee guessing logic | `actions_parser._guess_project()`, `actions_enricher.py` |
| Email template / Outlook calendar search window | `outlook_sender.py` |
| HTML minutes styling | `html_exporter.py` |
| Tray menu items / pipeline triggers | `tray_app.py` |
| Web UI layout and filtering | `web/app.js`, `web/app.css` |
| Storage paths / retention policy | `storage.py` |
| Install / update / restart flow | `tr_env.ps1` first, then `restart_after_update.ps1`, `hooks/post-merge`, `.claude/skills/teamsrecorder/SKILL.md` |

## Logging

All modules log to both `teamsrecorder.log` (project root) and stdout at INFO level. To debug, increase to DEBUG in `main.py`'s `logging.basicConfig` call.
