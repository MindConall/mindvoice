"""Interfaz gráfica sencilla (tkinter) para MindVoice.

El asistente corre en un hilo de trabajo con su propio bucle asyncio. El
usuario escribe órdenes de texto en la ventana y el modelo responde con voz
(native-audio, voz "Puck"). Las llamadas a Tk ocurren siempre en el hilo
principal para evitar cuelgues.
"""

import asyncio
import logging
import queue
import sys
import threading

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from branding import LOGO_ICO
from config import Settings
from hotkeys import HotkeyController
from live_assistant import LiveAssistant
from prefs import apply_prefs
from rutas import log_file

_AUDIBLE_LOGGERS = (
    "live_assistant",
    "media.playback",
    "media.screen",
    "hotkeys",
)

LOG_FILE = str(log_file("mindvoice-gui.log"))


class GuiLogHandler(logging.Handler):
    """Reenvía los registros de la app a la cola que consume la GUI."""

    def __init__(self, sink: "queue.Queue[str]") -> None:
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink.put(self.format(record))
        except Exception:
            pass


class MindVoiceGui:
    """Ventana principal: órdenes por teclado, controles y registro."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("MindVoice")
        root.geometry("560x480")
        root.resizable(False, False)
        try:
            root.iconbitmap(str(LOGO_ICO))
        except Exception:
            pass

        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._call_queue: "queue.Queue[object]" = queue.Queue()
        self._thread: threading.Thread = None
        self._loop: asyncio.AbstractEventLoop = None
        self._assistant: LiveAssistant = None
        self._hotkeys: HotkeyController = None
        self._lock = threading.Lock()

        self._build_widgets()
        self._wire_logging()
        self._poll_queues()

        # Arranque automático nada más abrir la ventana.
        root.after(300, self.start)

    # ------------------------------------------------------------------
    # Construcción de la ventana
    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        pad = dict(padx=8, pady=4)

        cmd = ttk.LabelFrame(self.root, text="Órdenes por teclado")
        cmd.pack(fill="x", **pad)

        row = ttk.Frame(cmd)
        row.pack(fill="x", padx=6, pady=4)
        self.cmd_var = tk.StringVar()
        self.cmd_entry = ttk.Entry(row, textvariable=self.cmd_var)
        self.cmd_entry.pack(side="left", fill="x", expand=True)
        self.cmd_entry.bind("<Return>", self._on_cmd_submit)
        ttk.Button(row, text="Enviar", command=self._on_cmd_submit).pack(
            side="left", padx=6
        )
        ttk.Label(
            cmd,
            text=(
                "Escribe una orden y pulsa Enter. El asistente responde con "
                "voz (gemini-3.1-flash-live-preview, voz 'Puck') y, con cada "
                "orden, ve una captura actual de tu pantalla."
            ),
        ).pack(anchor="w", padx=6, pady=(0, 6))

        ctrl = ttk.Frame(self.root)
        ctrl.pack(fill="x", **pad)
        self.btn_start = ttk.Button(ctrl, text="Iniciar", command=self.start)
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(ctrl, text="Detener", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.btn_voz = ttk.Button(ctrl, text="Voz: activo", command=self.toggle_mute)
        self.btn_voz.pack(side="left", padx=4)
        self.btn_limpiar = ttk.Button(ctrl, text="Limpiar", command=self._on_clear)
        self.btn_limpiar.pack(side="left", padx=4)
        self.btn_quit = ttk.Button(ctrl, text="Salir", command=self._on_close)
        self.btn_quit.pack(side="right", padx=4)

        self._status_var = tk.StringVar(value="Conectando…")
        ttk.Label(self.root, textvariable=self._status_var).pack(anchor="w", **pad)

        self._log_widget = scrolledtext.ScrolledText(
            self.root, height=13, state="disabled", font=("Consolas", 9)
        )
        self._log_widget.pack(fill="both", expand=True, **pad)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Órdenes por teclado
    # ------------------------------------------------------------------
    def _on_cmd_submit(self, _event=None) -> None:
        text = self.cmd_var.get().strip()
        if not text:
            return
        self.cmd_var.set("")
        self._append_log(f"[Tú] {text}")
        with self._lock:
            assistant = self._assistant
        if assistant is None or not assistant.submit_command(text):
            self._append_log("[MindVoice] El asistente aún no está listo; "
                             "espera a que conecte.")

    # ------------------------------------------------------------------
    # Registros (seguros para el hilo principal)
    # ------------------------------------------------------------------
    def _wire_logging(self) -> None:
        sink = self._log_queue
        gui_handler = GuiLogHandler(sink)
        gui_handler.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
        )
        for name in _AUDIBLE_LOGGERS:
            logger = logging.getLogger(name)
            logger.setLevel(logging.INFO)
            logger.addHandler(gui_handler)
            logger.addHandler(file_handler)

    def _enqueue_log(self, msg: str) -> None:
        self._log_queue.put(msg)

    def _gui_call(self, fn) -> None:
        """Ejecuta ``fn`` en el hilo principal desde otro hilo."""
        self._call_queue.put(fn)

    def _poll_queues(self) -> None:
        for _ in range(200):
            try:
                item = self._call_queue.get_nowait()
            except queue.Empty:
                break
            try:
                item()
            except Exception as exc:  # noqa: BLE001 - nunca tumbar la GUI
                print("Error en callback GUI:", exc)
        while True:
            try:
                msg = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(msg)
            if "Sesión Live conectada" in msg:
                self._set_status("Conectado")
            elif "Conexión perdida" in msg or "Reintento" in msg:
                self._set_status("Reconectando…")
            elif "Error" in msg:
                self._set_status("Error")
        self.root.after(100, self._poll_queues)

    def _append_log(self, msg: str) -> None:
        self._log_widget.configure(state="normal")
        self._log_widget.insert(tk.END, msg + "\n")
        self._log_widget.see(tk.END)
        self._log_widget.configure(state="disabled")

    def _on_clear(self) -> None:
        """Borra el texto del panel de registros en pantalla."""
        self._log_widget.configure(state="normal")
        self._log_widget.delete("1.0", tk.END)
        self._log_widget.configure(state="disabled")

    def _set_status(self, status: str) -> None:
        self._status_var.set(f"Estado: {status}")

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
        settings = Settings()
        apply_prefs(settings)

        self._thread = threading.Thread(
            target=self._worker, args=(settings,), name="mindvoice", daemon=True
        )
        self._thread.start()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._set_status("Conectando…")

    def stop(self) -> None:
        with self._lock:
            loop = self._loop
            assistant = self._assistant
        if loop is not None and loop.is_running() and assistant is not None:
            loop.call_soon_threadsafe(assistant.quit_event.set)

    def toggle_mute(self) -> None:
        with self._lock:
            hotkeys = self._hotkeys
        if hotkeys is not None:
            hotkeys.toggle()

    # ------------------------------------------------------------------
    # Hilo de trabajo del asistente
    # ------------------------------------------------------------------
    def _worker(self, settings: Settings) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Entrada por teclado: el botón "Voz" silencia solo la salida de audio.
        hotkeys = HotkeyController(
            mode=settings.mute_mode,
            ptt_key=settings.ptt_key,
            toggle_key=settings.toggle_key,
            initially_muted=False,
        )
        assistant = LiveAssistant(settings=settings, hotkeys=hotkeys)

        assistant.on_text = lambda text: self._enqueue_log(f"[Gemini] {text}")
        assistant.on_meta = lambda text: self._enqueue_log(f"[MindVoice] {text}")
        assistant.on_turn_complete = lambda: self._enqueue_log("[Gemini] Terminó de hablar.")
        hotkeys.on_mute_changed = lambda muted: self._gui_call(
            lambda: self.btn_voz.configure(
                text="Voz: silenciado" if muted else "Voz: activo"
            )
        )

        with self._lock:
            self._loop = loop
            self._assistant = assistant
            self._hotkeys = hotkeys

        try:
            loop.run_until_complete(assistant.run())
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - error fatal en segundo plano
            self._enqueue_log(f"Error del asistente: {exc}")
        finally:
            with self._lock:
                self._loop = None
                self._assistant = None
                self._hotkeys = None
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
        self._gui_call(self._on_worker_finished)

    def _on_worker_finished(self) -> None:
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self._set_status("Detenido")
        self._append_log("Asistente detenido.")

    # ------------------------------------------------------------------
    # Cierre
    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        with self._lock:
            busy = self._thread is not None and self._thread.is_alive()
        if busy and not messagebox.askokcancel("MindVoice", "¿Detener el asistente y salir?"):
            return
        self.stop()
        self.root.destroy()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("google.genai").setLevel(logging.INFO)
    root = tk.Tk()
    MindVoiceGui(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())