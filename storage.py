import glob
import re
from datetime import datetime, timedelta
from pathlib import Path

from config import RECORDINGS_DIR, MINUTES_DIR, INBOX_DIR, MINUTES_RETENTION_DAYS


def ensure_directories():
    for d in (RECORDINGS_DIR, MINUTES_DIR, INBOX_DIR, RECORDINGS_DIR / 'processed'):
        d.mkdir(parents=True, exist_ok=True)


def _slugify(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*]', '', text)
    text = re.sub(r'[\s\-]+', '_', text)
    return text.strip('_')[:60]


def _recording_timestamp(path: Path) -> str:
    """Extract YYYYMMDD_HHMM from stem YYYY-MM-DD_HH-MM_..."""
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})', path.stem)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}_{m.group(4)}{m.group(5)}"
    return ''


# Sufijo para dos grabaciones con el mismo nombre en el mismo minuto. Es el mismo
# convenio que ya usa la papelera (stem__2).
_COLLISION_SEP = '__'


def _recording_stem_taken(stem: str) -> bool:
    """True si ya existe CUALQUIER fichero de una grabacion con este stem.

    No basta con mirar el .wav final: mientras se graba solo existen los
    temporales (.mic.part, .loop.part); tras procesar, el WAV se va a processed/;
    y el transcript (_transcript.txt) cuelga del mismo stem. Pisar cualquiera
    de ellos mezcla o pierde datos de otra grabacion.
    """
    pat = glob.escape(stem)
    for folder in (RECORDINGS_DIR, RECORDINGS_DIR / 'processed'):
        if not folder.is_dir():
            continue
        if next(folder.glob(f'{pat}.*'), None) or next(folder.glob(f'{pat}_transcript.*'), None):
            return True
    return False


def get_recording_path(name: str = 'recording') -> Path:
    """Ruta para una grabacion nueva, que no pise ninguna anterior.

    El nombre solo llega al minuto. El 2026-10-08 dos pruebas de "Grabar ahora"
    a las 14:05 recibieron el mismo nombre y la segunda sobrescribio la primera:
    se perdieron 14 s de audio. Ahora, si el nombre ya esta en uso, se anade
    __2, __3... El formato YYYY-MM-DD_HH-MM_<nombre> no cambia, porque varias
    partes de la app lo leen (sesiones, recuperacion al arrancar, minutas).
    """
    ts = datetime.now().strftime('%Y-%m-%d_%H-%M')
    base = f"{ts}_{_slugify(name)}"
    stem, n = base, 2
    while _recording_stem_taken(stem):
        stem = f"{base}{_COLLISION_SEP}{n}"
        n += 1
    return RECORDINGS_DIR / f"{stem}.wav"


def get_transcript_path(recording_path: Path) -> Path:
    return recording_path.parent / f"{recording_path.stem}_transcript.txt"


def get_minutes_path(recording_path: Path, title: str = None) -> Path:
    ts = _recording_timestamp(recording_path)
    if not ts:
        ts = datetime.now().strftime('%Y%m%d_%H%M')
    slug = _slugify(title) if title else 'Reunion'
    return MINUTES_DIR / f"{ts}_{slug}.md"


def get_latest_minutes() -> Path | None:
    files = sorted(MINUTES_DIR.glob('*.md'), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def list_recordings(limit: int = 10) -> list[Path]:
    files = sorted(
        list(RECORDINGS_DIR.glob('*.wav')) + list((RECORDINGS_DIR / 'processed').glob('*.wav')),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[:limit]


def cleanup_old_recordings(days: int = 10):
    cutoff = datetime.now() - timedelta(days=days)
    audio_patterns = ('*.wav', '*.mp3')
    aux_patterns   = ('*.lang', '*.partial', '*.context', '*.live')
    for d in (RECORDINGS_DIR, RECORDINGS_DIR / 'processed'):
        for pattern in audio_patterns + aux_patterns:
            for f in d.glob(pattern):
                if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                    try:
                        f.unlink()
                    except OSError:
                        pass


def cleanup_old_minutes():
    cutoff = datetime.now() - timedelta(days=MINUTES_RETENTION_DAYS)
    for pattern in ('*.md', '*.html', '*_actions.json', '*_transcript.txt'):
        for f in MINUTES_DIR.glob(pattern):
            if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                try:
                    f.unlink()
                except OSError:
                    pass
