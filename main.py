"""Punto de entrada del asistente multitarea en vivo.

Ejecución básica::

    python main.py

El usuario escribe órdenes de texto por la terminal y el asistente responde
con voz (modelo native-audio). También observa la pantalla en tiempo real.

Ejecuta ``python main.py --help`` para ver todas las opciones.
"""

import argparse
import asyncio
import logging
import sys

from config import Settings
from hotkeys import HotkeyController
from live_assistant import LiveAssistant
from prefs import apply_prefs

import firstrun

logger = logging.getLogger("live")


def build_parser() -> argparse.ArgumentParser:
    """Crea el analizador de argumentos de línea de comandos."""
    parser = argparse.ArgumentParser(
        description="Asistente multitarea en tiempo real con Gemini Live "
        "(pantalla + órdenes por teclado + voz de salida).",
    )
    parser.add_argument(
        "--key",
        help="Clave API de Gemini. Si se omite, usa GEMINI_API_KEY.",
    )
    parser.add_argument(
        "--model",
        help="Identificador del modelo Live "
        "(por defecto gemini-3.1-flash-live-preview).",
    )
    parser.add_argument("--quit-keys", default="esc",
                        help="Combinación de teclas para cerrar la aplicación.")
    # ``default=None`` en los argumentos de pantalla: si no se pasan, manda la
    # configuración de ``config.py``/``prefs``. Con un default numérico aquí
    # se pisaba en cada arranque el valor de fidelidad con 1024 px.
    parser.add_argument("--fps", type=float, default=None,
                        help="Fotogramas de pantalla por segundo "
                             "(por defecto el de config.py).")
    parser.add_argument("--max-size", type=int, default=None,
                        help="Lado máximo de la imagen enviada "
                             "(por defecto el de config.py; p. ej. 2048).")
    parser.add_argument("--quality", type=int, default=None,
                        help="Calidad JPEG de la captura, 1-95 "
                             "(por defecto la de config.py).")
    parser.add_argument("--no-screen", action="store_true",
                        help="No enviar la pantalla al modelo.")
    parser.add_argument("--debug", action="store_true",
                        help="Habilita registros detallados.")
    return parser


def build_settings(args: argparse.Namespace) -> Settings:
    """Construye la configuración final combinando CLI + entorno + valores base."""
    settings = Settings()
    apply_prefs(settings)
    if args.key:
        settings.api_key = args.key
    if args.model:
        settings.model = args.model
    settings.quit_keys = args.quit_keys
    # Solo se pisan los valores de pantalla que se hayan pasado de verdad.
    if args.fps is not None:
        settings.screen_fps = args.fps
    if args.max_size is not None:
        settings.screen_max_size = args.max_size
    if args.quality is not None:
        settings.screen_quality = max(1, min(95, args.quality))
    settings.screen_enabled = not args.no_screen
    return settings


def _show_banner(settings: Settings) -> None:
    """Imprime un resumen de la configuración al arrancar."""
    screen = (
        f"captura por orden (JPEG ≤{settings.screen_max_size}px)"
        if settings.screen_enabled
        else "desactivada"
    )
    print("=" * 62)
    print("  Asistente multitarea en vivo (Gemini Live)")
    print(f"  Modelo   : {settings.model}")
    print(f"  Voz      : salida {settings.output_rate} Hz (Puck)")
    print(f"  Pantalla : {screen}")
    print("  Órdenes  : escribe texto y pulsa Enter (la respuesta se oye)")
    print(f"  Salir    : '{settings.quit_keys}'")
    print("=" * 62, flush=True)


async def _run_repl(assistant: LiveAssistant) -> None:
    """Lee líneas de la terminal y las entrega al asistente."""
    while True:
        line = await asyncio.to_thread(input, ">>> ")
        if not line.strip():
            continue
        try:
            assistant.submit_command(line)
        except Exception as exc:  # noqa: BLE001 - nunca tumbar el REPL
            print(f"Error al enviar la orden: {exc}")


async def amain(args: argparse.Namespace) -> int:
    """Lógica principal asíncrona del asistente."""
    settings = build_settings(args)
    if not settings.api_key:
        # Sin clave: se ofrece el diálogo de primera ejecución en vez de
        # terminar con un "Falta GEMINI_API_KEY" que no dice qué hacer.
        settings.api_key = firstrun.asegurar_clave()
    if not settings.api_key:
        print(
            "Falta la clave de la API de Gemini. Consíguela gratis en "
            "https://aistudio.google.com/apikey y vuelve a ejecutar, o "
            "defínela en la variable GEMINI_API_KEY.",
            file=sys.stderr,
        )
        return 2
    _show_banner(settings)

    loop = asyncio.get_running_loop()

    hotkeys = HotkeyController(
        mode=settings.mute_mode,
        ptt_key=settings.ptt_key,
        toggle_key=settings.toggle_key,
        quit_keys=settings.quit_keys,
    )

    # El asistente consultará a este controlador solo para silenciar la salida
    # de voz; la entrada es por teclado (submit_command).
    assistant = LiveAssistant(settings=settings, hotkeys=hotkeys)
    assistant.on_text = lambda text: print(f"\n[Gemini] {text}", flush=True)
    assistant.on_meta = lambda text: print(f"\n[Sys] {text}", flush=True)
    assistant.on_turn_complete = (
        lambda: print("[Gemini] Ha terminado de hablar.", flush=True)
    )

    hotkeys.on_quit = lambda: loop.call_soon_threadsafe(assistant.quit_event.set)
    hotkeys.on_mute_changed = lambda muted: print(
        ("[Voz] silenciada" if muted else "[Voz] activa"),
        flush=True,
    )
    hotkeys.start()

    repl = asyncio.create_task(_run_repl(assistant))
    try:
        await assistant.run()
    finally:
        repl.cancel()
        hotkeys.stop()
    return 0


def main() -> int:
    """Punto de entrada síncrono: configura, lanza el bucle y captura la salida."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("google.genai").setLevel(logging.INFO)

    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\nAsistente detenido por el usuario.")
        return 130
    except Exception as exc:  # noqa: BLE001 - error fatal no gestionado
        logger.exception("Error fatal: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())