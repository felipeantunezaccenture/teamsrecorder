"""Que Windows no frene a los procesos de Noted que transcriben.

Medido el 20/10/2026 en una reunion real de 69 min: el daemon (12 hilos de
Whisper, prioridad normal) usaba 2,4 de 14 nucleos al transcribir tras colgar;
el proceso del transcript en vivo, 0,7 nucleos para 4 hilos. Windows trata a un
proceso sin ventana como de segundo plano y lo pone en modo de eficiencia
(EcoQoS): nucleos lentos y frecuencia baja. Al sacarlos de ese modo: el daemon
paso a 7,7 nucleos y el en vivo de 247 s de retraso a ir al dia en 3 minutos.
Probablemente la causa de que el 19/10 una reunion de 48 min tardara 84 en
transcribirse.

API documentada: SetProcessInformation(ProcessPowerThrottling) con
ControlMask = PROCESS_POWER_THROTTLING_EXECUTION_SPEED y StateMask = 0. No toca
la prioridad: un proceso con prioridad minima sigue cediendo la CPU a Teams.
"""
import os

_done = False


def opt_out_of_efficiency_mode() -> bool:
    """Saca el proceso actual del modo de eficiencia. Una vez por proceso;
    nunca lanza. False fuera de Windows o si la llamada falla."""
    global _done
    if _done:
        return True
    if os.name != 'nt':
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
            _fields_ = [('Version', wintypes.ULONG), ('ControlMask', wintypes.ULONG),
                        ('StateMask', wintypes.ULONG)]

        state = PROCESS_POWER_THROTTLING_STATE(1, 0x1, 0)   # version 1, EXECUTION_SPEED, apagado
        k32 = ctypes.windll.kernel32
        process_power_throttling = 4                        # PROCESS_INFORMATION_CLASS
        _done = bool(k32.SetProcessInformation(k32.GetCurrentProcess(), process_power_throttling,
                                               ctypes.byref(state), ctypes.sizeof(state)))
        return _done
    except Exception:
        return False
