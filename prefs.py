"""Preferencias del usuario guardadas en un JSON local.

Las preferencias de audio (micrófono, altavoz y volumen) que el usuario elige
en los Ajustes del overlay se persisten en ``user_prefs.json`` al lado del
código, de modo que sobreviven reinicios. Se aplican encima de la
configuración por defecto de ``config.Settings``.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from config import Settings, normalize_web_engine
from rutas import data_file

logger = logging.getLogger(__name__)

# Las preferencias viven en el directorio de datos del usuario (no junto al
# código) para que la app funcione instalada en "Program Files", donde el
# usuario no puede escribir. En una copia de desarrollo anterior se migra
# automáticamente desde la raíz del proyecto.
PREFS_FILE = data_file("user_prefs.json")

# Valores por defecto: coinciden con los de Settings.
DEFAULTS: Dict[str, Any] = {
    "mic_device_name": None,
    "output_device_name": None,
    "output_volume": 1.0,
    "voice_on_start": False,
    "tokens_lifetime": 0,
    "voice": "Puck",
    "transcript_lang": "es-ES",
    "web_search_enabled": True,
    "screen_enabled": True,
    "overlay_opacity": 0.7,
    "overlay_max_lines": 60,
    # ``None`` = el usuario NO ha escogido motor, así que decide
    # ``default_web_engine()`` (serper si hay SERPER_API_KEY, si no
    # DuckDuckGo). Guardar aquí "duckduckgo" a pelo fijaría el motor en un clon
    # limpio aunque tuviera clave de serper puesta.
    "web_search_provider": None,
    "serper_api_key": "",
    "web_smart_detect": True,
    "mute_mode": "toggle",
    "ptt_key": "right ctrl",
    "overlay_hotkey": "Ctrl+Shift+Z",
    "save_transcripts": True,
    "response_modalities": "audio",
    "voice_manual_vad": True,
}


def prefs_path() -> Path:
    """Ruta del archivo de preferencias del usuario."""
    return PREFS_FILE


def load_prefs() -> Dict[str, Any]:
    """Carga las preferencias guardadas (con valores por defecto)."""
    prefs = dict(DEFAULTS)
    try:
        if PREFS_FILE.exists():
            with open(PREFS_FILE, encoding="utf-8") as handle:
                stored = json.load(handle)
            if isinstance(stored, dict):
                prefs.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except Exception as exc:  # noqa: BLE001 - un JSON corrupto no debe romperla
        logger.warning("No se pudieron leer las preferencias (%s); se usan las de defecto.", exc)
    return prefs


def save_prefs(prefs: Dict[str, Any]) -> None:
    """Guarda las preferencias en ``user_prefs.json``."""
    clean = {k: v for k, v in prefs.items() if k in DEFAULTS}
    if "output_volume" in clean:
        clean["output_volume"] = max(0.0, min(1.5, float(clean["output_volume"] or 0.0)))
    if "tokens_lifetime" in clean:
        clean["tokens_lifetime"] = int(clean["tokens_lifetime"] or 0)
    temp_file = PREFS_FILE.with_name(f".{PREFS_FILE.name}.tmp")
    try:
        with open(temp_file, "w", encoding="utf-8") as handle:
            json.dump(clean, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_file, PREFS_FILE)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudieron guardar las preferencias: %s", exc)
        try:
            temp_file.unlink(missing_ok=True)
        except OSError:
            pass


def apply_prefs(settings: Settings, prefs: Optional[Dict[str, Any]] = None) -> Settings:
    """Aplica las preferencias del usuario sobre ``settings`` (misma instancia)."""
    prefs = prefs or load_prefs()
    settings.mic_device_name = prefs.get("mic_device_name") or None
    settings.output_device_name = prefs.get("output_device_name") or None
    settings.output_volume = float(prefs.get("output_volume") or 1.0)
    settings.overlay_voice_on_start = bool(prefs.get("voice_on_start"))
    voice = prefs.get("voice")
    if isinstance(voice, str) and voice.strip():
        settings.voice = voice.strip()
    lang = prefs.get("transcript_lang")
    if isinstance(lang, str) and lang.strip():
        settings.transcript_lang = lang.strip()
    settings.web_search_enabled = bool(prefs.get("web_search_enabled"))
    settings.screen_enabled = bool(prefs.get("screen_enabled"))
    try:
        settings.overlay_opacity = max(
            0.3, min(0.95, float(prefs.get("overlay_opacity") or 0.7))
        )
    except (TypeError, ValueError):
        pass
    try:
        settings.overlay_max_lines = max(
            20, min(200, int(prefs.get("overlay_max_lines") or 60))
        )
    except (TypeError, ValueError):
        pass
    # El motor se normaliza contra config.WEB_ENGINES: si el JSON guardado trae
    # "auto" o un proveedor ya eliminado (tavily, serpapi, brave, google),
    # se migra al motor por defecto en vez de dejar la app sin motor válido.
    provider = prefs.get("web_search_provider")
    settings.web_search_provider = normalize_web_engine(
        provider if isinstance(provider, str) else None
    )
    # Clave de los motores que la piden (serper.dev). Lo guardado en Ajustes
    # manda sobre la variable de entorno: si no, cambiar la clave desde el
    # panel no serviría de nada mientras SERPER_API_KEY siga puesta en Windows
    # (Settings la lee al construir). Vacío = se usa la del entorno, que es la
    # de fábrica.
    stored_key = prefs.get("serper_api_key")
    if isinstance(stored_key, str) and stored_key.strip():
        settings.serper_api_key = stored_key.strip()
    settings.web_smart_detect = bool(prefs.get("web_smart_detect"))
    mode = prefs.get("mute_mode")
    if isinstance(mode, str) and mode in ("toggle", "push_to_talk"):
        settings.mute_mode = mode
    key = prefs.get("ptt_key")
    if isinstance(key, str) and key.strip():
        settings.ptt_key = key.strip()
    hotkey = prefs.get("overlay_hotkey")
    if isinstance(hotkey, str) and hotkey.strip():
        settings.overlay_hotkey = hotkey.strip()
    # Vocabulario de voz: cadena vacía = se deja el valor por defecto de la
    # config, para que borrar el campo en Ajustes no desactive la ortografía.
    vocab = prefs.get("speech_vocabulary")
    if isinstance(vocab, str) and vocab.strip():
        settings.speech_vocabulary = vocab.strip()
    settings.save_transcripts = bool(prefs.get("save_transcripts"))
    modalities = prefs.get("response_modalities")
    if isinstance(modalities, str) and modalities.strip() == "audio_text":
        settings.response_modalities = ["AUDIO", "TEXT"]
    else:
        settings.response_modalities = ["AUDIO"]
    # Si la clave no está (user_prefs.json antiguo), se mantiene el valor por
    # defecto en vez de caer al VAD automático: callar es lo que se pidió evitar.
    settings.voice_manual_vad = bool(prefs.get("voice_manual_vad", True))
    return settings