"""Estado del microfono por defecto de Windows: cual es y si esta silenciado.

Windows guarda un silencio distinto POR DISPOSITIVO. El 2026-10-08 una prueba de
"Grabar ahora" sin auriculares grabo 27 s de nada: el micro integrado del portatil
estaba silenciado y no se habia notado nunca, porque en las llamadas se usan
auriculares, que son otro microfono con su propio silencio. La app abria el micro
sin problema (Windows lo registra) y solo se supo al final, con un "sin voz".

Este modulo es informativo: ante cualquier error devuelve None / False y nunca
lanza. No puede ser la causa de que una grabacion falle.
"""
import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

_MAX_NAME = 48


@dataclass(frozen=True)
class MicState:
    name: str
    muted: bool


def short_name(friendly: str) -> str:
    """Quita el prefijo de instancia que pone Windows ('2- ') y recorta.

    'Microphone Array (2- Intel Smart Sound ...)' -> 'Microphone Array (Intel Smart Sound ...)'
    """
    name = re.sub(r'\(\d+-\s*', '(', (friendly or '').strip())
    if len(name) > _MAX_NAME:
        name = name[:_MAX_NAME - 3].rstrip() + '...'
    return name or '?'


def com_init() -> None:
    """Inicializa COM en el hilo que llama, como hace teams_detector en el suyo.

    Si el hilo ya lo tenia inicializado en otro modo, pythoncom lanza y se ignora:
    COM ya esta disponible igualmente.
    """
    try:
        import pythoncom
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
    except Exception:
        pass


def _endpoint_volume(device):
    """QueryInterface, NUNCA ctypes.cast.

    cast() crea un segundo puntero al mismo objeto COM sin AddRef; al recolectar
    los dos, el objeto se libera dos veces. Con el patron "lo creo una vez y lo
    guardo" no se nota, pero leyendo cada 2 s si: el 2026-10-08 el daemon cayo
    con 0xC0000005 a los 100 s, y 1.200 lecturas en 3 hilos reprodujeron
    "access violation" en comtypes Release(). Es el patron que ya usa
    teams_detector._check_mic_session.
    """
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import IAudioEndpointVolume
    interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return interface.QueryInterface(IAudioEndpointVolume)


def read_default_mic() -> MicState | None:
    """El microfono que usara una grabacion nueva, o None si no se puede saber."""
    try:
        from pycaw.pycaw import AudioUtilities
        device = AudioUtilities.GetMicrophone()
        if device is None:
            return None
        muted = bool(_endpoint_volume(device).GetMute())
        name = short_name(AudioUtilities.CreateDevice(device).FriendlyName)
        return MicState(name=name, muted=muted)
    except Exception as e:
        log.debug(f"No se pudo leer el microfono por defecto: {e}")
        return None


def set_default_mic_muted(muted: bool) -> bool:
    """Silencia o activa el microfono por defecto. True si Windows lo acepto."""
    try:
        from pycaw.pycaw import AudioUtilities
        device = AudioUtilities.GetMicrophone()
        if device is None:
            return False
        _endpoint_volume(device).SetMute(1 if muted else 0, None)
        return True
    except Exception as e:
        log.warning(f"No se pudo cambiar el silencio del microfono: {e}")
        return False
