"""Overlay HUD transparente para MindVoice (Windows 11, PyQt6).

Capa visual que se superpone a juegos/aplicaciones en pantalla completa:

- Ventana sin bordes, translúcida, a pantalla completa y SIEMPRE encima.
  ``Qt.WindowType.Tool`` -> ``WS_EX_TOOLWINDOW`` (sin botón en la barra de
  tareas), ``WA_TranslucentBackground`` -> ``WS_EX_LAYERED`` y
  ``WindowStaysOnTopHint`` -> ventana sobre TODAS las demás.
- Panel central oscuro semi-transparente con el chat de la conversación
  (lo que escribes -> ``Tú``, lo que responde la IA -> ``IA``), línea de
  órdenes estilo launcher e indicador de estado (procesando / listo / error).
- Botón ``Voz`` estilo asistente de Google: **mantén pulsado para hablar**; el
  HUD muestra ``Hablando…`` mientras hablas y al soltar, la IA responde. Tu
  voz interrumpe (barge-in) la respuesta en curso del modelo. El botón
  ``Cancelar`` corta la respuesta actual y descarta lo pendiente.
- Conmutación instantánea de visibilidad con un atajo global (por defecto
  ``Ctrl+Shift+Z``) registrado con ``RegisterHotKey`` de Win32 en un hilo con
  su propia cola de mensajes: al pulsarlo, ``WM_HOTKEY`` despierta ese hilo y
  la señal se reenvía al hilo de Qt de forma segura mediante una cola de UI.
  Sin filtros de eventos nativos de Qt (ver nota abajo).
- No modifica el motor: el asistente corre en un hilo con su propio bucle
  asyncio y se le entregan órdenes con ``LiveAssistant.submit_command``,
  voz con ``LiveAssistant.set_voice`` y cortes con ``LiveAssistant.cancel``.

Ejecución::

    python overlay.py            # modo normal
    python overlay.py --no-auto  # sin arrancar el asistente automáticamente
    python overlay.py --smoke    # prueba de arranque de la ventana (1.5 s)

Nota: NO se usa ``QAbstractNativeEventFilter``: en PyQt6 6.11 (Windows) el
mero hecho de crear una instancia de ese tipo provoca un aborto nativo al
mostrar la ventana o registrar atajos Win32 (fastfail 0xC0000409). El atajo
global se resuelve con ``RegisterHotKey`` + un hilo de mensajes Win32.
"""

import asyncio
import ctypes
import ctypes.wintypes
from concurrent.futures import ThreadPoolExecutor
import html
import json
import logging
import math
import queue
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QEvent, QPointF, QRectF, QSize, QTimer, Qt
from PyQt6.QtGui import (
    QColor,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
    QTextCursor,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

import branding
from config import (
    DEFAULT_WEB_ENGINE,
    Settings,
    WEB_ENGINES,
    default_web_engine,
    normalize_web_engine,
)
from hotkeys import HotkeyController
from live_assistant import LiveAssistant
from media import list_input_devices, list_output_devices
from perf_instr import PERF as _perf
from prefs import apply_prefs, load_prefs, save_prefs

# Tokens de diseno (Fase 3/4): el QSS de este archivo toma color,
# radio y tipografia de aqui, sin cambiar los valores que ya se veian.
from ui import tokens as TKN

# Animaciones del HUD. Se animan sobre el PANEL (widget hijo), nunca con
# `setWindowOpacity`: esa última solo vale para ventanas de nivel superior y en
# un hijo Qt la ignora en silencio, así que el fundido no se vería. Además
# `setWindowOpacity` y ya está documentado que multiplicar el alpha de la
# ventana lo acumulaba con el `rgba` del fondo y dejaba el panel el doble de
# transparente.
from ui.animations import Desvanecer, Fade

# Capa de acento (Fase 5): anillo QML que respira con el estado del motor. Se
# importa la CLASE, pero la isla QML no se construye al importar ni al crear el
# HUD: solo cuando el motor entra en un estado que merece el acento. Así el
# arranque en frío no paga el motor QML (medido: 70-230 ms).
from ui.accento import HaloAcento

logger = logging.getLogger(__name__)

# Cada cuánto se refresca el panel de diagnóstico. Un segundo es suficiente
# para leerlo y de paso no añade trabajo apreciable: con el panel cerrado el
# timer ni siquiera corre.
_PERF_TICK_MS = 1000


def _chat_text(text: str) -> str:
    """Formato inteligente del chat para textos largos del modelo.

    - Respeta los párrafos explícitos del texto.
    - Pone en SU PROPIA fila cada "fase/etapa/paso/punto/parte/opción N",
      cada elemento de lista numerada ("1. …", "2) …") y cada viñeta "•",
      para que una respuesta larga quede acomodada y no toda regada.
    El texto es HTML ya escapado por ``html.escape``.
    """
    safe = html.escape(text or "")
    chunks = re.split(r"\n+", safe)
    lines: list[str] = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            lines.append("")
            continue
        # Fase/etapa/paso/punto/parte/opción/fase N: → su propia fila.
        chunk = re.sub(
            r"(?i)(?=(?:fase|etapa|paso|punto|parte|seccion|sección|"
            r"opcion|opción|numero|número)\s*\d+\s*[.:)])",
            "<br>",
            chunk,
        )
        # Lista numerada o viñeta SOLO al empezar segmento (tras punto, punto y
        # coma, dos puntos, signo de fin, salto o inicio) para no partir
        # "Opción 3)" ni los decimales "1.5":
        #   "Haz esto. 1. primero  2. luego" → cada "N." en su propia fila.
        chunk = re.sub(
            r"(?i)((?:^|[.;:!?…])\s*)(?=\d+[.)]\s+|[•])",
            lambda m: m.group(1) + "<br>",
            chunk,
        )
        lines.append(chunk)
    joined = "<br>".join(line for line in lines if line != "")
    return re.sub(r"^(?:<br>)+", "", joined)


_URL_RE = re.compile(r"(https?://[^\s<>\"')]+)", re.IGNORECASE)
# Puntuación final que casi nunca es parte del URL (la nota web usa el estilo
# "título — url: snippet", donde los ``:``/``,`` son separadores, no la URL).
_URL_TAIL = ".,;:!?…"


def _web_html(text: str) -> str:
    """HTML de una nota de resultados de búsqueda web (etiqueta ``[Web]``).

    Mantiene cada resultado en su propia fila y convierte las URLs en
    enlaces clicables que el chat (QTextBrowser) abre en el navegador. Las
    URLs se extraen del texto CRUDO (antes de escapar) para que caracteres
    como ``&`` no acaben doblemente escapados en el ``href``.
    """
    lines: list[str] = []
    for raw in (text or "").splitlines():
        raw = (raw or "").strip()
        if not raw:
            continue
        pieces = _URL_RE.split(raw)  # [antes, url, después, url, …]
        buff: list[str] = []
        for i, seg in enumerate(pieces):
            if i % 2 == 1:
                url = seg.rstrip(_URL_TAIL)
                if url:
                    href = html.escape(url, quote=True)
                    visible = html.escape(url)
                    buff.append(
                        f'<a href="{href}" style="color:#f0c67a;'
                        'text-decoration:underline;'
                        f'">{visible}</a>'
                    )
            else:
                buff.append(html.escape(seg))
        line_html = "".join(buff)
        if line_html.strip():
            lines.append(line_html)
    return "<br>".join(lines)


# Comandos cortos auto-completables (Tab) en la línea de órdenes. Coinciden con
# los disparadores que el motor reconoce (ver ``live_assistant``).
_SLASH_COMMANDS = (
    "/web ",
    "/clima",
    "/temporizadores",
    "/alarma 07:00",
    "/olvida todo",
    "/ayuda",
)

# Voces de síntesis de Gemini Live (el modelo rechaza con un aviso cualquier
# nombre que no esté disponible). Idiomas de transcripción BCP-47: el listado
# no es exhaustivo, basta con los habituales del asistente.
_VOICES = ("Puck", "Charon", "Kore", "Fenrir", "Aoede", "Leda", "Orus", "Zephyr")
_LANGUAGES = (
    ("es-ES", "Español"),
    ("en-US", "English"),
    ("fr-FR", "Français"),
    ("de-DE", "Deutsch"),
    ("it-IT", "Italiano"),
    ("pt-PT", "Português"),
)

# ---------------------------------------------------------------------------
# Atajo global Win32 (RegisterHotKey en un hilo con su propia cola de mensajes)
# ---------------------------------------------------------------------------
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
HOTKEY_ID = 0x0ACE
TOGGLE_PIPE = r"\\.\pipe\MindVoiceOverlayToggle"
CTRL_PIPE = r"\\.\pipe\MindVoiceHotkeyCtrl"
OVERLAY_MUTEX = "Local\\MindVoiceOverlayMutex"
LAUNCHER_MUTEX = "Local\\MindVoiceHotkeyMutex"
ERROR_ALREADY_EXISTS = 183
ERROR_PIPE_CONNECTED = 535
PIPE_ACCESS_DUPLEX = 0x0003
PIPE_TYPE_BYTE = 0x0000
PIPE_READMODE_BYTE = 0x0000
PIPE_WAIT = 0x0000
PIPE_UNLIMITED_INSTANCES = 0xFF

# Hook global de teclado (WH_KEYBOARD_LL) para el push-to-talk: mantén la tecla
# configurada `ptt_key` para hablar y suéltala para enviar la orden a la IA.
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
HC_ACTION = 0
LLKHF_INJECTED = 0x10
LLKHF_UP = 0x80
WPARAM = ctypes.c_ssize_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("vkCode", ctypes.wintypes.DWORD),
        ("scanCode", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# Tipado explícito de punteros (Win64): sin restype/argtypes, ctypes trunca
# los handles de 64 bits a 32 y SetWindowsHookExW/GetModuleHandleW fallan.
_user32.SetWindowsHookExW.restype = ctypes.c_void_p
_user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int, HOOKPROC, ctypes.c_void_p, ctypes.c_uint,
]
_user32.CallNextHookEx.restype = LRESULT
_user32.CallNextHookEx.argtypes = [
    ctypes.c_void_p, ctypes.c_int, WPARAM, LPARAM,
]
_user32.UnhookWindowsHookEx.restype = ctypes.c_int
_user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
_kernel32.GetModuleHandleW.restype = ctypes.c_void_p
_kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]

_VK_NAMES = {
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "A": 0x41, "B": 0x42, "C": 0x43, "D": 0x44, "E": 0x45,
    "F": 0x46, "G": 0x47, "H": 0x48, "I": 0x49, "J": 0x4A,
    "K": 0x4B, "L": 0x4C, "M": 0x4D, "N": 0x4E, "O": 0x4F,
    "P": 0x50, "Q": 0x51, "R": 0x52, "S": 0x53, "T": 0x54,
    "U": 0x55, "V": 0x56, "W": 0x57, "X": 0x58, "Y": 0x59,
    "Z": 0x5A,
    "SPACE": 0x20, "ENTER": 0x0D, "ESC": 0x1B, "TAB": 0x09,
    "BACKSPACE": 0x08, "CAPSLOCK": 0x14, "NUMLOCK": 0x90,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74,
    "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79,
    "F11": 0x7A, "F12": 0x7B,
    "INSERT": 0x2D, "DELETE": 0x2E, "HOME": 0x24, "END": 0x23,
    "PAGEUP": 0x21, "PAGEDOWN": 0x22,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "RIGHT CTRL": 0xA3, "LEFT CTRL": 0xA2, "CTRL": 0x11,
    "RIGHT SHIFT": 0xA1, "LEFT SHIFT": 0xA0, "SHIFT": 0x10,
    "RIGHT ALT": 0xA5, "LEFT ALT": 0xA4, "ALT": 0x12,
}


def _parse_hotkey(spec: str, default: str = "Ctrl+Shift+Z") -> tuple[int, int]:
    """Convierte ''Ctrl+Shift+Z'' en (modificadores, código VK) de Win32."""
    if not spec or not isinstance(spec, str):
        spec = default
    modifiers = 0
    key = None
    for token in spec.split("+"):
        part = token.strip().upper()
        if part in ("CTRL", "CONTROL"):
            modifiers |= MOD_CONTROL
        elif part == "ALT":
            modifiers |= MOD_ALT
        elif part in ("SHIFT", "MAYÚS"):
            modifiers |= MOD_SHIFT
        elif part in ("WIN", "WINKEY", "SUPER"):
            modifiers |= MOD_WIN
        elif key is None:
            key = _VK_NAMES.get(part)
    if key is None:
        logger.error("Atajo global inválido: %r (se ignora)", spec)
        return 0, 0
    return modifiers, key


def _parse_ptt_key(spec: str) -> int:
    """Convierte la tecla de voz (``ptt_key``) en el código VK de Win32."""
    if not spec or not isinstance(spec, str):
        return 0
    return int(_VK_NAMES.get(spec.strip().upper()) or 0)


def _mutex_exists(name: str) -> bool:
    handle = _kernel32.OpenMutexW(0x1F0001, False, name)
    if handle:
        _kernel32.CloseHandle(handle)
        return True
    return False

# OJO con las llaves dobles: estos bloques son cadenas NORMALES, no f-strings,
# así que `{{` sale tal cual en el QSS, que es como lo lleva leyendo Qt. Por eso
# los tokens van por concatenación y no interpolados: si esto pasara a ser
# f-string, habría que escribir `{{{{`, y es la clase de cambio que rompe el
# QSS sin que se note. Solo se sustituyen valores que coinciden EXACTOS con un
# token; un color parecido se deja como estaba.
_BTN_QSS = (
    "QPushButton {{ background: rgba(255,255,255,24); color:" + TKN.tinta + ";"
    " border:1px solid rgba(255,255,255,64); border-radius:"
    + f"{TKN.radio_tarjeta}px;"
    " padding:5px 14px; font:" + f"{TKN.tamano_cuerpo}px" + " 'Consolas'; }}"
    "QPushButton:hover {{ background: rgba(255,255,255,42); }}"
)
_BTN_CANCEL_QSS = (
    "QPushButton {{ background: rgba(255,90,90,40); color:" + TKN.rojo_tenue + ";"
    " border:1px solid rgba(255,120,120,90); border-radius:"
    + f"{TKN.radio_tarjeta}px;"
    " padding:5px 14px; font:" + f"{TKN.tamano_cuerpo}px" + " 'Consolas'; }}"
    "QPushButton:hover {{ background: rgba(255,90,90,80); }}"
)
_BTN_POWER_QSS = (
    "QPushButton {{ background: rgba(255,45,45,90); color:#ffe1e1;"
    " border:1px solid rgba(255,80,80,150); border-radius:"
    + f"{TKN.radio_tarjeta}px;"
    " padding:5px 14px; font:" + f"{TKN.tamano_cuerpo}px" + " 'Consolas'; }}"
    "QPushButton:hover {{ background: rgba(255,60,60,140); }}"
)
# Botones de icono mínimo (38x38, solo glifo, esquinas suaves).
_ICO_QSS = (
    "QPushButton {{ background: rgba(255,255,255,22); color:" + TKN.tinta + ";"
    " border:1px solid rgba(255,255,255,60); border-radius:"
    + f"{TKN.radio_bloque}px; " + "}}"
    "QPushButton:hover {{ background: rgba(255,255,255,44); }}"
    "QPushButton:pressed {{ background: rgba(255,255,255,60); }}"
)
_ICO_RED_QSS = (
    "QPushButton {{ background: rgba(255,90,90,42); color:" + TKN.rojo_tenue + ";"
    " border:1px solid rgba(255,120,120,95); border-radius:"
    + f"{TKN.radio_bloque}px; " + "}}"
    "QPushButton:hover {{ background: rgba(255,90,90,90); }}"
    "QPushButton:pressed {{ background: rgba(255,110,110,120); }}"
)
_CHIP_QSS = (
    "QPushButton {{ background: rgba(255,255,255,16); color:" + TKN.tinta_borde + ";"
    " border:1px solid rgba(255,255,255,45); border-radius:9px;"
    " padding:3px 10px; font:11px 'Consolas'; }}"
    "QPushButton:hover {{ background: rgba(255,255,255,34); color:#eef2f7; }}"
)
_GLYPH = QColor(235, 240, 246)
_GLYPH_SOFT = QColor(150, 168, 190)
_GLYPH_RED = QColor(255, 215, 215)


def _make_glyph(draw, size: int = 38, color=None) -> QPixmap:
    """Dibuja un glifo minimalista en un ``QPixmap`` transparente."""
    if color is None:
        color = _GLYPH
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(size / 32.0, size / 32.0)
    pen = QPen(QColor(color), 2.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    draw(painter)
    painter.end()
    return pix


def _draw_mic(p) -> None:
    p.drawRoundedRect(QRectF(13, 3.5, 6, 11), 2.4, 2.4)
    p.drawArc(QRectF(10.5, 12.5, 11, 10), 0, 180 * 16)
    p.drawLine(QPointF(16, 20.5), QPointF(16, 24.5))


def _draw_cancel(p) -> None:
    p.drawLine(QPointF(9.5, 9.5), QPointF(22.5, 22.5))
    p.drawLine(QPointF(22.5, 9.5), QPointF(9.5, 22.5))


def _draw_clear(p) -> None:
    p.drawEllipse(QRectF(8.5, 8.5, 15, 15))
    p.drawLine(QPointF(12.5, 12.5), QPointF(19.5, 19.5))
    p.drawLine(QPointF(19.5, 12.5), QPointF(12.5, 19.5))


def _draw_power(p) -> None:
    p.drawArc(QRectF(9.5, 7.5, 13, 13), 135 * 16, 270 * 16)
    p.drawLine(QPointF(16, 6.5), QPointF(16, 16.5))


def _draw_gear(p) -> None:
    p.drawEllipse(QRectF(11, 11, 10, 10))
    cx = cy = 16.0
    for step in range(8):
        angle = math.radians(step * 45)
        cos, sin = math.cos(angle), math.sin(angle)
        p.drawLine(
            QPointF(cx + cos * 10.5, cy + sin * 10.5),
            QPointF(cx + cos * 14.5, cy + sin * 14.5),
        )
    p.setBrush(QColor(24, 28, 38))
    p.drawEllipse(QRectF(14, 14, 4, 4))


def _draw_eye(p) -> None:
    """Glifo de ojo con la pupila abierta (privacidad: contexto visual)."""
    p.drawEllipse(QRectF(5.5, 11, 21, 10))
    p.drawEllipse(QRectF(13.5, 13.5, 5, 5))
    p.drawLine(QPointF(13.5, 20.5), QPointF(18.5, 20.5))


def _draw_brain(p) -> None:
    """Glifo de cerebro (olvidar la memoria del modelo)."""
    p.drawArc(QRectF(8.5, 10, 11, 12), 0, 180 * 16)
    p.drawArc(QRectF(12.5, 10, 11, 12), 0, 180 * 16)
    p.drawLine(QPointF(14, 22), QPointF(14, 25.5))
    p.drawLine(QPointF(18, 22), QPointF(18, 25.5))


def robot_logo_pixmap(size: int) -> QPixmap:
    """Logo 8-bit del robot dibujado píxel a píxel (Qt puro, sin ficheros)."""
    rows = [branding.pixel_row_colors(row) for row in branding.ROBOT_PIXELS]
    height = len(rows)
    width = len(rows[0]) if rows else 1
    image = QPixmap(width, height)
    image.fill(Qt.GlobalColor.transparent)
    from PyQt6.QtGui import QPainter as _QP
    _p = _QP(image)
    for y, row in enumerate(rows):
        for x, rgba in enumerate(row):
            _p.fillRect(x, y, 1, 1, QColor(*rgba))
    _p.end()
    return image.scaled(
        int(size),
        int(size),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )


class OverlayHud(QWidget):
    """Ventana fullscreen transparente con el HUD de MindVoice."""

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        self._ui_queue: "queue.Queue[tuple[str, object | None]]" = queue.Queue()
        self._transcript_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mindvoice-transcript"
        )
        self._lvl_pending = False  # coalesce: a lo sumo un nivel de voz en cola
        self._thread: threading.Thread = None
        self._loop: asyncio.AbstractEventLoop = None
        self._assistant: LiveAssistant = None
        self._processing = False
        self._talking = False
        self._continuous_voice = False
        self._lock = threading.Lock()
        self._hotkey_thread: threading.Thread = None
        self._hotkey_thread_id: int = 0
        self._hotkey_stop = False
        self._toggle_thread: threading.Thread = None
        self._single_mutex: int = 0
        self._ptt_thread: threading.Thread = None
        self._ptt_thread_id: int = 0
        self._ptt_stop = False
        self._ptt_vk = 0
        self._ptt_held = False
        self._ptt_hook: int = 0
        self._ptt_failures = 0
        self._ptt_proc: HOOKPROC = None
        # Historial de órdenes (↑/↓) y autocompletado slash (Tab).
        self._cmd_history: list[str] = []
        self._cmd_idx = -1
        self._slash_cycle = 0
        # Última respuesta del modelo (para las acciones Copiar/Repetir).
        self._ia_turn: list[str] = []
        self._last_ia = ""
        # Estado de silencio de salida (HotkeyController.muted) reflejado en el HUD.
        self._muted = False
        # Watchdog del motor: si el hilo del asistente muere por un error fatal
        # en segundo plano, se reinicia solo (con tope de frecuencia) para que
        # MindVoice no quede "detenido" hasta reiniciar la app.
        self._worker_started = False
        self._watchdog_stop = False
        self._last_worker_start: float = 0.0
        # Parada VOLUNTARIA (atajo de salir / botón): el watchdog no debe
        # resucitar el motor en ese caso, y la app se cierra.
        self._quit_requested = False
        self._worker_started_once = False
        self._restarts = 0
        self._quick_deaths = 0
        # Privacidad en sesión: oculta el contexto visual al modelo sin esperar
        # a Guardar (cambia screen_enabled en caliente).
        self._privacy_off = not bool(self._settings.screen_enabled)
        # Panel de diagnóstico (Fase 4). Apagado por defecto y sin coste
        # mientras lo está: sin timer corriendo y sin hueco en el layout. Los
        # atributos se crean aquí, antes del singleton, para que el atajo y el
        # tick no tengan que comprobar el caso de "otra instancia del HUD".
        self._perf_visible = False
        self._perf_texto = ""
        self._perf_box: QFrame | None = None
        self._perf_lbl: QLabel | None = None
        self._perf_timer: QTimer | None = None
        self._perf_shortcut: QShortcut | None = None
        # Capa de acento (Fase 5). Igual que el panel de diagnóstico, se declara
        # aquí y se construye en `_build_panel`: así una instancia duplicada del
        # HUD (que retorna antes de construir) no tiene un halo a medias.
        self._halo: HaloAcento | None = None
        self._halo_estado = ""

        self._single_ok = self._acquire_single_instance()
        if not self._single_ok:
            logger.warning("Ya hay una instancia del overlay; esta se cierra.")
            return

        self._tokens_prompt = 0
        self._tokens_response = 0
        try:
            self._tokens_lifetime = int(load_prefs().get("tokens_lifetime") or 0)
        except Exception:  # noqa: BLE001 - cosmético
            self._tokens_lifetime = 0

        self.setWindowTitle("MindVoice Overlay")
        try:
            self.setWindowIcon(QIcon(str(branding.LOGO_ICO)))
        except Exception:  # noqa: BLE001 - el icono es cosmético
            pass
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        screen = QApplication.primaryScreen()
        if settings.overlay_monitor > 1:
            screens = QApplication.screens()
            if settings.overlay_monitor <= len(screens):
                screen = screens[settings.overlay_monitor - 1]
        self._screen_geometry = screen.geometry()
        self.setGeometry(self._screen_geometry)
        self._build_panel()
        # Marca de arranque (Fase 0): el árbol de widgets ya existe, así que a
        # partir de aquí lo que se mide es cuánto tarda en verse en pantalla.
        _perf.mark_ui_built()
        self._setup_hotkey()
        self._start_toggle_pipe()
        self._setup_ptt()

        if settings.overlay_show_on_start:
            QTimer.singleShot(0, self.show_overlay)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _panel_qss(self, base: str) -> str:
        """Hoja de estilo del panel con el alpha de fondo según la opacidad."""
        alpha = max(76, min(242, int(255 * float(self._settings.overlay_opacity or 0.7))))
        return (
            f"QWidget#panel {{ background: rgba({base},{alpha});"
            f" border-radius: 18px; }}"
        )

    def _apply_opacity(self, value: float) -> None:
        """Fija la opacidad aplicando el alpha al FONDO del panel.

        La opacidad va solo en el ``rgba`` del fondo, nunca con
        ``setWindowOpacity``: esta última multiplica el alpha de toda la
        ventana (el texto se vuelve translúcido) y se ACUMULABA con el
        ``rgba`` del panel, así que tras el primer "Guardar" el panel quedaba
        el doble de transparente y con un aspecto distinto al del arranque.
        """
        self._settings.overlay_opacity = max(0.3, min(0.95, float(value)))
        panel = getattr(self, "panel", None)
        if panel is not None:
            panel.setStyleSheet(self._panel_qss("18,22,30"))

    def _build_panel(self) -> None:
        base = "18,22,30"
        W = self._screen_geometry.width()
        H = self._screen_geometry.height()
        panel_w = min(820, int(W * 0.52))
        # El panel NUNCA puede medir más que la pantalla: con el panel de
        # ajustes abierto la suma de filas se pasa de alto (a 150 % de
        # escalado, de sobra) y la barra de órdenes -el último widget del
        # layout- se iba por debajo del borde inferior.
        panel_max_h = max(320, H - 32)
        chat_h = max(240, min(int(H * 0.32), 420))
        # Holgura mínima del chat cuando hay que ceder espacio. Si el panel se
        # queda corto, el chat cede primero y el resto (chips + barra de
        # órdenes) siempre queda visible.
        chat_min_h = min(chat_h, max(120, int(H * 0.16)))

        self.panel = QWidget(self)
        self.panel.setObjectName("panel")
        self.panel.setFixedWidth(panel_w)
        self.panel.setMaximumHeight(panel_max_h)
        self.panel.setStyleSheet(self._panel_qss(base))

        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(8)

        # ---- Cabecera: logo robot 8-bit + nombre ---------------------------
        header = QHBoxLayout()
        header.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(robot_logo_pixmap(46))
        logo.setFixedSize(46, 46)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(logo)

        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        title = QLabel("MindVoice")
        title.setStyleSheet(
            "color:#eef2f7; font-size:17px; font-weight:bold;"
            " font-family:'Consolas'; letter-spacing:1px;"
        )
        title_col.addWidget(title)
        self._subtitle = QLabel("asistente en vivo · robot 8-bit")
        self._subtitle.setStyleSheet(
            f"color:{TKN.tinta_tenue}; font:11px 'Consolas'; letter-spacing:0.5px;"
        )
        title_col.addWidget(self._subtitle)
        header.addLayout(title_col)
        header.addStretch(1)
        layout.addLayout(header)

        # ---- Fila de estado y acciones -------------------------------------
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self._dot = QLabel(" ")
        self._dot.setFixedSize(12, 12)
        self._dot.setStyleSheet(f"background:{TKN.verde};border-radius:6px;")
        self._cap = QLabel("")
        self._cap.setStyleSheet(f"color:{TKN.tinta_suave};font:11px 'Consolas';")
        self._relabel_cap()
        status_row.addWidget(self._dot)
        status_row.addWidget(self._cap)

        self._state_lbl = QLabel("")
        self._state_lbl.setStyleSheet(f"color:{TKN.tinta_suave};font:11px 'Consolas';")
        self._state_lbl.setToolTip("Estado del motor (colas, voces, sesión)")
        status_row.addWidget(self._state_lbl)

        self._timers_lbl = QLabel("")
        self._timers_lbl.setStyleSheet(f"color:{TKN.ambar};font:11px 'Consolas';")
        self._timers_lbl.setToolTip("Temporizadores pendientes")
        status_row.addWidget(self._timers_lbl)

        self._mute_lbl = QLabel("")
        self._mute_lbl.setStyleSheet(f"color:{TKN.amarillo};font:11px 'Consolas';")
        self._mute_lbl.setToolTip("La salida de voz está silenciada")
        status_row.addWidget(self._mute_lbl)

        self._tokens_label = QLabel("tokens 0")
        self._tokens_label.setStyleSheet(
            f"color:{TKN.verde};font:11px 'Consolas';letter-spacing:0.5px;"
        )
        self._tokens_label.setToolTip("Tokens consumidos (prompt + respuesta)")
        status_row.addWidget(self._tokens_label)
        status_row.addStretch(1)

        self._lvl = QLabel("")
        self._lvl.setStyleSheet(f"color:{TKN.tinta_tenue};font:11px 'Consolas';")
        status_row.addWidget(self._lvl)

        self._privacy_btn = QPushButton()
        self._privacy_btn.setIcon(QIcon(_make_glyph(_draw_eye)))
        self._privacy_btn.setIconSize(QSize(28, 28))
        self._privacy_btn.setFixedSize(38, 38)
        self._privacy_btn.setStyleSheet(_ICO_RED_QSS if self._privacy_off else _ICO_QSS)
        self._privacy_btn.setToolTip(
            "Privacidad activa: MindVoice ya no ve la pantalla"
            if self._privacy_off
            else "Privacidad: no mostrar la pantalla al modelo"
        )
        self._privacy_btn.clicked.connect(self._toggle_privacy)
        status_row.addWidget(self._privacy_btn)

        self._forget_btn = QPushButton()
        self._forget_btn.setIcon(QIcon(_make_glyph(_draw_brain)))
        self._forget_btn.setIconSize(QSize(28, 28))
        self._forget_btn.setFixedSize(38, 38)
        self._forget_btn.setStyleSheet(_ICO_QSS)
        self._forget_btn.setToolTip("Olvidar: borra la memoria breve del modelo")
        self._forget_btn.clicked.connect(self._on_olvidar)
        status_row.addWidget(self._forget_btn)

        self._settings_btn = QPushButton()
        self._settings_btn.setIcon(QIcon(_make_glyph(_draw_gear)))
        self._settings_btn.setIconSize(QSize(28, 28))
        self._settings_btn.setFixedSize(38, 38)
        self._settings_btn.setStyleSheet(_ICO_QSS)
        self._settings_btn.setToolTip("Ajustes de audio")
        self._settings_btn.clicked.connect(self._toggle_settings)
        status_row.addWidget(self._settings_btn)

        self._voice_btn = QPushButton(" Voz")
        self._voice_btn.setIcon(QIcon(_make_glyph(_draw_mic)))
        self._voice_btn.setIconSize(QSize(20, 20))
        self._voice_btn.setStyleSheet(_BTN_QSS)
        self._voice_btn.pressed.connect(self._voice_start)
        self._voice_btn.released.connect(self._voice_stop)
        self._voice_btn.setToolTip("Mantén pulsado para hablar")
        status_row.addWidget(self._voice_btn)

        self._cancel_btn = QPushButton()
        self._cancel_btn.setIcon(QIcon(_make_glyph(_draw_cancel, color=_GLYPH_RED)))
        self._cancel_btn.setIconSize(QSize(28, 28))
        self._cancel_btn.setFixedSize(38, 38)
        self._cancel_btn.setStyleSheet(_ICO_RED_QSS)
        self._cancel_btn.setToolTip("Cancelar respuesta")
        self._cancel_btn.clicked.connect(self._on_cancel)
        status_row.addWidget(self._cancel_btn)

        self._clear_btn = QPushButton()
        self._clear_btn.setIcon(QIcon(_make_glyph(_draw_clear)))
        self._clear_btn.setIconSize(QSize(28, 28))
        self._clear_btn.setFixedSize(38, 38)
        self._clear_btn.setStyleSheet(_ICO_QSS)
        self._clear_btn.setToolTip("Limpiar conversación de la pantalla")
        self._clear_btn.clicked.connect(self._on_clear)
        status_row.addWidget(self._clear_btn)

        self._power_btn = QPushButton()
        self._power_btn.setIcon(QIcon(_make_glyph(_draw_power, color=_GLYPH_RED)))
        self._power_btn.setIconSize(QSize(28, 28))
        self._power_btn.setFixedSize(38, 38)
        self._power_btn.setStyleSheet(_ICO_RED_QSS)
        self._power_btn.setToolTip("Apagar MindVoice")
        self._power_btn.clicked.connect(self._on_power)
        status_row.addWidget(self._power_btn)

        layout.addLayout(status_row)

        # ---- Panel de diagnóstico (Fase 4) ----------------------------------
        # Nace oculto. Con `setVisible(False)` el layout no le reserva hueco,
        # así que el HUD de siempre se ve y se mide igual que antes de que
        # esto existiera. Abrirlo enciende la instrumentación en caliente y
        # arranca su timer; cerrarlo los para.
        self._perf_box = QFrame()
        self._perf_box.setObjectName("perf")
        self._perf_box.setFrameShape(QFrame.Shape.NoFrame)
        self._perf_box.setStyleSheet(
            "QFrame#perf { background: rgba(255,255,255,18); border:1px solid"
            " rgba(255,255,255,36); border-radius:"
            + str(TKN.radio_bloque)
            + "px; }"
        )
        perf_layout = QVBoxLayout(self._perf_box)
        perf_layout.setContentsMargins(8, 6, 8, 6)
        perf_layout.setSpacing(2)
        self._perf_lbl = QLabel("")
        self._perf_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self._perf_lbl.setStyleSheet(
            "color:" + TKN.cian + "; font:11px 'Consolas'; background:transparent;"
        )
        self._perf_lbl.setToolTip(
            "Métricas del HUD, pedidas en caliente al abrir este panel.\n"
            "Ctrl+Shift+D lo abre y lo cierra.\n"
            "ciclos = vueltas del volcado a pantalla; tirones = tramos sin\n"
            "refrescar (eso es lo que se ve como un tirón)."
        )
        perf_layout.addWidget(self._perf_lbl)
        layout.addWidget(self._perf_box)
        self._perf_box.setVisible(False)

        # ---- Chat real: QTextBrowser soporta HTML y abre enlaces externos
        # ---- (los resultados web se publican con URLs clicables).
        self.chat = QTextBrowser()
        self.chat.setObjectName("chat")
        self.chat.setReadOnly(True)
        self.chat.setOpenExternalLinks(True)
        self.chat.setFrameShape(QFrame.Shape.NoFrame)
        # Rango, no altura fija: con un ``setFixedHeight`` el chat no podía
        # ceder ni un píxel y al abrir Ajustes empujaba la barra de órdenes
        # fuera de la pantalla. Con min/max el layout lo encoge lo justo.
        self.chat.setMinimumHeight(chat_min_h)
        self.chat.setMaximumHeight(chat_h)
        self.chat.setStyleSheet(
            "QTextBrowser#chat { background: transparent; border: none;"
            " color: " + TKN.tinta + "; font: 13px 'Consolas'; }"
        )
        layout.addWidget(self.chat)

        # ---- Acciones rápidas sobre la última respuesta (ocultas hasta que
        # ---- el modelo haya hablado al menos una vez)
        self._acts = QWidget()
        acts_lay = QHBoxLayout(self._acts)
        acts_lay.setContentsMargins(0, 0, 0, 0)
        acts_lay.setSpacing(6)
        acts_lab = QLabel("ÚLTIMA RESPUESTA")
        acts_lab.setStyleSheet(
            "color:#6f8299;font:10px 'Consolas';letter-spacing:1px;"
        )
        acts_lay.addWidget(acts_lab)
        acts_lay.addStretch(1)
        self._copy_btn = QPushButton("Copiar")
        self._copy_btn.setStyleSheet(_BTN_QSS)
        self._copy_btn.setToolTip("Copia la última respuesta al portapapeles")
        self._copy_btn.clicked.connect(self._on_copy_last)
        acts_lay.addWidget(self._copy_btn)
        self._repeat_btn = QPushButton("Repetir")
        self._repeat_btn.setStyleSheet(_BTN_QSS)
        self._repeat_btn.setToolTip("Pide al modelo que repita su última respuesta")
        self._repeat_btn.clicked.connect(self._on_repeat_last)
        acts_lay.addWidget(self._repeat_btn)
        self._acts_wanted = False
        self._acts.hide()
        layout.addWidget(self._acts)

        # ---- Panel de ajustes (oculto por defecto) -------------------------
        self._settings_box = QFrame()
        self._settings_box.setObjectName("settings")
        self._settings_box.setStyleSheet(
            "QFrame#settings { background: rgba(24,28,38,190);"
            " border: 1px solid rgba(255,255,255,40); border-radius: 12px; }"
        )
        s_layout = QVBoxLayout(self._settings_box)
        s_layout.setContentsMargins(12, 10, 12, 10)
        s_layout.setSpacing(6)

        s_title = QLabel("AJUSTES")
        s_title.setStyleSheet(
            f"color:{TKN.tinta_suave}; font:10px 'Consolas'; letter-spacing:2px;"
        )
        s_layout.addWidget(s_title)

        # Los selectores se rellenan en un hilo de fondo: list_*_devices() crea y
        # destruye una instancia de PyAudio (bloqueante). Hacerlo aquí congelaba
        # el hilo de Qt y, si una tarjeta de sonido estaba colgada, la UI entera.
        self._mic_combo = self._device_combo([], True)
        s_layout.addWidget(self._combo_row("MICRÓFONO", self._mic_combo, True))
        self._out_combo = self._device_combo([], False)
        s_layout.addWidget(self._combo_row("ALTAVOCES", self._out_combo, False))

        self._voice_combo = QComboBox()
        for _voice in _VOICES:
            self._voice_combo.addItem(_voice, _voice)
        s_layout.addWidget(self._combo_row("VOZ", self._voice_combo, False))

        self._lang_combo = QComboBox()
        for _code, _label in _LANGUAGES:
            self._lang_combo.addItem(f"{_label} ({_code})", _code)
        s_layout.addWidget(self._combo_row("IDIOMA", self._lang_combo, False))

        vol_row = QHBoxLayout()
        vol_row.setSpacing(8)
        vol_lab = QLabel("VOLUMEN")
        vol_lab.setStyleSheet(f"color:{TKN.tinta_suave};font:10px 'Consolas';letter-spacing:1px;")
        vol_row.addWidget(vol_lab)
        self._vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._vol_slider.setRange(0, 150)
        self._vol_slider.setValue(
            int(max(0.0, min(1.5, float(self._settings.output_volume or 1.0))) * 100)
        )
        self._vol_slider.setStyleSheet(
            "QSlider::groove:horizontal { height:4px; background:"
            " rgba(255,255,255,40); border-radius:2px; }"
            "QSlider::handle:horizontal { width:14px; height:14px; margin:-5px 0;"
            " background:" + TKN.verde + "; border-radius:7px; }"
        )
        vol_row.addWidget(self._vol_slider, 1)
        self._vol_label = QLabel(f"{self._vol_slider.value()}%")
        self._vol_label.setStyleSheet(f"color:{TKN.tinta};font:11px 'Consolas';")
        self._vol_slider.valueChanged.connect(
            lambda v: self._vol_label.setText(f"{v}%")
        )
        vol_row.addWidget(self._vol_label)
        s_layout.addLayout(vol_row)

        self._cont_check = QCheckBox("Escucha continua (el micrófono queda activo al abrir la app)")
        self._cont_check.setStyleSheet(
            f"color:{TKN.tinta_media}; font:11px 'Consolas';"
            " QCheckBox::indicator { width:14px; height:14px; }"
        )
        self._cont_check.setChecked(bool(self._settings.overlay_voice_on_start))
        self._cont_check.toggled.connect(
            lambda on: (self.set_voice_wanted(on) if on else self._disengage_voice())
        )
        s_layout.addWidget(self._cont_check)

        self._web_check = QCheckBox("Búsqueda web con stickies y /web")
        self._web_check.setStyleSheet(
            f"color:{TKN.tinta_media}; font:11px 'Consolas';"
            " QCheckBox::indicator { width:14px; height:14px; }"
        )
        self._web_check.setChecked(bool(self._settings.web_search_enabled))
        s_layout.addWidget(self._web_check)

        self._screen_check = QCheckBox("Ver pantalla (contexto visual al atender órdenes)")
        self._screen_check.setStyleSheet(
            f"color:{TKN.tinta_media}; font:11px 'Consolas';"
            " QCheckBox::indicator { width:14px; height:14px; }"
        )
        self._screen_check.setChecked(bool(self._settings.screen_enabled))
        s_layout.addWidget(self._screen_check)

        self._transcript_check = QCheckBox("Guardar transcripción diaria en data_dir/sessions")
        self._transcript_check.setStyleSheet(
            f"color:{TKN.tinta_media}; font:11px 'Consolas';"
            " QCheckBox::indicator { width:14px; height:14px; }"
        )
        self._transcript_check.setChecked(bool(getattr(self._settings, "save_transcripts", True)))
        s_layout.addWidget(self._transcript_check)

        self._manual_vad_check = QCheckBox(
            "Hablar sin límite: la IA contesta al soltar el botón"
        )
        self._manual_vad_check.setStyleSheet(
            f"color:{TKN.tinta_media}; font:11px 'Consolas';"
            " QCheckBox::indicator { width:14px; height:14px; }"
        )
        self._manual_vad_check.setToolTip(
            "Activado: puedes hablar indefinidamente y la IA espera a que "
            "sueltes el botón para responder (no se corta por pausas).\n"
            "Desactivado: la IA decide el fin de turno por el silencio."
        )
        self._manual_vad_check.setChecked(
            bool(getattr(self._settings, "voice_manual_vad", True))
        )
        s_layout.addWidget(self._manual_vad_check)

        # Silencio que cierra el turno en modo alterno. Es el ajuste que hace
        # que "pulsas, hablas y esperas" funcione, así que va justo debajo del
        # modo de voz y no escondido en un advanced.
        silencio = QDoubleSpinBox()
        silencio.setRange(0.0, 15.0)
        silencio.setSingleStep(0.5)
        silencio.setDecimals(1)
        silencio.setSuffix(" s")
        silencio.setValue(float(getattr(self._settings, "voice_toggle_silence", 2.5) or 0.0))
        silencio.setToolTip(
            "En modo alterno, cuántos segundos de silencio cierran el turno y "
            "envían lo que has dicho.\n"
            "0 = solo se envía al pulsar por segunda vez (el comportamiento "
            "antiguo, que no avisaba de nada).\n"
            "1 s era demasiado corto: cortaba la frase a mitad al pensar."
        )
        self._toggle_silence_spin = silencio
        s_layout.addWidget(self._combo_row("CIERRE POR SILENCIO", silencio, False))

        # ---- Memoria ----------------------------------------------------
        # El grafo de memoria no tenía ningún control: sepongía solo, growing
        # sin freno, hasta llegar a megabytes. Aquí se puede apagar, limitar por
        # cantidad y por antigüedad, y vaciarlo entero.
        self._memory_check = QCheckBox("Recordar entre sesiones (memoria)")
        self._memory_check.setStyleSheet(
            f"color:{TKN.tinta_media}; font:11px 'Consolas';"
            " QCheckBox::indicator { width:14px; height:14px; }"
        )
        self._memory_check.setToolTip(
            "Activado: MindVoice recuerda lo que cuentas y lo usa al responder.\n"
            "Desactivado: cada turno va sin memoria, como la primera versión."
        )
        self._memory_check.setChecked(bool(getattr(self._settings, "memory_enabled", True)))
        s_layout.addWidget(self._memory_check)

        self._memory_nodes_spin = QSpinBox()
        self._memory_nodes_spin.setRange(0, 200000)
        self._memory_nodes_spin.setSingleStep(250)
        self._memory_nodes_spin.setSpecialValueText("sin tope")
        self._memory_nodes_spin.setValue(
            int(getattr(self._settings, "memory_max_nodes", 2000) or 0)
        )
        self._memory_nodes_spin.setToolTip(
            "Máximo de recuerdos guardados. Al superar el tope se olvidan los\n"
            "menos importantes, nunca los más recientes. 0 = sin tope."
        )
        s_layout.addWidget(self._combo_row("TOPE DE RECUERDOS", self._memory_nodes_spin, False))

        self._memory_days_spin = QSpinBox()
        self._memory_days_spin.setRange(0, 3650)
        self._memory_days_spin.setSuffix(" días")
        self._memory_days_spin.setSpecialValueText("sin tope")
        self._memory_days_spin.setValue(
            int(getattr(self._settings, "memory_retention_days", 90) or 0)
        )
        self._memory_days_spin.setToolTip(
            "Antigüedad máxima de un recuerdo. 0 = se guarda para siempre."
        )
        s_layout.addWidget(self._combo_row("ANTIGÜEDAD", self._memory_days_spin, False))

        self._memory_stats = QLabel("")
        self._memory_stats.setStyleSheet(
            f"color:{TKN.tinta_suave}; font:10px 'Consolas';"
        )
        s_layout.addWidget(self._memory_stats)

        self._memory_purge_btn = QPushButton("Vaciar memoria")
        self._memory_purge_btn.setStyleSheet(_BTN_CANCEL_QSS)
        self._memory_purge_btn.setToolTip(
            "Olvida todos los recuerdos conservando el hilo de los temas.\n"
            "No se puede deshacer."
        )
        self._memory_purge_btn.clicked.connect(self._on_purge_memory)
        s_layout.addWidget(self._memory_purge_btn)

        self._modes_combo = QComboBox()
        self._modes_combo.addItem("Solo voz", "audio")
        self._modes_combo.addItem("Voz + texto en pantalla", "audio_text")
        s_layout.addWidget(self._combo_row("SALIDA", self._modes_combo, False))

        # ---- Aspecto y comportamiento del HUD (I6-I11) ---------------------
        op_row = QHBoxLayout()
        op_row.setSpacing(8)
        op_lab = QLabel("OPACIDAD")
        op_lab.setStyleSheet(f"color:{TKN.tinta_suave};font:10px 'Consolas';letter-spacing:1px;")
        op_row.addWidget(op_lab)
        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(30, 95)
        self._opacity_slider.setValue(
            int(max(0.3, min(0.95, float(self._settings.overlay_opacity or 0.7))) * 100)
        )
        self._opacity_slider.setStyleSheet(
            "QSlider::groove:horizontal { height:4px; background:"
            " rgba(255,255,255,40); border-radius:2px; }"
            "QSlider::handle:horizontal { width:14px; height:14px; margin:-5px 0;"
            " background:" + TKN.verde + "; border-radius:7px; }"
        )
        op_row.addWidget(self._opacity_slider, 1)
        self._opacity_val = QLabel(f"{self._opacity_slider.value()}%")
        self._opacity_val.setStyleSheet(f"color:{TKN.tinta};font:11px 'Consolas';")
        self._opacity_slider.valueChanged.connect(
            lambda v: self._opacity_val.setText(f"{v}%")
        )
        # Vista previa inmediata: el alpha del panel sigue al deslizador, así
        # que el ajuste se ve al momento (antes solo cambiaba al guardar).
        self._opacity_slider.valueChanged.connect(
            lambda v: self._apply_opacity(v / 100.0)
        )
        op_row.addWidget(self._opacity_val)
        s_layout.addLayout(op_row)

        self._lines_combo = QComboBox()
        for _n in (30, 60, 100, 150):
            self._lines_combo.addItem(f"{_n} líneas", _n)
        s_layout.addWidget(self._combo_row("HISTORIAL", self._lines_combo, False))

        # El combo se puebla desde config.WEB_ENGINES: si algún día se añade o
        # quita un motor, el menú se actualiza solo y no puede desincronizarse
        # de lo que realmente hace la búsqueda.
        self._web_provider_combo = QComboBox()
        for _pkey in WEB_ENGINES:
            self._web_provider_combo.addItem(
                WEB_ENGINES[_pkey]["label"], _pkey
            )
        self._web_provider_combo.setCurrentIndex(
            max(0, self._web_provider_combo.findData(
                normalize_web_engine(self._settings.web_search_provider)))
        )
        s_layout.addWidget(self._combo_row("BUSCADOR", self._web_provider_combo, False))

        # Clave de los motores que la necesitan. Se muestra solo cuando el
        # motor elegido la pide (lo declara el registro, no una lista aquí), y
        # se escribe en user_prefs.json al guardar: así la clave vive dentro de
        # MindVoice y se cambia desde este panel sin tocar Windows.
        self._api_key_edit = QLineEdit(str(self._settings.serper_api_key or ""))
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.setPlaceholderText("clave de serper.dev (serper.dev/apikey)")
        self._api_key_edit.setToolTip(
            "Clave de serper.dev. Se guarda en user_prefs.json y sustituye a la\n"
            "variable SERPER_API_KEY de Windows. Vacío = se usa la del entorno."
        )
        self._api_key_edit.setStyleSheet(
            "QLineEdit { background: rgba(255,255,255,24); border:1px solid"
            " rgba(255,255,255,50); border-radius:8px; padding:4px 8px;"
            " color:" + TKN.tinta + "; font:11px 'Consolas'; }"
        )
        s_layout.addWidget(self._api_key_edit)
        self._web_provider_combo.currentIndexChanged.connect(
            self._sync_api_key_visibility
        )
        self._sync_api_key_visibility()

        self._smart_check = QCheckBox("Detección inteligente: busca aunque no digas \"/web\"")
        self._smart_check.setStyleSheet(
            f"color:{TKN.tinta_media}; font:11px 'Consolas';"
            " QCheckBox::indicator { width:14px; height:14px; }"
        )
        self._smart_check.setChecked(bool(self._settings.web_smart_detect))
        s_layout.addWidget(self._smart_check)

        self._mute_mode_combo = QComboBox()
        self._mute_mode_combo.addItem("Alterna con una tecla (toggle)", "toggle")
        self._mute_mode_combo.addItem(
            "Mantener pulsada (push-to-talk)", "push_to_talk"
        )
        s_layout.addWidget(self._combo_row("MODO DE VOZ", self._mute_mode_combo, False))

        keys_row = QHBoxLayout()
        keys_row.setSpacing(8)
        keys_lab = QLabel("TECLAS")
        keys_lab.setStyleSheet(f"color:{TKN.tinta_suave};font:10px 'Consolas';letter-spacing:1px;")
        keys_row.addWidget(keys_lab)
        keys_col = QVBoxLayout()
        keys_col.setSpacing(4)
        self._ptt_edit = QLineEdit(str(self._settings.ptt_key or "right ctrl"))
        self._ptt_edit.setPlaceholderText("tecla para hablar (right ctrl, f7…)")
        self._ptt_edit.setStyleSheet(
            "QLineEdit { background: rgba(255,255,255,24); border:1px solid"
            " rgba(255,255,255,50); border-radius:8px; padding:4px 8px;"
            " color:" + TKN.tinta + "; font:11px 'Consolas'; }"
        )
        keys_col.addWidget(self._ptt_edit)
        self._hotkey_edit = QLineEdit(str(self._settings.overlay_hotkey or "Ctrl+Shift+Z"))
        self._hotkey_edit.setPlaceholderText("atajo para mostrar el HUD (Ctrl+Shift+Z)")
        self._hotkey_edit.setStyleSheet(
            "QLineEdit { background: rgba(255,255,255,24); border:1px solid"
            " rgba(255,255,255,50); border-radius:8px; padding:4px 8px;"
            " color:" + TKN.tinta + "; font:11px 'Consolas'; }"
        )
        keys_col.addWidget(self._hotkey_edit)
        keys_row.addLayout(keys_col, 1)
        s_layout.addLayout(keys_row)

        # Vocabulario de voz: nombres propios que el reconocedor autocorrige.
        # Sin esto, decir "OpenCode" se transcribía como "OpenCog" y la búsqueda
        # y la respuesta iban sobre el programa equivocado.
        self._vocab_edit = QLineEdit(
            str(getattr(self._settings, "speech_vocabulary", "") or "")
        )
        self._vocab_edit.setPlaceholderText(
            "OpenCode, Tetravex, GraphBoost…"
        )
        self._vocab_edit.setToolTip(
            "Términos que dices en voz y que el reconocedor escribe mal.\n"
            "Se inyectan en la instrucción del sistema como ortografía "
            "literal, así que el modelo los transcribe y los nombra tal cual."
        )
        self._vocab_edit.setStyleSheet(
            "QLineEdit { background: rgba(255,255,255,24); border:1px solid"
            " rgba(255,255,255,50); border-radius:8px; padding:4px 8px;"
            " color:" + TKN.tinta + "; font:11px 'Consolas'; }"
        )
        s_layout.addWidget(self._vocab_edit)

        s_actions = QHBoxLayout()
        s_actions.addStretch(1)
        self._restore_btn = QPushButton("Restaurar")
        self._restore_btn.setStyleSheet(_BTN_QSS)
        self._restore_btn.clicked.connect(self._restore_defaults)
        s_actions.addWidget(self._restore_btn)
        self._save_btn = QPushButton("Guardar")
        self._save_btn.setStyleSheet(_BTN_QSS)
        self._save_btn.clicked.connect(self._save_settings)
        s_actions.addWidget(self._save_btn)
        s_layout.addLayout(s_actions)

        self._settings_open = False
        self._settings_box.hide()
        # El panel de ajustes tiene ~18 filas: en pantallas cortas o con
        # escalado alto (150 %+) su altura no cabe junto al chat. Se mete en un
        # área desplazable para que el contenido largo se pueda recorrer con la
        # rueda en vez de empujar la barra de órdenes fuera de la pantalla.
        self._settings_scroll = QScrollArea()
        self._settings_scroll.setObjectName("settingsScroll")
        self._settings_scroll.setWidget(self._settings_box)
        self._settings_scroll.setWidgetResizable(True)
        self._settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._settings_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._settings_scroll.setStyleSheet(
            "QScrollArea#settingsScroll { background: transparent; border: none; }"
            "QScrollArea#settingsScroll > QWidget > QWidget { background: transparent; }"
        )
        self._settings_scroll.hide()
        layout.addWidget(self._settings_scroll)

        # ---- Órdenes rápidas (chips) --------------------------------------
        self._chips = QWidget()
        self._chips.setObjectName("chips")
        chips = QHBoxLayout(self._chips)
        chips.setContentsMargins(0, 0, 0, 0)
        chips.setSpacing(6)
        for _label, _cmd in (
            ("Ayuda", None),
            ("Clima", "/clima"),
            ("Timers", "/temporizadores"),
            ("Olvidar", "olvida todo"),
        ):
            chip = QPushButton(_label)
            chip.setStyleSheet(_CHIP_QSS)
            chip.clicked.connect(
                lambda _=False, cmd=_cmd, lab=_label: (
                    self._on_help() if cmd is None else self._submit_text(cmd)
                )
            )
            chips.addWidget(chip)
        chips.addStretch(1)
        layout.addWidget(self._chips)

        # ---- Entrada de órdenes ---------------------------------------------
        self.input = QLineEdit()
        self.input.setPlaceholderText(
            "Ordena a MindVoice…  (Enter envía · Esc oculta)"
        )
        self.input.setStyleSheet(
            "QLineEdit { background: rgba(255,255,255,34); border: 1px solid"
            " rgba(255,255,255,60); border-radius: 10px; padding: 8px 12px;"
            " color: " + TKN.tinta_alta + "; font: 14px 'Consolas'; }"
            "QLineEdit:focus { border-color: rgba(255,255,255,130); }"
        )
        self.input.returnPressed.connect(self._on_submit)
        self.input.installEventFilter(self)
        layout.addWidget(self.input)

        outer = QVBoxLayout(self)
        outer.addStretch(1)
        outer.addWidget(self.panel, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addSpacing(28)

        self._blink = QTimer(self)
        self._blink.setInterval(500)
        self._blink.timeout.connect(self._blink_tick)
        # Fundido de entrada/salida del panel. Vive aquí para poder pararlo si
        # se repite la acción a mitad, y para que al cerrar la ventana no quede
        # una animación contando sola.
        self._anim_panel = None
        self._cerrando = False

        self._poll = QTimer(self)
        self._poll.setInterval(60)
        self._poll.timeout.connect(self._drain_ui_queue)
        self._poll.start()

        # Tick del panel de diagnóstico. Se crea parado a propósito: lo
        # arranca y lo para `_toggle_perf`, así que con el panel cerrado no
        # hay ni un tick por segundo de overhead en el HUD.
        self._perf_timer = QTimer(self)
        self._perf_timer.setInterval(_PERF_TICK_MS)
        self._perf_timer.timeout.connect(self._perf_tick)

        # Atajo del panel. Es un QShortcut y no un keyPressEvent porque al
        # abrir el HUD el foco se va al campo de órdenes, que se traga las
        # teclas antes de que la ventana las vea. WindowShortcut lo recibe
        # igual con el foco dentro del HUD.
        self._perf_shortcut = QShortcut(QKeySequence("Ctrl+Shift+D"), self)
        self._perf_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._perf_shortcut.activated.connect(self._toggle_perf)

        # Capa de acento (Fase 5). Hijo de la ventana, no del panel: va por
        # DEBAJO del panel para que el anillo asome por los bordes redondeados
        # sin tapar el texto. Aquí solo se crea el widget vacío; la isla QML que
        # lo pinta no existe todavía. La geometría no se fija aquí: el panel aún
        # no está colocado. Se acomoda al mostrar el HUD y antes de encenderlo.
        self._halo = HaloAcento(self)

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(5000)
        self._watchdog.timeout.connect(self._watchdog_tick)

        self._ptt_watch = QTimer(self)
        self._ptt_watch.setInterval(3000)
        self._ptt_watch.timeout.connect(self._ptt_watchdog_tick)
        self._ptt_watch.start()

        threading.Thread(
            target=self._enumerate_devices, name="dev-enum", daemon=True
        ).start()

    @staticmethod
    def _combo_row(label: str, combo: QComboBox, is_input: bool) -> QWidget:
        """Fila etiqueta + selector de dispositivo."""
        row = QHBoxLayout()
        row.setSpacing(8)
        lab = QLabel(label)
        lab.setStyleSheet(f"color:{TKN.tinta_suave};font:10px 'Consolas';letter-spacing:1px;")
        row.addWidget(lab, 0)
        combo.setStyleSheet(
            "QComboBox { background: rgba(255,255,255,24); color:" + TKN.tinta + ";"
            " border:1px solid rgba(255,255,255,50); border-radius:8px;"
            " padding:4px 8px; font:11px 'Consolas'; }"
            "QComboBox:hover { border-color: rgba(255,255,255,110); }"
            "QComboBox QAbstractItemView { background:" + TKN.fondo + "; color:" + TKN.tinta + ";"
            " border:1px solid rgba(255,255,255,40); selection-background-color:#2a3547; }"
        )
        row.addWidget(combo, 1)
        wrap = QWidget()
        wrap.setLayout(row)
        return wrap

    def _sync_api_key_visibility(self) -> None:
        """Muestra el campo de clave solo si el motor elegido necesita clave.

        Lo decide ``WEB_ENGINES`` (no una lista escrita aquí), igual que la
        comprobación que hace la búsqueda real: así el panel no puede prometer
        una clave donde no hace falta ni esconderla donde sí.
        """
        provider = normalize_web_engine(self._web_provider_combo.currentData())
        self._api_key_edit.setVisible(
            WEB_ENGINES[provider]["key_field"] is not None
        )

    @staticmethod
    def _device_combo(devices: list, is_input: bool) -> QComboBox:
        """Selector con la opción predeterminada + cada dispositivo real."""
        combo = QComboBox()
        combo.addItem("(Predeterminado de Windows)", None)
        for dev in devices:
            name = str(dev.get("name") or "Sin nombre")
            host = str(dev.get("host") or "")
            combo.addItem(f"{name} · {host}", name)
        return combo

    def _enumerate_devices(self) -> None:
        """Enumeración de dispositivos (corre en un hilo propio, no el de Qt).

        ``list_*_devices`` crea y destruye instancias de PyAudio (bloqueante);
        los resultados se entregan a la UI por la cola, sin congelar Qt.
        """
        try:
            ins = list_input_devices()
            self._uiput("devs", json.dumps({"side": "in", "devices": ins}))
        except Exception as exc:  # noqa: BLE001 - lo enumera la próxima vez
            logger.warning("No se pudieron enumerar micrófonos: %s", exc)
        try:
            outs = list_output_devices()
            self._uiput("devs", json.dumps({"side": "out", "devices": outs}))
        except Exception as exc:  # noqa: BLE001 - lo enumera la próxima vez
            logger.warning("No se pudieron enumerar altavoces: %s", exc)

    def _apply_devices(self, payload: str | None) -> None:
        """Re-llena un selector con los dispositivos enumerados (hilo de Qt)."""
        try:
            data = json.loads(payload or "{}")
            side = str(data.get("side") or "")
            devices = list(data.get("devices") or [])
        except Exception:  # noqa: BLE001 - payload mal formado
            return
        if side not in ("in", "out"):
            return
        combo = self._mic_combo if side == "in" else self._out_combo
        selected = (
            self._settings.mic_device_name
            if side == "in"
            else self._settings.output_device_name
        )
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("(Predeterminado de Windows)", None)
        for dev in devices:
            name = str(dev.get("name") or "Sin nombre")
            host = str(dev.get("host") or "")
            combo.addItem(f"{name} · {host}", name)
        index = combo.findData(selected)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def _set_status(self, color: str, caption: str) -> None:
        self._dot.setStyleSheet(f"background:{color};border-radius:6px;")
        self._cap.setText(caption)

    def _relabel_cap(self) -> None:
        """Refresca el texto de las teclas activas (ptt + mostrar + modo)."""
        mode = str(self._settings.mute_mode or "toggle")
        label = {
            "push_to_talk": "habla mientras mantienes",
            "toggle": "alterna hablar con",
        }.get(mode, mode)
        self._cap.setText(
            f"[{self._settings.ptt_key}] {label} · "
            f"[{self._settings.overlay_hotkey}] mostrar"
        )

    def _blink_tick(self) -> None:
        style = self._dot.styleSheet()
        self._dot.setStyleSheet(
            f"background:{TKN.amarillo};border-radius:6px;"
            if "ffb340" in style else "background:#7a5a20;border-radius:6px;"
        )

    def _set_processing(self, on: bool) -> None:
        self._processing = on
        if on:
            self._set_status(f"{TKN.amarillo}", "MindVoice — procesando…")
            self._blink.start()
        else:
            self._blink.stop()
            caption = "MindVoice — listo · voz activa" if self._continuous_voice else "MindVoice — listo"
            self._set_status(f"{TKN.verde}", caption)

    def add_history(self, tag: str, text: str) -> None:
        if (
            tag in ("Tú", "IA", "Web")
            and getattr(self._settings, "save_transcripts", True)
        ):
            self._transcript_executor.submit(self._log_transcript, tag, text or "")
        label = {
            "Tú": f"{TKN.cian}",
            "IA": "#aee9ae",
            "Web": f"{TKN.ambar}",
            "Sys": f"{TKN.tinta_suave}",
        }.get(tag, f"{TKN.tinta_suave}")
        if tag == "Web":
            body = _web_html(text)
        elif tag in ("IA",):
            body = _chat_text(text)
        elif tag == "Tú":
            body = html.escape(text or "").replace("\n", "<br>")
        else:
            body = html.escape(text or "")
        # Separación entre turnos: línea en blanco antes de cada pregunta y cada
        # respuesta (no dentro del mismo mensaje), para que no queden pegados.
        separator = (
            "<br>"
            if tag in ("Tú", "IA", "Web")
            and self.chat.document().blockCount() > 1
            else ""
        )
        self.chat.append(
            separator
            + f"<span style='color:{label}; font-weight:bold;'>[{tag}]</span> {body}"
        )
        # Desplaza la vista HACIA ABAJO: sin esto, las respuestas largas
        # quedaban fuera del área visible del chat (no se veían hasta hacer
        # scroll manual).
        sb = self.chat.verticalScrollBar()
        sb.setValue(sb.maximum())
        limit = int(getattr(self._settings, "overlay_max_lines", 60) or 60)
        doc = self.chat.document()
        if doc.blockCount() > limit:
            cursor = self.chat.textCursor()
            cursor.setPosition(0)
            cursor.movePosition(
                QTextCursor.MoveOperation.Down,
                QTextCursor.MoveMode.KeepAnchor,
            )
            cursor.removeSelectedText()
            cursor.deleteChar()

    # ------------------------------------------------------------------
    # Voz (push-to-talk estilo Google) y cancelación
    # ------------------------------------------------------------------
    def _voice_start(self) -> None:
        """El usuario abre un turno de voz (botón o tecla de alternancia)."""
        self._talking = True
        self._voice_btn.setText("Hablando…")
        self._voice_btn.setStyleSheet(
            "QPushButton { background: rgba(89,217,143,60); color:#eafff3;"
            " border:1px solid rgba(89,217,143,180); border-radius:12px;"
            " padding:5px 14px; font:12px 'Consolas'; }"
        )
        self._set_status(f"{TKN.amarillo}", self._voice_hint())
        self._blink.start()
        # ``toggle=True``: este turno lo ha abierto el usuario, así que puede
        # cerrarse solo cuando se calle (ver ``voice_toggle_silence``). Con
        # False, que es la escucha continua, no se cierra nunca solo.
        self._set_engine_voice(True, toggle=True)

    def _voice_hint(self) -> str:
        """Qué tiene que hacer el usuario para cerrar el turno que tiene abierto.

        Antes esto decia siempre "suelta el botón para responder", pero en modo
        alterno soltar la tecla no hace NADA: el turno solo se cerraba al
        pulsar por segunda vez. El HUD mandaba al usuario a la acción
        equivocada y, si pulsaba una vez y esperaba, no pasaba nada en
        silencio: parecia que el micrófono estaba roto.
        """
        if self._settings.mute_mode == "toggle":
            silencio = float(getattr(self._settings, "voice_toggle_silence", 0.0) or 0.0)
            if silencio > 0:
                return (
                    f"Escuchando…  (habla; se envía sola al parar "
                    f"{silencio:g} s, o pulsa de nuevo para enviarla ya)"
                )
            return "Escuchando…  (pulsa de nuevo para enviar)"
        return "Hablando…  (suelta el botón para responder)"

    def _show_voice_level(self, rms: float | None) -> None:
        """Muestra el nivel del micrófono en dB mientras se transmite."""
        if not rms or rms <= 0:
            self._lvl.setText("")
            return
        db = 20.0 * math.log10(max(rms, 1.0) / 32768.0)
        self._lvl.setText(f"{db:.0f} dB")
        strong = rms >= 40.0
        self._lvl.setStyleSheet(
            ("color:#7fd3a5;" if strong else f"color:{TKN.tinta_tenue};") + "font:11px 'Consolas';"
        )

    def _set_tokens(self, payload: str) -> None:
        """Actualiza el contador de tokens de la sesión (prompt, respuesta)."""
        try:
            prompt, response = (int(x) for x in payload.split(","))
        except Exception:  # noqa: BLE001 - payload mal formado
            return
        self._tokens_prompt = max(self._tokens_prompt, prompt)
        self._tokens_response = max(self._tokens_response, response)
        total = self._tokens_lifetime + self._tokens_prompt + self._tokens_response
        try:
            self._tokens_label.setText(f"tokens {total:,}".replace(",", " "))
            self._tokens_label.setToolTip(
                f"tokens: prompt {self._tokens_prompt:,}".replace(",", " ")
                + f" · respuesta {self._tokens_response:,}".replace(",", " ")
                + f" · acumulado {self._tokens_lifetime:,}".replace(",", " ")
            )
        except Exception:  # noqa: BLE001 - etiqueta ya destruida
            pass

    def _voice_stop(self) -> None:
        """El usuario suelta el botón: deja de transmitir y espera respuesta."""
        self._talking = False
        self._voice_btn.setText("Voz")
        self._voice_btn.setStyleSheet(_BTN_QSS)
        self._set_engine_voice(False)
        self._set_processing(False)

    def _ptt_key_down(self) -> None:
        """Se pulsó la tecla de voz: empieza o alterna la transmisión."""
        if self._ptt_held:
            return
        self._ptt_held = True
        logger.info(
            "PTT: tecla pulsada (%s) · modo=%s · hablando=%s",
            self._settings.ptt_key,
            self._settings.mute_mode,
            self._talking,
        )
        if self._settings.mute_mode == "toggle":
            if self._talking:
                self._voice_stop()
            else:
                self._voice_start()
        elif not self._talking:
            self._voice_start()

    def _ptt_key_up(self) -> None:
        """Se soltó la tecla de voz: culmina el turno en modo PTT."""
        if not self._ptt_held:
            return
        self._ptt_held = False
        logger.info(
            "PTT: tecla soltada (%s) · modo=%s · hablando=%s",
            self._settings.ptt_key,
            self._settings.mute_mode,
            self._talking,
        )
        if self._settings.mute_mode != "toggle" and self._talking:
            self._voice_stop()

    def set_voice_wanted(self, active: bool) -> None:
        """Escucha continua opcional (sin botón): activa el micrófono.

        *Ojo*: por defecto el modo es push-to-talk (botón ``Voz``). Esto solo
        se usa si activas ``overlay_voice_on_start`` en la configuración.
        """
        self._continuous_voice = bool(active)
        if active:
            self.add_history("Sys", "Voz de escucha continua activada")
        self._set_engine_voice(self._continuous_voice)
        self._set_processing(False)

    def _set_engine_voice(self, active: bool, *, toggle: bool = False) -> None:
        with self._lock:
            assistant = self._assistant
        if assistant is not None:
            assistant.set_voice(bool(active), toggle=bool(toggle))
        elif active:
            # El motor todavía no existe (la app arranca en ~4 s). No es un
            # fallo: ``_worker_job`` reitera el estado al levantarlo, pero
            # conviene dejarlo escrito en el log porque desde fuera parece un
            # micrófono que se abre solo.
            logger.info(
                "Voz activada antes de que el motor arrancara; se aplicará "
                "al conectar."
            )

    def _on_cancel(self) -> None:
        self.add_history("Sys", "Cancelado por el usuario")
        self._set_processing(False)
        with self._lock:
            assistant = self._assistant
        if assistant is not None:
            assistant.cancel()

    def _on_clear(self) -> None:
        """Borra el texto del chat en pantalla (no la memoria del modelo)."""
        self.chat.clear()
        self._last_ia = ""
        self._ia_turn = []
        self._acts_wanted = False
        self._acts.hide()
        self.add_history("Sys", "Conversación de pantalla limpiada")

    def _log_transcript(self, tag: str, text: str) -> None:
        """Apéndice a ``data_dir/sessions/AAAA-MM-DD.txt`` (un archivo por día).

        Solo registra turnos reales de la conversación (Tú/IA/Web), con marca
        de hora, y nunca falla si el sistema de archivos está bloqueado.
        """
        if tag not in ("Tú", "IA", "Web"):
            return
        if not getattr(self._settings, "save_transcripts", True):
            return
        try:
            day = datetime.now().strftime("%Y-%m-%d")
            stamp = datetime.now().strftime("%H:%M:%S")
            path = Path(self._settings.data_dir) / "sessions" / f"{day}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {tag}: {text}\n")
        except Exception as exc:  # noqa: BLE001 - el chat no debe romperse
            logger.debug("No se pudo guardar la transcripción: %s", exc)

    def _on_power(self) -> None:
        """Apaga MindVoice por completo: overlay + lanzador en segundo plano."""
        self.add_history("Sys", "Apagando MindVoice…")
        try:
            from launcher import _send_pipe
            _send_pipe(CTRL_PIPE, b"q")
        except Exception:  # noqa: BLE001 - sin lanzador: no pasa nada
            pass
        self.close()

    # ------------------------------------------------------------------
    # Ajustes de audio (micrófono, altavoces y volumen)
    # ------------------------------------------------------------------
    def _apply_settings_visibility(self, open_: bool) -> None:
        """Abre o cierra Ajustes, y con ellos aparta la conversación.

        Al abrir, el chat, los chips de órdenes y los accesos rápidos se
        ocultan: Ajustes es una tarea aparte y así tiene TODO el alto del panel
        (con 18 filas competían con el chat y acababan apretados en ~40 px con
        scroll). La barra de órdenes se queda SIEMPRE visible, que es lo que se
        perdió al volver de Ajustes.
        """
        self._settings_open = open_
        self._settings_scroll.setVisible(open_)
        self.chat.setVisible(not open_)
        self._chips.setVisible(not open_)
        self._acts.setVisible(not open_ and self._acts_wanted)
        if open_:
            # El QScrollArea no crece solo hasta su contenido: hay que darle la
            # altura natural de los ajustes (acotada al 72 % de la pantalla, que
            # es lo que cabe con cabecera, estado y barra de órdenes).
            self._settings_box.adjustSize()
            want = self._settings_box.sizeHint().height()
            avail = max(220, int(self._screen_geometry.height() * 0.72))
            self._settings_scroll.setMaximumHeight(max(220, min(want, avail)))
        else:
            self._settings_scroll.setMaximumHeight(16777215)

    def _toggle_settings(self) -> None:
        """Muestra u oculta el panel de ajustes de audio.

        El estado se lleva en ``_settings_open`` y NO en
        ``_settings_box.isVisible()``: esa propiedad es la visibilidad
        EFECTIVA, que pasa a False en cuanto se oculta la ventana entera
        (Esc, clic fuera, atajo global). Con ella, ocultar el HUD dejaba los
        ajustes "lógicamente abiertos" y al volver el panel crecía de nuevo
        con la barra de órdenes fuera de la pantalla, sin forma de cerrarlos.
        """
        self._apply_settings_visibility(not self._settings_open)
        if self._settings_open:
            self._populate_selection()
        self._relayout_panel()
        self._input_focus()

    def _close_settings(self) -> None:
        """Cierra Ajustes dejando la interfaz recompacta y el foco en la barra."""
        self._apply_settings_visibility(False)
        self._relayout_panel()
        self._input_focus()

    def _relayout_panel(self) -> None:
        """Re-compacta el panel tras ocultar/mostrar Ajustes o los chips.

        Sin esto, ``outer`` conserva la altura que alcanzó con los ajustes
        abiertos y la barra de órdenes (último widget) queda fuera del área
        visible al cerrar Ajustes.

        Hay que invalidar la cadena COMPLETA y en este orden: ``panel`` recalcula
        su tamaño desde su layout, ``updateGeometry`` avisa a ``outer`` de que el
        size hint del panel cambió, y sólo entonces ``outer`` vuelve a colocar.
        Antes se activaba el layout equivocado (``self.layout()`` es ``outer``
        pero sin invalidar) y con ``adjustSize()`` solo, que en Qt no notifica al
        layout propietario: el panel quedaba declarado corto y Burstido alto, y
        el siguiente showing/activateWindow lo volvía a dejar alto.
        """
        panel_layout = self.panel.layout()
        if panel_layout is not None:
            panel_layout.invalidate()
            panel_layout.activate()
        self.panel.updateGeometry()
        self.panel.adjustSize()
        outer = self.layout()
        if outer is not None:
            outer.invalidate()
            outer.setGeometry(self.rect())
        self.chat.viewport().update()
        self._acomodar_halo()

    def _acomodar_halo(self) -> None:
        """Coloca el anillo de acento alrededor del panel y lo deja por debajo.

        El panel cambia de alto al abrir Ajustes o el panel de diagnóstico, y de
        ancho al moverse de monitor: el halo tiene que seguirlo. Va con
        ``stackUnder`` y no con ``raise_`` porque su sitio es DETRÁS del panel:
        es un halo, no una capa que tape el contenido.
        """
        if self._halo is None:
            return
        self._halo.sincronizar_geometria(self.panel.geometry())
        self._halo.stackUnder(self.panel)

    # ------------------------------------------------------------------
    # Panel de diagnóstico (Fase 4)
    # ------------------------------------------------------------------
    def _toggle_perf(self) -> None:
        """Abre o cierra el panel de métricas (Ctrl+Shift+D).

        Abrirlo enciende la instrumentación en caliente, que es lo que el
        módulo ``perf_instr`` prometía y nadie usaba: las métricas solo se
        recogían con ``MINDVOICE_PERF`` puesto antes de arrancar. Cerrarlo
        devuelve el mando a la variable de entorno, así que si el proceso se
        lanzó medido, sigue midiendo (y el panel no miente al decirlo).

        Todo el coste de la función está en abrir. Cerrado, el panel no tiene
        timer corriendo ni hueco en el layout.
        """
        if self._perf_box is None:
            return  # instancia duplicada: este HUD no llegó a construir panel
        self._perf_visible = not self._perf_visible
        self._perf_box.setVisible(self._perf_visible)
        if self._perf_visible:
            _perf.encender()
            if self._perf_timer is not None:
                self._perf_timer.start()
            self._perf_tick()
        else:
            if self._perf_timer is not None:
                self._perf_timer.stop()
            _perf.apagar()
        # El panel crece o mengua, así que el size hint del panel cambia: sin
        # esto el layout se queda con la medida anterior.
        self._relayout_panel()
        logger.info("Panel de diagnóstico %s", "abierto" if self._perf_visible else "cerrado")

    def _perf_tick(self) -> None:
        """Rellena el panel de métricas. Solo lo llama el timer ya abierto."""
        if self._perf_lbl is None or not self._perf_visible:
            return
        lineas = list(_perf.resumen())
        # Salud del proceso: contadores que el HUD llevaba sin enseñar en
        # ningún sitio. Un reinicio del motor en pleno uso no se ve en el
        # chat, y es justo lo que hay que ver cuando algo va raro.
        lineas.append(
            "motor: %d reinicio(s) · %d muerte(s) rápida(s) · PTT %d fallo(s) · %s"
            % (
                self._restarts,
                self._quick_deaths,
                self._ptt_failures,
                "silenciado" if self._muted else "con voz",
            )
        )
        texto = "\n".join(lineas)
        # Solo se escribe si el texto cambió: un `setText` con lo mismo
        # igual fuerza un repintado del panel entero cada segundo.
        if texto != self._perf_texto:
            self._perf_texto = texto
            self._perf_lbl.setText(texto)

    def _sync_privacy_button(self) -> None:
        if self._privacy_off:
            self._privacy_btn.setStyleSheet(_ICO_RED_QSS)
            self._privacy_btn.setToolTip(
                "Privacidad activa: MindVoice ya no ve la pantalla"
            )
        else:
            self._privacy_btn.setStyleSheet(_ICO_QSS)
            self._privacy_btn.setToolTip("Privacidad: no mostrar la pantalla al modelo")

    def _toggle_privacy(self) -> None:
        """Pausa/reanuda el envío del contexto visual (sesión).

        Cambia ``screen_enabled`` en caliente: el motor lee ese valor por cada
        turno, así que la próxima orden ya no envía captura de pantalla. No se
        persiste hasta pulsar Guardar en Ajustes.
        """
        self._privacy_off = not self._privacy_off
        enabled = not self._privacy_off
        self._settings.screen_enabled = enabled
        self._apply_runtime_screen(enabled)
        if self._privacy_off:
            self.add_history("Sys", "Privacidad: dejo de ver tu pantalla (hasta pulsar Guardar).")
        else:
            self.add_history("Sys", "Privacidad: vuelvo a ver tu pantalla.")
        try:
            self._screen_check.setChecked(not self._privacy_off)
        except Exception:  # noqa: BLE001 - cheque aún no construido
            pass

    def _on_olvidar(self) -> None:
        """Borra la memoria breve del modelo (envía 'olvida todo')."""
        self.add_history("Sys", "Se olvidará el contexto anterior del modelo.")
        self._submit_text("olvida todo")

    def _on_help(self) -> None:
        """Muestra en el chat un recordatorio de los atajos y comandos."""
        self.add_history(
            "Sys",
            "AYUDA: "
            f"[{self._settings.ptt_key}] hablar (o botón Voz) · "
            f"[{self._settings.overlay_hotkey}] mostrar/ocultar · "
            "Comandos: /web consulta · /clima · /temporizadores · "
            "/alarma HH:MM · olvida todo · Privacidad (ojo) pausa la "
            "pantalla · Guardar aplica los Ajustes.",
        )

    def _populate_selection(self) -> None:
        """Sincroniza los selectores con lo seleccionado/guardado."""
        self._select_combo_value(
            self._mic_combo, self._settings.mic_device_name
        )
        self._select_combo_value(
            self._out_combo, self._settings.output_device_name
        )
        self._cont_check.setChecked(bool(self._settings.overlay_voice_on_start))
        self._web_check.setChecked(bool(self._settings.web_search_enabled))
        self._screen_check.setChecked(bool(self._settings.screen_enabled))
        self._transcript_check.setChecked(bool(getattr(self._settings, "save_transcripts", True)))
        self._manual_vad_check.setChecked(bool(getattr(self._settings, "voice_manual_vad", True)))
        mods = "audio_text" if "TEXT" in (self._settings.response_modalities or []) else "audio"
        self._select_combo_value(self._modes_combo, mods)
        self._select_combo_value(self._voice_combo, self._settings.voice)
        self._select_combo_value(self._lang_combo, self._settings.transcript_lang)
        self._vol_slider.setValue(
            int(max(0.0, min(1.5, float(self._settings.output_volume or 1.0))) * 100)
        )
        self._opacity_slider.setValue(
            int(max(0.3, min(0.95, float(self._settings.overlay_opacity or 0.7))) * 100)
        )
        self._select_combo_value(
            self._lines_combo,
            int(getattr(self._settings, "overlay_max_lines", 60) or 60),
        )
        self._select_combo_value(
            self._web_provider_combo, self._settings.web_search_provider
        )
        self._api_key_edit.setText(str(self._settings.serper_api_key or ""))
        self._sync_api_key_visibility()
        self._smart_check.setChecked(bool(self._settings.web_smart_detect))
        self._select_combo_value(self._mute_mode_combo, self._settings.mute_mode)
        self._ptt_edit.setText(str(self._settings.ptt_key or "right ctrl"))
        self._hotkey_edit.setText(str(self._settings.overlay_hotkey or "Ctrl+Shift+Z"))
        self._vocab_edit.setText(
            str(getattr(self._settings, "speech_vocabulary", "") or "")
        )

    @staticmethod
    def _select_combo_value(combo: QComboBox, value) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _save_settings(self) -> None:
        """Aplica y guarda las preferencias elegidas."""
        mic = self._mic_combo.currentData()
        out = self._out_combo.currentData()
        volume = self._vol_slider.value() / 100.0
        continuous = self._cont_check.isChecked()
        web = self._web_check.isChecked()
        screen = self._screen_check.isChecked()
        voice = str(self._voice_combo.currentData() or "").strip() or "Puck"
        lang = str(self._lang_combo.currentData() or "").strip() or "es-ES"
        opacity = self._opacity_slider.value() / 100.0
        max_lines = int(self._lines_combo.currentData() or 60)
        provider = normalize_web_engine(self._web_provider_combo.currentData())
        smart = self._smart_check.isChecked()
        mute_mode = str(self._mute_mode_combo.currentData() or "toggle").strip()
        ptt_key = str(self._ptt_edit.text() or "").strip() or "right ctrl"
        hotkey = str(self._hotkey_edit.text() or "").strip() or "Ctrl+Shift+Z"
        applied_voice = str(self._settings.voice or "") or "Puck"
        applied_lang = str(self._settings.transcript_lang or "") or "es-ES"
        applied_ptt = str(self._settings.ptt_key or "right ctrl")
        applied_hotkey = str(self._settings.overlay_hotkey or "Ctrl+Shift+Z")
        applied_screen = bool(self._settings.screen_enabled)
        applied_modes = (
            "audio_text"
            if "TEXT" in (self._settings.response_modalities or [])
            else "audio"
        )
        prefs = load_prefs()
        prefs["mic_device_name"] = mic or None
        prefs["output_device_name"] = out or None
        prefs["output_volume"] = volume
        prefs["voice_on_start"] = continuous
        prefs["voice"] = voice
        prefs["transcript_lang"] = lang
        prefs["web_search_enabled"] = web
        prefs["screen_enabled"] = screen
        prefs["overlay_opacity"] = round(opacity, 2)
        prefs["overlay_max_lines"] = max_lines
        prefs["web_search_provider"] = provider
        # Cadena vacía = se vuelve a la clave del entorno (SERPER_API_KEY).
        prefs["serper_api_key"] = self._api_key_edit.text().strip()
        prefs["web_smart_detect"] = smart
        prefs["mute_mode"] = mute_mode
        prefs["ptt_key"] = ptt_key
        prefs["overlay_hotkey"] = hotkey
        prefs["speech_vocabulary"] = self._vocab_edit.text().strip()
        prefs["save_transcripts"] = self._transcript_check.isChecked()
        prefs["voice_manual_vad"] = self._manual_vad_check.isChecked()
        prefs["voice_toggle_silence"] = self._toggle_silence_spin.value()
        prefs["memory_enabled"] = self._memory_check.isChecked()
        prefs["memory_max_nodes"] = self._memory_nodes_spin.value()
        prefs["memory_retention_days"] = self._memory_days_spin.value()
        prefs["response_modalities"] = str(
            self._modes_combo.currentData() or "audio"
        ).strip()
        prefs["tokens_lifetime"] = (
            self._tokens_lifetime + self._tokens_prompt + self._tokens_response
        )
        save_prefs(prefs)
        apply_prefs(self._settings, prefs)
        self._privacy_off = not bool(screen)
        self._sync_privacy_button()
        self.add_history("Sys", "Ajustes guardados")
        self._apply_runtime_audio(out or None, volume)
        self._apply_opacity(opacity)
        self._relabel_cap()
        self._refrescar_memoria_en_vivo()
        changed = voice != applied_voice or lang != applied_lang
        if changed:
            self._apply_runtime_voice(voice, lang)
            self._uiput(
                "sys",
                f"Voz/idioma ({voice}, {lang}) se aplicarán al reconectar la sesión",
            )
        new_modes = str(self._modes_combo.currentData() or "audio")
        if new_modes != applied_modes:
            self._apply_runtime_modes(new_modes)
            self._uiput(
                "sys",
                "Modo de salida cambiado; se aplicará al reconectar la sesión.",
            )
        if screen != applied_screen:
            self._apply_runtime_screen(screen)
        if ptt_key != applied_ptt or (
            self._ptt_thread is not None and not self._ptt_thread.is_alive()
        ):
            self._stop_ptt()
            self._setup_ptt()
        if hotkey != applied_hotkey:
            if _mutex_exists(LAUNCHER_MUTEX):
                self.add_history(
                    "Sys",
                    "Atajo guardado; se aplicará al reiniciar el lanzador.",
                )
            else:
                self._stop_hotkey()
                self._setup_hotkey()
        # Los toggles de web/pantalla ya quedaron en la instancia de Settings que
        # comparte el motor: entran en vigor con la próxima orden.
        self._close_settings()

    def _restore_defaults(self) -> None:
        """Restaura la configuración predeterminada (sin tocar nada más)."""
        self._mic_combo.setCurrentIndex(0)
        self._out_combo.setCurrentIndex(0)
        self._vol_slider.setValue(100)
        self._cont_check.blockSignals(True)
        self._cont_check.setChecked(False)
        self._cont_check.blockSignals(False)
        self._web_check.setChecked(True)
        self._screen_check.setChecked(True)
        self._transcript_check.setChecked(True)
        self._select_combo_value(self._modes_combo, "audio")
        self._voice_combo.setCurrentIndex(0)
        self._select_combo_value(self._lang_combo, "es-ES")
        self._opacity_slider.setValue(70)
        self._select_combo_value(self._lines_combo, 60)
        self._select_combo_value(self._web_provider_combo, default_web_engine())
        self._api_key_edit.clear()
        self._sync_api_key_visibility()
        self._smart_check.setChecked(True)
        self._select_combo_value(self._mute_mode_combo, "toggle")
        self._ptt_edit.setText("right ctrl")
        self._hotkey_edit.setText("Ctrl+Shift+Z")
        self.add_history("Sys", "Ajustes restaurados a la vista (pulsa Guardar)")

    def _disengage_voice(self) -> None:
        """Desactiva la escucha continua al marcar el checkbox en off."""
        self._continuous_voice = False
        self._set_engine_voice(False)
        self._set_processing(False)

    def _apply_runtime_audio(self, out_name, volume: float) -> None:
        """Propaga la salida/volumen al asistente que corre en segundo plano."""
        with self._lock:
            assistant = self._assistant
        if assistant is None:
            return
        assistant.set_output_device(out_name)
        assistant.set_volume(volume)

    def _apply_runtime_screen(self, enabled: bool) -> None:
        with self._lock:
            assistant = self._assistant
        if assistant is not None:
            assistant.set_screen_enabled(bool(enabled))

    def _apply_runtime_voice(self, voice: str, lang: str) -> None:
        """Propaga la voz/idioma al motor (se aplican al reconectar la sesión)."""
        with self._lock:
            assistant = self._assistant
        if assistant is None:
            return
        assistant.set_voice_name(voice)
        assistant.set_transcript_lang(lang)

    # -- Memoria ---------------------------------------------------------
    def _estadisticas_memoria(self) -> dict | None:
        """Números reales del grafo, o ``None`` si no hay índice en marcha."""
        with self._lock:
            assistant = self._assistant
        if assistant is None:
            return None
        try:
            indice = getattr(assistant, "_memoria_indice", None)
            if indice is None or not hasattr(indice, "estadisticas"):
                return None
            return indice.estadisticas()
        except Exception as exc:  # noqa: BLE001 - es un dato cosmético
            logger.debug("No se pudieron leer las estadísticas de memoria: %s", exc)
            return None

    def _refrescar_memoria_en_vivo(self) -> None:
        """Pinta lo que hay en memoria y aplica el límite si se ha cambiado.

        Apagar la memoria surte efecto sin reiniciar: se suelta el índice y se
        reconstruye al guardar. Antes estos ajustes no existían, así que lo
        único que se podía hacer con la memoria era dejar de escribirla.
        """
        activo = self._memory_check.isChecked()
        self._memory_nodes_spin.setEnabled(activo)
        self._memory_days_spin.setEnabled(activo)
        self._memory_purge_btn.setEnabled(activo)

        with self._lock:
            assistant = self._assistant
        if assistant is not None:
            try:
                assistant.set_memory_enabled(activo)
            except Exception as exc:  # noqa: BLE001
                logger.debug("El motor no admite cambiar la memoria en caliente: %s", exc)

        stats = self._estadisticas_memoria()
        if stats is None:
            self._memory_stats.setText(
                "memoria: sin índice (se guarda en plano)" if activo else "memoria: apagada"
            )
            return
        if not activo:
            self._memory_stats.setText("memoria: apagada")
            return
        extra = " · degradado" if stats.get("degradado") else ""
        self._memory_stats.setText(
            f"memoria: {stats['nodos']} nodos, {stats['aristas']} aristas, "
            f"{stats.get('disputados', 0)} en disputa{extra}"
        )

    def _on_purge_memory(self) -> None:
        """Vacía la memoria conservando el hilo de los temas."""
        with self._lock:
            assistant = self._assistant
        indice = getattr(assistant, "_memoria_indice", None) if assistant else None
        if indice is None:
            self.add_history("Sys", "No hay índice de memoria que vaciar.")
            return
        if not indice.reset():
            self.add_history("Sys", "No se pudo vaciar la memoria.")
            return
        self.add_history("Sys", "Memoria vaciada (se conservan los temas).")
        self._refrescar_memoria_en_vivo()

    def _apply_runtime_modes(self, mode: str) -> None:
        """Propaga el modo de salida (solo voz / voz+texto) al motor."""
        with self._lock:
            assistant = self._assistant
        if assistant is None:
            return
        assistant.set_response_modalities(
            ["AUDIO", "TEXT"] if mode == "audio_text" else ["AUDIO"]
        )

    # ------------------------------------------------------------------
    # Instancia única y control externo (pipe con nombre)
    # ------------------------------------------------------------------
    def _acquire_single_instance(self) -> bool:
        handle = _kernel32.CreateMutexW(None, False, OVERLAY_MUTEX)
        if not handle:
            return True
        if _kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            _kernel32.CloseHandle(handle)
            return False
        self._single_mutex = handle
        return True

    def _start_toggle_pipe(self) -> None:
        if self._toggle_thread is not None:
            return
        self._toggle_thread = threading.Thread(
            target=self._toggle_pipe_worker,
            name="mindvoice-toggle-pipe",
            daemon=True,
        )
        self._toggle_thread.start()

    def _toggle_pipe_worker(self) -> None:
        """Sirve el pipe: la placa de fondo (launcher) pide toggle aquí."""
        while True:
            pipe = _kernel32.CreateNamedPipeW(
                TOGGLE_PIPE,
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
            buf = ctypes.create_string_buffer(1)
            nread = ctypes.c_ulong(0)
            _kernel32.ReadFile(pipe, buf, 1, ctypes.byref(nread), None)
            reply = ctypes.create_string_buffer(b"ok")
            nwritten = ctypes.c_ulong(0)
            _kernel32.WriteFile(pipe, reply, 2, ctypes.byref(nwritten), None)
            _kernel32.CloseHandle(pipe)
            if nread.value == 1 and buf.raw[:1] == b"t":
                try:
                    self._ui_queue.put(("toggle", None))
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------
    # Atajo global Win32: RegisterHotKey + hilo con cola de mensajes
    # ------------------------------------------------------------------
    def _setup_hotkey(self) -> None:
        if _mutex_exists(LAUNCHER_MUTEX):
            logger.info(
                "El lanzador en segundo plano ya posee el atajo %s;"
                " se omite el registro local.",
                self._settings.overlay_hotkey,
            )
            return
        if self._hotkey_thread is not None and self._hotkey_thread.is_alive():
            return
        self._hotkey_thread = None
        modifiers, vk = _parse_hotkey(self._settings.overlay_hotkey)
        if vk == 0:
            return
        self._hotkey_stop = False
        self._hotkey_thread = threading.Thread(
            target=self._hotkey_worker,
            name="mindvoice-hotkey",
            daemon=True,
            args=(modifiers, vk),
        )
        self._hotkey_thread.start()

    def _hotkey_worker(self, modifiers: int, vk: int) -> None:
        """Registra el atajo y bombea su cola de mensajes hasta el cierre."""
        # Crea la cola de mensajes del hilo antes de registrar el atajo.
        msg = ctypes.wintypes.MSG()
        _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
        self._hotkey_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        thread_id = self._hotkey_thread_id

        registered = _user32.RegisterHotKey(
            None, HOTKEY_ID, ctypes.c_uint(modifiers), ctypes.c_uint(vk)
        )
        if not registered:
            logger.warning(
                "No se pudo registrar el atajo %s (¿otra app ya lo usa?).",
                self._settings.overlay_hotkey,
            )
            self._hotkey_thread = None
            self._hotkey_thread_id = 0
            return
        logger.info("Atajo global registrado: %s", self._settings.overlay_hotkey)

        try:
            while not self._hotkey_stop:
                result = _user32.GetMessageW(
                    ctypes.byref(msg), None, 0, 0
                )
                if result <= 0:
                    break
                if (
                    msg.message == WM_HOTKEY
                    and (ctypes.c_ulong(msg.wParam).value & 0xFFFF) == HOTKEY_ID
                ):
                    self._hotkey_hit()
        finally:
            _user32.UnregisterHotKey(None, HOTKEY_ID)
            if self._hotkey_thread_id == thread_id:
                self._hotkey_thread_id = 0
            if self._hotkey_thread is threading.current_thread():
                self._hotkey_thread = None

    def _hotkey_hit(self) -> None:
        """Corre en el hilo del atajo; solo toca la cola de UI (thread-safe)."""
        try:
            self._ui_queue.put(("toggle", None))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Push-to-talk global (hook low-level): mantén la tecla para hablar,
    # suéltala para culminar el turno y enviar la orden a la IA
    # ------------------------------------------------------------------
    def _setup_ptt(self) -> None:
        if self._ptt_thread is not None and self._ptt_thread.is_alive():
            return
        self._ptt_thread = None
        vk = _parse_ptt_key(self._settings.ptt_key)
        if vk == 0:
            logger.warning("Tecla de voz (ptt_key) inválida: %r", self._settings.ptt_key)
            return
        self._ptt_vk = vk
        self._ptt_held = False
        self._ptt_stop = False
        self._ptt_failures = 0
        if not self._ptt_watch.isActive():
            self._ptt_watch.start()
        self._ptt_thread = threading.Thread(
            target=self._ptt_worker,
            name="mindvoice-ptt",
            daemon=True,
            args=(vk,),
        )
        self._ptt_thread.start()

    def _ptt_worker(self, vk: int) -> None:
        """Instala el hook WH_KEYBOARD_LL y bombea su cola de mensajes."""
        msg = ctypes.wintypes.MSG()
        _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
        self._ptt_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        thread_id = self._ptt_thread_id

        @HOOKPROC
        def _callback(n_code: int, w_param, l_param) -> int:
            if n_code == HC_ACTION:
                kbd = ctypes.cast(
                    ctypes.c_void_p(l_param), ctypes.POINTER(KBDLLHOOKSTRUCT)
                ).contents
                if int(kbd.vkCode) == vk:
                    event = None
                    if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        event = "ptt_on"
                    elif w_param in (WM_KEYUP, WM_SYSKEYUP):
                        event = "ptt_off"
                    if event is not None and not self._ptt_stop:
                        logger.debug(
                            "PTT hook: %s (vk=%d, flags=0x%X)",
                            "down" if event == "ptt_on" else "up",
                            kbd.vkCode,
                            kbd.flags,
                        )
                        try:
                            self._ui_queue.put((event, None))
                        except Exception:  # noqa: BLE001
                            pass
            return _user32.CallNextHookEx(
                ctypes.c_void_p(self._ptt_hook), n_code, w_param, l_param
            )

        hmod = _kernel32.GetModuleHandleW(None)
        self._ptt_hook = _user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, _callback, hmod, 0
        )
        if not self._ptt_hook:
            logger.warning(
                "No se pudo instalar el hook de voz (%s).", self._settings.ptt_key
            )
            self._ptt_thread = None
            self._ptt_thread_id = 0
            return
        self._ptt_proc = _callback  # conserva la referencia (GC)
        logger.info("Push-to-talk global activo: %s", self._settings.ptt_key)
        try:
            while not self._ptt_stop:
                result = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result <= 0:
                    break
        except Exception:  # noqa: BLE001 - el hook nunca debe morir en silencio
            logger.exception(
                "El hilo de la tecla de voz (%s) lanzó una excepción.",
                self._settings.ptt_key,
            )
        finally:
            _user32.UnhookWindowsHookEx(ctypes.c_void_p(self._ptt_hook))
            self._ptt_hook = None
            self._ptt_proc = None
            if self._ptt_thread_id == thread_id:
                self._ptt_thread_id = 0
            if self._ptt_thread is threading.current_thread():
                self._ptt_thread = None
            # Imprescindible: si el hook muere con la tecla "pegada",
            # `_ptt_held` se quedaba en True y `_ptt_key_down` cortaba siempre,
            # dejando el PTT muerto hasta reiniciar MindVoice.
            self._ptt_held = False
            if not self._ptt_stop:
                logger.warning(
                    "El hook de la tecla de voz (%s) se detuvo inesperadamente.",
                    self._settings.ptt_key,
                )

    _PTT_WATCH_LIMIT = 5

    def _ptt_watchdog_tick(self) -> None:
        """Reinstala el hook si el hilo de la tecla de voz ha muerto.

        Antes, un hook caído dejaba el PTT inutilizable y en silencio: sin
        aviso en pantalla ni en el log, parecía que la tecla no funcionaba.
        """
        if self._ptt_stop:
            return
        thread = self._ptt_thread
        if thread is not None and thread.is_alive():
            self._ptt_failures = 0
            return
        if self._ptt_failures >= self._PTT_WATCH_LIMIT:
            if self._ptt_watch.isActive():
                self._ptt_watch.stop()
            logger.error(
                "No se pudo reinstalar el hook de la tecla de voz (%s) tras %d "
                "intentos: el PTT queda desactivado.",
                self._settings.ptt_key,
                self._ptt_failures,
            )
            self.add_history(
                "Sys",
                f"La tecla de voz ({self._settings.ptt_key}) dejó de funcionar y no "
                "se pudo reinstalar. Cambia la tecla en Ajustes o reabre MindVoice.",
            )
            return
        self._ptt_failures += 1
        logger.warning(
            "Watchdog PTT: el hilo de la tecla de voz no está vivo (intento %d/%d); "
            "reinstalando el hook.",
            self._ptt_failures,
            self._PTT_WATCH_LIMIT,
        )
        self._ptt_held = False
        self._setup_ptt()

    def _stop_ptt(self) -> None:
        self._ptt_stop = True
        if self._ptt_watch.isActive():
            self._ptt_watch.stop()
        thread = self._ptt_thread
        if thread is not None and self._ptt_thread_id:
            _user32.PostThreadMessageW(
                ctypes.c_uint(self._ptt_thread_id), WM_QUIT, 0, 0
            )
        if thread is not None:
            thread.join(timeout=0.1)
            if thread.is_alive() and self._ptt_thread_id:
                _user32.PostThreadMessageW(
                    ctypes.c_uint(self._ptt_thread_id), WM_QUIT, 0, 0
                )
                thread.join(timeout=2.0)
            if not thread.is_alive():
                self._ptt_thread = None
        self._ptt_thread_id = 0
        self._ptt_held = False

    # ------------------------------------------------------------------
    # Visibilidad y eventos
    # ------------------------------------------------------------------
    def show_overlay(self) -> None:
        self._settings_scroll.setVisible(self._settings_open)
        self._cerrando = False
        self.show()
        self.raise_()
        self.activateWindow()
        self._relayout_panel()
        self._input_focus()
        # Si el panel de diagnóstico estaba abierto, su reloj vuelve a correr
        # con la ventana en pantalla. Con la ventana oculta no tiene sentido
        # pedir un snapshot: el panel ya no se ve.
        if self._perf_visible and self._perf_timer is not None:
            self._perf_timer.start()
        self._perf_tick()
        # Entra con un fundido corto en vez de aparecer de golpe.
        self._fundir_panel(entrando=True)
        # Visible de verdad: el panel ya está en pantalla. Esta es la marca que
        # cierra la ventana de arranque.
        _perf.mark_ui_visible()

    def _prefetch_screen_context(self) -> None:
        """Avisa al motor de que va a hacer falta ver la pantalla.

        Se dispara al ENFOCAR la barra de órdenes, no al enviar: mientras el
        usuario escribe, el motor prepara en segundo plano la descripción de lo
        que hay en pantalla, que es una mini-sesión de visión aparte y cuesta
        unos segundos. Antes esa espera se pagaba dentro del turno (con un tope
        de 4 s que la cancelaba casi la mitad de las veces), así que la IA
        respondía sin contexto visual y se inventaba lo que tenías delante.
        """
        with self._lock:
            assistant, loop = self._assistant, self._loop
        if assistant is None or loop is None:
            return

        def _ask() -> None:
            try:
                assistant.prefetch_description()
            except Exception:  # noqa: BLE001 - es una optimización, no crítica
                pass

        try:
            loop.call_soon_threadsafe(_ask)
        except RuntimeError:  # el bucle ya está cerrado: se calienta al enviar
            pass

    def _input_focus(self) -> None:
        self.input.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self.input.selectAll()

    def hide_overlay(self) -> None:
        """Desaparece con un fundido corto y, al terminar, oculta la ventana.

        Se cubre con el flag ``_cerrando`` porque durante el fundido el panel
        sigue visible: sin él, dos ``Ctrl+Shift+Z`` seguidos en el mismo
        instante se pisarían y la ventana se quedaría a medio ocultar.
        """
        if self._cerrando:
            return
        self._cerrando = True
        # El panel de diagnóstico deja de pedir snapshots mientras la ventana
        # no se ve. El flag `_perf_visible` NO se toca: al volver a salir, el
        # panel sigue abierto como estaba.
        if self._perf_timer is not None:
            self._perf_timer.stop()
        if not self.isVisible():
            # Nunca se mostró: no hay nada que fundir.
            self._cerrando = False
            self.hide()
            return
        self._fundir_panel(entrando=False)

    def _fundir_panel(self, *, entrando: bool) -> None:
        """Aparece o desaparece el panel con un fundido de opacidad."""
        panel = getattr(self, "panel", None)
        if panel is None:
            if not entrando:
                self.hide()
            return
        anim = self._anim_panel
        if anim is not None:
            anim.stop()
            # `disconnect()` sin ranura quita todas las de esta señal. Con
            # ranura concreta revienta si esa ranura no estaba conectada (el
            # `Fade` de entrada no la usa), que es lo que pasaba.
            try:
                anim.finished.disconnect()
            except TypeError:
                pass
            self._anim_panel = None
        if entrando:
            panel.show()
            nuevo = Fade(panel, desde=0.0, hasta=1.0, parent=self)
            nuevo.start()
        else:
            nuevo = Desvanecer(panel, parent=self)
            nuevo.finished.connect(self._panel_oculto)
            nuevo.start()
        self._anim_panel = nuevo

    def _panel_oculto(self) -> None:
        """Fin del desvanecido: la ventana entera se va."""
        try:
            self.hide()
        except RuntimeError:  # la ventana ya se había destruido
            pass

    def toggle(self) -> None:
        if self._cerrando:
            # A medio desvanecer: la pulsación lo deja como estaba.
            self.show_overlay()
        elif self.isVisible():
            self.hide_overlay()
        else:
            self.show_overlay()
        logger.info("Overlay %s", "visible" if self.isVisible() else "oculto")

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.hide_overlay()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self.panel.geometry().contains(event.position().toPoint()):
            self.hide_overlay()
            return
        super().mousePressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._watchdog_stop = True
        self._watchdog.stop()
        # El panel de diagnóstico se para aquí: si no, su tick de un segundo
        # sigue pidiendo un snapshot contra widgets que se están borrando.
        if self._perf_timer is not None:
            self._perf_timer.stop()
        # El fundido en curso se para aquí: si no, su ``finished`` dispara
        # ``_panel_oculto`` contra una ventana que ya se está cerrando.
        if self._anim_panel is not None:
            self._anim_panel.stop()
            self._anim_panel = None
        # La isla QML del acento se suelta aquí: si no, el motor QML seguiría con
        # su respiración viva contra una ventana que se está destruyendo.
        if self._halo is not None:
            self._halo.apagar()
        self._stop_hotkey()
        self._stop_ptt()
        self._stop_worker()
        if self._thread is not None and self._thread.is_alive():
            # Deja que el worker libere PyAudio/mss antes de salir (cierre
            # limpio del pipeline de audio/red, evita handles colgados).
            self._thread.join(timeout=2.0)
        self._thread = None
        self._transcript_executor.shutdown(wait=True, cancel_futures=True)
        super().closeEvent(event)
        QApplication.instance().quit()

    def _stop_hotkey(self) -> None:
        self._hotkey_stop = True
        thread = self._hotkey_thread
        if thread is not None and self._hotkey_thread_id:
            _user32.PostThreadMessageW(
                ctypes.c_uint(self._hotkey_thread_id), WM_QUIT, 0, 0
            )
        if thread is not None:
            thread.join(timeout=0.1)
            if thread.is_alive() and self._hotkey_thread_id:
                _user32.PostThreadMessageW(
                    ctypes.c_uint(self._hotkey_thread_id), WM_QUIT, 0, 0
                )
                thread.join(timeout=2.0)
            if not thread.is_alive():
                self._hotkey_thread = None
        self._hotkey_thread_id = 0

    # ------------------------------------------------------------------
    # Motor (asyncio en hilo aparte, colas hacia Qt)
    # ------------------------------------------------------------------
    def _start_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self._quit_requested or self._watchdog_stop:
            return
        self._worker_started = True
        self._last_worker_start = time.monotonic()
        if not self._watchdog.isActive():
            self._watchdog.start()
        self._thread = threading.Thread(
            target=self._worker_job, name="mindvoice-overlay", daemon=True
        )
        self._thread.start()
        # Reinicio en marcha: se cuentan para poder frenar el bucle de fallos.
        if self._worker_started_once:
            self._restarts += 1
        else:
            self._worker_started_once = True

    # Intervalo mínimo entre reinicios automáticos del motor (evita bucles
    # de reinicio si la falla es inmediata y repetible).
    _WATCHDOG_MIN_GAP = 8.0
    # Si el motor muere una y otra vez en menos de ``_WATCHDOG_QUICK_DEAD_S``, no
    # es un fallo puntual: reiniciar cada 8 s solo consume cuota (cada arranque
    # abre sesión Live y reintenta el modelo web) y el usuario ve un Latigazo de
    # "reconectando" perpetuo. A partir de ``_WATCHDOG_QUICK_LIMIT`` muertes
    # seguidas se para del todo y se avisa.
    _WATCHDOG_QUICK_DEAD_S = 15.0
    _WATCHDOG_QUICK_LIMIT = 4

    def _watchdog_tick(self) -> None:
        """Reinicia el motor si el hilo del asistente murió en segundo plano."""
        if not self._worker_started or self._watchdog_stop:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        if self._quit_requested:
            # Parada voluntaria: el watchdog NO debe resucitar el motor.
            return
        now = time.monotonic()
        if now - self._last_worker_start < self._WATCHDOG_MIN_GAP:
            return
        # ¿Murió de forma inmediata? (recién arrancado y ya caído)
        if now - self._last_worker_start < self._WATCHDOG_QUICK_DEAD_S:
            self._quick_deaths += 1
            if self._quick_deaths >= self._WATCHDOG_QUICK_LIMIT:
                self._watchdog_stop = True
                self._watchdog.stop()
                logger.error(
                    "El motor se cae repetidamente (%d intentos): no se reinicia "
                    "más. Mira el log (overlay-launcher.log) para la causa.",
                    self._quick_deaths,
                )
                self.add_history(
                    "Sys",
                    "El motor se cae una y otra vez: se detiene el reinicio "
                    "automático para no gastar cuota. Revisa "
                    "overlay-launcher.log y vuelve a abrir MindVoice.",
                )
                self._set_status("#ff5d5d", "MindVoice — detenido")
                return
        else:
            self._quick_deaths = 0
        logger.info("Watchdog: el motor se detuvo; se reinicia solo.")
        self.add_history("Sys", "El motor se detuvo; reiniciándolo…")
        self._start_worker()

    def _stop_worker(self) -> None:
        with self._lock:
            assistant, loop = self._assistant, self._loop
        if assistant is not None and loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(assistant.quit_event.set)
            except RuntimeError:
                pass

    def _request_quit(self) -> None:
        """Atajo/acción de "salir": para el motor Y cierra la app.

        Antes solo paraba el motor y el watchdog lo volvía a levantar 8 s
        después, así que el atajo de salir no salía de nada y se veía un ciclo
        de reinicios. Ahora marca la parada como voluntaria (el watchdog no
        reinicia) y pide el cierre desde el hilo de Qt.
        """
        self._quit_requested = True
        self._watchdog_stop = True
        self._watchdog.stop()
        with self._lock:
            assistant, loop = self._assistant, self._loop
        if assistant is not None and loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(assistant.quit_event.set)
            except RuntimeError:
                pass
        logger.info("Atajo de salir pulsado: se cierra MindVoice.")
        self._uiput("quit", None)

    def _worker_job(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        mode = str(self._settings.mute_mode or "toggle")
        mode = "toggle" if mode not in ("toggle", "push_to_talk") else mode
        hotkeys = HotkeyController(mode=mode, initially_muted=False)
        hotkeys.on_mute_changed = lambda muted: self._uiput("mute", "1" if muted else "0")
        assistant = LiveAssistant(
            settings=self._settings,
            hotkeys=hotkeys,
            on_text=lambda text: self._uiput("ia", text),
            on_user_text=lambda text: self._uiput("me", text),
            on_meta=lambda text: self._uiput("sys", text),
            on_web=lambda text: self._uiput("web", text),
            on_turn_complete=lambda: self._uiput("done", None),
            on_interrupted=lambda: self._uiput("sys", "Interrumpiste la respuesta"),
            on_voice_level=lambda rms: self._uiput("lvl", rms),
            on_tokens=lambda prompt, response: self._uiput(
                "toks", f"{prompt},{response}"
            ),
            on_state=lambda text: self._uiput("state", str(text)),
            on_timers=lambda summary: self._uiput("timers", str(summary)),
        )
        with self._lock:
            self._loop = loop
            self._assistant = assistant
        hotkeys.on_quit = self._request_quit
        try:
            hotkeys.start()
        except Exception as exc:
            logger.warning("No se pudieron registrar los atajos globales: %s", exc)
            if self._continuous_voice or self._talking:
                # Reitera el estado que el usuario dejó puesto mientras el motor
                # aún no existía (la app tarda ~4 s en arrancarlo). Si el turno
                # lo abrió una pulsación y no la escucha continua, es un turno
                # acotado y puede cerrarse solo por silencio.
                assistant.set_voice(True, toggle=bool(self._talking))
        try:
            loop.run_until_complete(assistant.run())
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - error fatal en segundo plano
            logger.exception("Error del motor: %s", exc)
            self._uiput("sys", f"Error del motor: {exc}")
            self._uiput("err", None)
        finally:
            hotkeys.stop()
            with self._lock:
                self._loop = None
                self._assistant = None
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
        self._uiput("dead", None)

    def _uiput(self, kind: str, payload: object | None = None) -> None:
        # Coalesce: el nivel de voz se actualiza ~5 veces/s y solo importa el
        # último valor. Con un nivel ya pendiente, el siguiente lo refrescará.
        if kind == "lvl" and self._lvl_pending:
            return
        if self._ui_queue.qsize() > 2000 and kind not in {
            "ptt_on",
            "ptt_off",
            "toggle",
            "dead",
            "err",
            "lvl",
            "quit",
        }:
            logger.debug("Cola de UI saturada, soltando mensaje (%s)", kind)
            _perf.note_drop()
            return
        self._ui_queue.put((kind, payload))
        if kind == "lvl":
            self._lvl_pending = True

    def _drain_ui_queue(self) -> None:
        # Marca de Fase 0: un ciclo de volcado de la cola. Con esto se mide lo
        # que tarda el HUD en atender un mensaje, no la velocidad del reloj.
        _perf.ui_cycle_start()
        try:
            self._drain_ui_queue_cuerpo()
        finally:
            _perf.ui_cycle_end()

    def _drain_ui_queue_cuerpo(self) -> None:
        deadline = time.monotonic() + 0.05
        for _ in range(200):
            if time.monotonic() >= deadline:
                break
            try:
                kind, payload = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "ia":
                self._ia_turn.append(payload or "")
                self.add_history("IA", payload or "")
                self._set_processing(self._talking or self._continuous_voice)
            elif kind == "done":
                self._last_ia = "".join(self._ia_turn).strip()
                self._ia_turn = []
                if self._last_ia:
                    self._acts_wanted = True
                    self._acts.setVisible(not self._settings_open)
                    self._relayout_panel()
                self._set_processing(self._talking or self._continuous_voice)
            elif kind == "mute":
                self._muted = bool(payload == "1")
                if self._muted:
                    self.add_history("Sys", "Salida de voz silenciada.")
                else:
                    self.add_history("Sys", "Salida de voz activada.")
                self._mute_lbl.setText("🔇 silenciado" if self._muted else "")
            elif kind == "me":
                self.add_history("Tú", payload or "")
            elif kind == "lvl":
                self._lvl_pending = False
                level = float(payload) if isinstance(payload, (int, float)) else None
                self._show_voice_level(level)
            elif kind == "toks":
                self._set_tokens(payload or "")
            elif kind == "devs":
                self._apply_devices(payload)
            elif kind == "sys":
                self.add_history("Sys", payload or "")
            elif kind == "web":
                self.add_history("Web", payload or "")
            elif kind == "state":
                self._on_assistant_state(payload or "")
            elif kind == "timers":
                self._timers_lbl.setText(payload or "")
                self._timers_lbl.setToolTip(
                    payload or "Sin temporizadores pendientes"
                )
            elif kind == "err":
                self._set_status("#ff5d5d", "MindVoice — error")
            elif kind == "dead":
                self._set_status("#ff5d5d", "MindVoice — detenido")
            elif kind == "quit":
                self.close()
            elif kind == "toggle":
                self.toggle()
            elif kind == "ptt_on":
                self._ptt_key_down()
            elif kind == "ptt_off":
                self._ptt_key_up()

    def _on_assistant_state(self, state: str) -> None:
        """Refleja el estado del motor (I12) en el chip junto al punto."""
        label = {
            "LISTENING": "escuchando",
            "PROCESSING": "procesando",
            "SPEAKING": "hablando",
            "ERROR": "reconectando",
            "IDLE": "en reposo",
        }.get(state, state and state.lower() or "")
        colors = {
            "escuchando": f"{TKN.cian}",
            "procesando": f"{TKN.amarillo}",
            "hablando": f"{TKN.verde}",
            "reconectando": "#ff5d5d",
            "en reposo": f"{TKN.tinta_tenue}",
        }
        self._state_lbl.setText(f"· {label}" if label else "")
        self._state_lbl.setStyleSheet(
            f"color:{colors.get(label, TKN.tinta_suave)};font:11px 'Consolas';"
        )
        self._refrescar_halo(label, colors.get(label, TKN.tinta_suave))

    def _refrescar_halo(self, label: str, color: str) -> None:
        """Enciende o apaga el acento según el estado del motor.

        Aquí, y solo aquí, se paga el arranque del motor QML: un estado que
        merece acento (escuchando/procesando/hablando/reconectando) lo carga la
        primera vez. En reposo no se carga nada, así que un HUD recién abierto
        que no usa el motor no paga QML.

        Si la isla no está disponible (sin QtQuick, QML inválido), ``animar``
        devuelve sin más y el HUD se queda como estaba: con acento no, pero
        funcionando.
        """
        if self._halo is None:
            return
        if label in ("escuchando", "procesando", "hablando", "reconectando"):
            self._acomodar_halo()
            self._halo.animar(label, color)
            self._halo_estado = label
        else:
            self._halo.reposar()
            self._halo_estado = ""

    def _on_submit(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self._submit_text(text)

    def _submit_text(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if not self._cmd_history or self._cmd_history[-1] != text:
            self._cmd_history.append(text)
            self._cmd_history = self._cmd_history[-50:]
        self._cmd_idx = -1
        self._slash_cycle = 0
        self.add_history("Tú", text)
        with self._lock:
            assistant = self._assistant
        if assistant is None or not assistant.submit_command(text):
            self.add_history("Sys", "El motor aún no está listo; espera la conexión.")
            return
        self._set_processing(True)

    # ------------------------------------------------------------------
    # Historial de órdenes (↑/↓) y autocompletado slash (Tab)
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """↑/↓ recorren el historial de órdenes; Tab completa comandos "/…"."""
        if obj is self.input and event.type() == QEvent.Type.FocusIn:
            self._prefetch_screen_context()
        if obj is self.input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Up:
                if self._cmd_history:
                    if self._cmd_idx < 0:
                        self._cmd_idx = len(self._cmd_history) - 1
                    else:
                        self._cmd_idx = max(0, self._cmd_idx - 1)
                    self.input.setText(self._cmd_history[self._cmd_idx])
                    self.input.setCursorPosition(len(self.input.text()))
                return True
            if key == Qt.Key.Key_Down:
                if self._cmd_history and self._cmd_idx >= 0:
                    self._cmd_idx += 1
                    if self._cmd_idx >= len(self._cmd_history):
                        self._cmd_idx = -1
                        self.input.clear()
                    else:
                        self.input.setText(self._cmd_history[self._cmd_idx])
                        self.input.setCursorPosition(len(self.input.text()))
                return True
            if key == Qt.Key.Key_Tab:
                self._complete_slash()
                return True
        return super().eventFilter(obj, event)

    def _complete_slash(self) -> None:
        """Tab sobre una orden '/' cicla por los comandos cortos existentes."""
        text = self.input.text().strip()
        if not text.startswith("/"):
            return
        candidates = [c for c in _SLASH_COMMANDS if c.startswith(text)]
        if not candidates:
            return
        self._slash_cycle = (self._slash_cycle + 1) % len(candidates)
        self.input.setText(candidates[self._slash_cycle])
        self.input.setCursorPosition(len(self.input.text()))

    # ------------------------------------------------------------------
    # Acciones rápidas sobre la última respuesta
    # ------------------------------------------------------------------
    def _on_copy_last(self) -> None:
        if not self._last_ia:
            return
        QGuiApplication.clipboard().setText(self._last_ia)
        self.add_history("Sys", "Respuesta copiada al portapapeles")

    def _on_repeat_last(self) -> None:
        if not self._last_ia:
            return
        self._submit_text("Repite tu última respuesta")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("google.genai").setLevel(logging.INFO)

    args = [a for a in sys.argv[1:]]
    smoke = "--smoke" in args
    no_auto = "--no-auto" in args

    app = QApplication(sys.argv)
    try:
        if not branding.LOGO_ICO.exists():
            branding.generate_asset_files()
        if branding.LOGO_ICO.exists():
            app.setWindowIcon(QIcon(str(branding.LOGO_ICO)))
    except Exception as exc:  # noqa: BLE001 - el icono es cosmético
        logger.debug("No se pudo cargar el icono: %s", exc)

    settings = Settings()
    apply_prefs(settings)  # recupera micrófono/altavoz/volumen guardados
    widget = OverlayHud(settings)

    if not widget._single_ok:
        return 0

    if not no_auto and not smoke:
        if settings.api_key:
            widget._start_worker()
            if settings.overlay_voice_on_start:
                widget.set_voice_wanted(True)
        else:
            logger.error("No hay clave de la API de Gemini; el motor no arranca.")
            widget.add_history(
                "Sys",
                "Falta la clave de la API de Gemini. Consíguela gratis en "
                "aistudio.google.com/apikey: al volver a abrir MindVoice te "
                "la pido y la guardo cifrada.",
            )

    if smoke:
        QTimer.singleShot(1500, app.quit)

    widget.show_overlay()
    code = app.exec()
    return code


if __name__ == "__main__":
    raise SystemExit(main())