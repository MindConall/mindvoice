"""Control de teclas rápidas globales: push-to-talk, mute y salida.

Utiliza la librería ``keyboard`` (código nativo de hooks de teclado). En
Linux puede requerir permisos de superusuario o los de ``/dev/input``; si la
librería no está disponible, la aplicación seguirá funcionando simplemente
sin atajos de teclado.

Los callbacks se ejecutan en los hilos propios de ``keyboard``, por lo que
todas las notificaciones al bucle asíncrono se hacen mediante callbacks
(opcional) que ``main.py`` engancha con ``loop.call_soon_threadsafe``.
"""

import logging
import threading

logger = logging.getLogger(__name__)

try:
    import keyboard as _keyboard

    _HAS_KEYBOARD = True
except (ImportError, OSError) as exc:  # pragma: no cover - según plataforma
    _HAS_KEYBOARD = False
    logger.warning("Librería 'keyboard' no disponible (%s). Sin atajos.", exc)


class HotkeyController:
    """Gestiona el estado de silencio y la salida mediante teclas globales.

    - ``push_to_talk``: mantener ``ptt_key`` pulsada desactiva el silencio;
      soltarla lo reactiva.
    - ``toggle``: cada pulsación de ``toggle_key`` alterna el silencio.
    - ``quit_keys``: combinación que detiene la aplicación.

    OJO: ``quit_keys`` es un hook GLOBAL, así que la tecla suelta choca con
    cualquier programa. Antes era ``esc`` y en un juego (donde se pulsa Esc sin
    parar)     apagaba el motor en cada pulsación: el overlay debe distinguir "el
    usuario quiere salir" de "el motor se ha caído", porque antes el watchdog
    resucitaba la sesión y el resultado era un bucle de reinicios de 8 s que
    el usuario veía como "se desconecta y se conecta". Por defecto es una
    combinación (``ctrl+shift+esc``) que nadie pulsa sin querer.
    """

    def __init__(
        self,
        mode: str = "push_to_talk",
        ptt_key: str = "right ctrl",
        toggle_key: str = "f9",
        quit_keys: str = "ctrl+shift+esc",
        initially_muted: bool = True,
    ) -> None:
        self._lock = threading.Lock()
        self._muted = initially_muted
        self._mode = mode
        self._ptt_key = ptt_key
        self._toggle_key = toggle_key
        self._quit_keys = quit_keys
        self.on_quit = None          # callable() -> None (desde otro hilo)
        self.on_mute_changed = None  # callable(muted: bool)

    # -- Estado ------------------------------------------------------------
    @property
    def muted(self) -> bool:
        """``True`` si el micrófono está silenciado (no se envía audio)."""
        with self._lock:
            return self._muted

    def _set_muted(self, value: bool) -> None:
        with self._lock:
            changed = self._muted != value
            self._muted = value
        if changed and self.on_mute_changed is not None:
            self.on_mute_changed(value)

    def _toggle_muted(self) -> None:
        with self._lock:
            state = not self._muted
        self._set_muted(state)

    def toggle(self) -> None:
        """Alterna el estado de silencio (útil para botones de la GUI)."""
        self._toggle_muted()

    # -- Registro de hooks -------------------------------------------------
    def start(self) -> None:
        """Registra los atajos de teclado globales."""
        if not _HAS_KEYBOARD:
            return
        if self._mode == "push_to_talk":
            _keyboard.on_press_key(self._ptt_key, lambda e: self._set_muted(False))
            _keyboard.on_release_key(self._ptt_key, lambda e: self._set_muted(True))
        else:
            _keyboard.add_hotkey(self._toggle_key, self._toggle_muted)
        _keyboard.add_hotkey(self._quit_keys, self._on_quit_pressed)
        logger.info(
            "Atajos activos — modo=%s ptt=%s toggle=%s salir=%s",
            self._mode,
            self._ptt_key,
            self._toggle_key,
            self._quit_keys,
        )

    def _on_quit_pressed(self) -> None:
        if self.on_quit is not None:
            self.on_quit()

    def stop(self) -> None:
        """Limpia todos los hooks de teclado registrados."""
        if _HAS_KEYBOARD:
            _keyboard.unhook_all()
            logger.info("Atajos de teclado desregistrados")