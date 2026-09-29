"""Lanzador de MindVoice en segundo plano.

Registra el atajo global (``Ctrl+Shift+Z``) mediante ``RegisterHotKey`` de
Win32 y se queda escuchando. Cuando se pulsa:

* Si el overlay está en marcha: le envía "toggle" por un pipe con nombre y
  la ventana se muestra u oculta.
* Si el overlay no está en marcha: lo arranca (se muestra al abrir).

Para que el atajo funcione siempre, este lanzador debe estar ejecutándose en
segundo plano. Se incluye un acceso directo en Inicio de Windows para que
arranque automáticamente al iniciar sesión.

Uso::

    pythonw launcher.py            # modo normal (segundo plano)
    python launcher.py --press    # simula una pulsación (prueba / CLI)
    python launcher.py --stop     # detiene el lanzador en segundo plano
"""

import argparse
import ctypes
import ctypes.wintypes
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from config import Settings
from overlay import _parse_hotkey, OVERLAY_MUTEX, TOGGLE_PIPE
from prefs import apply_prefs
from rutas import log_file, python_exe

logger = logging.getLogger("mindvoice.launcher")

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
HOTKEY_ID = 0x0ACF
CTRL_PIPE = r"\\.\pipe\MindVoiceHotkeyCtrl"
LAUNCHER_MUTEX = "Local\\MindVoiceHotkeyMutex"
ERROR_ALREADY_EXISTS = 183
ERROR_PIPE_CONNECTED = 535
PIPE_ACCESS_DUPLEX = 0x0003
PIPE_TYPE_BYTE = 0x0000
PIPE_READMODE_BYTE = 0x0000
PIPE_WAIT = 0x0000
PIPE_UNLIMITED_INSTANCES = 0xFF

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

PROJECT_DIR = Path(__file__).resolve().parent
# Intérprete resuelto en tiempo de ejecución: .venv en desarrollo, runtime
# embebido en la instalación empaquetada. Fijarlo a .venv hacía que el overlay
# se lanzara con una ruta inexistente en cualquier instalación.
PYTHONW = python_exe(windowless=True)
OVERLAY = str(PROJECT_DIR / "overlay.py")
# Los registros van al directorio de datos del usuario, no junto al código: en
# "Program Files" la escritura falla y el FileHandler del lanzador tumbaba el
# arranque.
LAUNCHER_LOG = log_file("launcher.log")
OVERLAY_LOG = log_file("overlay-launcher.log")
ERROR_EXISTING_INSTANCE = 183
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
ERROR_FILE_NOT_FOUND = 2
ERROR_PIPE_BUSY = 231
ERROR_BROKEN_PIPE = 109
INVALID_HANDLE_VALUE = -1


def _post_wm_quit(thread_id: int) -> None:
    _user32.PostThreadMessageW(ctypes.c_uint(thread_id), WM_QUIT, 0, 0)


def _send_pipe(name: str, data: bytes, retries: int = 20) -> bool:
    """Cliente de pipe clásico (CreateFileW/WriteFile). Reintenta si ocupa."""
    payload = ctypes.create_string_buffer(data)
    for _ in range(retries):
        handle = _kernel32.CreateFileW(
            name, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None
        )
        if handle and handle != INVALID_HANDLE_VALUE:
            nwritten = ctypes.c_ulong(0)
            ok = False
            try:
                ok = _kernel32.WriteFile(
                    handle, payload, len(data), ctypes.byref(nwritten), None
                )
            finally:
                _kernel32.CloseHandle(handle)
            if ok and nwritten.value == len(data):
                return True
            error = _kernel32.GetLastError()
            if error == ERROR_BROKEN_PIPE:
                return True
            logger.warning(
                "WriteFile falló (ok=%s err=%s escritos=%s)",
                ok,
                error,
                nwritten.value,
            )
            return False
        error = _kernel32.GetLastError()
        if error == ERROR_PIPE_BUSY:
            threading.Event().wait(0.05)
            continue
        logger.warning("CreateFileW falló (err=%s)", error)
        return False
    return False


class HotkeyLauncher:
    """Dueño del atajo global y encargado de abrir/ocultar el overlay."""

    def __init__(self, settings: Settings) -> None:
        self._stop = False
        self._thread_id = 0
        self._mutex = 0
        self._mutex_owner = False
        self._settings = settings
        self._acquire_single_instance()
        self._mods, self._vk = _parse_hotkey(self._settings.overlay_hotkey)

    def _acquire_single_instance(self) -> None:
        handle = _kernel32.CreateMutexW(None, False, LAUNCHER_MUTEX)
        if not handle:
            return
        if _kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            _kernel32.CloseHandle(handle)
            self._mutex_owner = False
            return
        self._mutex = handle
        self._mutex_owner = True

    def _overlay_running(self) -> bool:
        handle = _kernel32.OpenMutexW(0x1F0001, False, OVERLAY_MUTEX)
        if handle:
            _kernel32.CloseHandle(handle)
            return True
        return False

    def _spawn_overlay(self) -> None:
        logger.info("Arrancando el overlay...")
        with OVERLAY_LOG.open("ab") as log:
            subprocess.Popen(
                [PYTHONW, OVERLAY],
                cwd=str(PROJECT_DIR),
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
                close_fds=True,
            )

    def _toggle_overlay(self) -> None:
        logger.info("Enviando toggle al overlay en marcha...")
        if _send_pipe(TOGGLE_PIPE, b"t"):
            logger.info("Toggle enviado.")
        else:
            logger.warning("No se pudo enviar el toggle (¿overlay arrancando?).")

    def _on_hotkey(self) -> None:
        if self._overlay_running():
            self._toggle_overlay()
        else:
            self._spawn_overlay()

    # ------------------------------------------------------------------
    def _hotkey_worker(self) -> None:
        """Bomba la cola de mensajes y atiende WM_HOTKEY (bloquea)."""
        msg = ctypes.wintypes.MSG()
        _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
        self._thread_id = _kernel32.GetCurrentThreadId()
        if self._vk == 0:
            logger.error("Atajo global inválido; launcher sin acción.")
            return
        registered = _user32.RegisterHotKey(
            None, HOTKEY_ID, ctypes.c_uint(self._mods), ctypes.c_uint(self._vk)
        )
        if not registered:
            logger.warning(
                "No se pudo registrar %s (¿ya lo usa otra app?).",
                self._settings.overlay_hotkey,
            )
            return
        logger.info("Atajo global activo: %s", self._settings.overlay_hotkey)
        try:
            while not self._stop:
                result = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result <= 0:
                    break
                if (
                    msg.message == WM_HOTKEY
                    and (ctypes.c_ulong(msg.wParam).value & 0xFFFF) == HOTKEY_ID
                ):
                    self._on_hotkey()
        finally:
            _user32.UnregisterHotKey(None, HOTKEY_ID)

    def _ctrl_worker(self) -> None:
        """Pipe de control: un ``q`` ordena detener el launcher."""
        while True:
            pipe = _kernel32.CreateNamedPipeW(
                CTRL_PIPE,
                PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
                PIPE_UNLIMITED_INSTANCES,
                64,
                64,
                0,
                None,
            )
            if not pipe:
                threading.Event().wait(0.2)
                continue
            connected = _kernel32.ConnectNamedPipe(pipe, None)
            if not connected and _kernel32.GetLastError() != ERROR_PIPE_CONNECTED:
                _kernel32.CloseHandle(pipe)
                continue
            buf = ctypes.create_string_buffer(4)
            nread = ctypes.c_ulong(0)
            _kernel32.ReadFile(pipe, buf, 4, ctypes.byref(nread), None)
            reply = ctypes.create_string_buffer(b"ok")
            nwritten = ctypes.c_ulong(0)
            _kernel32.WriteFile(pipe, reply, 2, ctypes.byref(nwritten), None)
            _kernel32.CloseHandle(pipe)
            if nread.value and buf.raw[:nread.value].strip() == b"q":
                logger.info("Orden de detención recibida.")
                self._stop = True
                if self._thread_id:
                    _post_wm_quit(self._thread_id)
                return

    # ------------------------------------------------------------------
    def run(self) -> int:
        if not self._mutex_owner:
            logger.warning("Ya hay un lanzador activo; saliendo.")
            return 1
        if self._vk == 0:
            return 1
        ctrl = threading.Thread(target=self._ctrl_worker, daemon=True)
        ctrl.start()
        self._hotkey_worker()
        return 0

    def press_once(self) -> int:
        """Simula una pulsación del atajo sin registrarlo (uso CLI/pruebas)."""
        if self._vk == 0:
            return 1
        self._on_hotkey()
        return 0


def _stop_running_launcher() -> bool:
    return _send_pipe(CTRL_PIPE, b"q")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        filename=str(LAUNCHER_LOG),
    )
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--press", action="store_true")
    parser.add_argument("-h", "--help", action="store_true")
    args, _ = parser.parse_known_args()
    if args.help:
        print(__doc__)
        return 0
    if args.stop:
        ok = _stop_running_launcher()
        print("Lanzador detenido." if ok else "No hay lanzador activo.")
        return 0 if ok else 1
    settings = Settings()
    apply_prefs(settings)
    launcher = HotkeyLauncher(settings)
    if args.press:
        return launcher.press_once()
    return launcher.run()


if __name__ == "__main__":
    sys.exit(main())