"""Disco libre mientras se graba.

26/10/2026: el disco se lleno a mitad de una reunion (el pagefile de Windows
crecio de ~14 a ~20 GB con la memoria al limite) y se perdieron 11 minutos de
audio sin ningun aviso. Mientras se graba, la bandeja mira el disco cada 20 s:
por debajo de WARN_GB avisa, y por debajo de CRITICAL_GB borra la reserva para
que la grabacion siga escribiendo. Umbrales por niveles, como las "disk
watermarks" de Elasticsearch.

La reserva es un archivo de relleno (el "ballast file" de SRE) de
`disk_reserve_gb` GB en la carpeta de Noted. Apagada si no se configura. Solo se
crea si despues quedan RESERVE_HEADROOM_GB libres, asi que nunca es lo que
llena el disco.
"""
import logging
import os
import shutil
from pathlib import Path

import config

log = logging.getLogger(__name__)

GB = 1024 ** 3
WARN_GB = 3.0
WARN_CLEAR_GB = 4.0          # histeresis: vuelve a avisar solo tras subir de aqui
CRITICAL_GB = 1.0
RESERVE_HEADROOM_GB = 5.0


def free_gb(path: Path | None = None) -> float | None:
    """GB libres donde se graba (o en `path`); None si no se puede saber."""
    try:
        return shutil.disk_usage(path or config.RECORDINGS_DIR).free / GB
    except OSError:
        return None


def system_free_gb() -> float | None:
    """GB libres en el disco del sistema, donde crece el pagefile."""
    return free_gb(Path(os.environ.get('SystemDrive', 'C:') + os.sep))


class Levels:
    """Nivel del disco con histeresis: cada aviso sale una vez por bajada."""

    def __init__(self):
        self.warned = False
        self.critical = False

    def update(self, free: float | None) -> str | None:
        """'warn' o 'critical' al cruzar cada umbral hacia abajo; None si no."""
        if free is None:
            return None
        if free >= WARN_CLEAR_GB:
            self.warned = self.critical = False
            return None
        if free < CRITICAL_GB and not self.critical:
            self.critical = self.warned = True
            return 'critical'
        if free < WARN_GB and not self.warned:
            self.warned = True
            return 'warn'
        return None


def reserve_file() -> Path:
    return config.PROJECT_DIR / '.disk_reserve'


def reserve_gb() -> float:
    try:
        return max(0.0, float(config.read_settings().get('disk_reserve_gb') or 0))
    except (TypeError, ValueError):
        return 0.0


def ensure_reserve() -> bool:
    """Crea la reserva si esta configurada, falta y cabe con margen; la borra
    si se apago o cambio de tamano. True si queda puesta."""
    path, size = reserve_file(), reserve_gb()
    want = int(size * GB)
    try:
        have = path.stat().st_size
    except OSError:
        have = None
    if have is not None and have != want:
        path.unlink(missing_ok=True)
        have = None
    if want <= 0:
        return False
    if have is not None:
        return True
    free = free_gb(path.parent)
    if free is None or free < size + RESERVE_HEADROOM_GB:
        return False
    try:
        # truncate en NTFS reserva los clusters de verdad: no es un fichero
        # disperso (medido el 26/10/2026 con 512 MB).
        with open(path, 'wb') as f:
            f.truncate(want)
    except OSError as e:
        path.unlink(missing_ok=True)
        log.warning(f"No se pudo crear la reserva de disco: {e}")
        return False
    log.info(f"Reserva de disco creada: {size:g} GB en {path}")
    return True


def release_reserve() -> float:
    """Borra la reserva; devuelve los GB liberados (0 si no habia)."""
    path = reserve_file()
    try:
        size = path.stat().st_size
        path.unlink()
    except FileNotFoundError:
        return 0.0
    except OSError as e:
        log.error(f"No se pudo borrar la reserva de disco: {e}")
        return 0.0
    log.warning(f"Disco casi lleno: reserva de {size / GB:.1f} GB liberada")
    return size / GB
