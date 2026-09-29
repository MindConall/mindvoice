"""Configuración central del asistente.

Toda la configuración de la aplicación vive en un único sitio para que sea
sencillo ajustarla sin tocar el código de cada módulo. Los valores se pueden
sobrescribir mediante variables de entorno o argumentos de línea de comandos
(ver ``main.py``).
"""

import os
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional

import credenciales
from rutas import user_data_dir

# ---------------------------------------------------------------------------
# Claves y credenciales
# ---------------------------------------------------------------------------
#
# Ninguna credencial se incrusta en el código. Se resuelve en tiempo de
# ejecución desde el entorno o desde el almacén local cifrado; ver
# ``credenciales.py``.
DEFAULT_API_KEY = ""

# ---------------------------------------------------------------------------
# Parámetros de red y modelo
# ---------------------------------------------------------------------------
# Modelo multimodal Live (vista + audio). En 2026 quedaron obsoletos los
# "native-audio" para ver/responder bien con imagen: `gemini-2.5-flash-native-
# audio` no ve la pantalla (ignora el video realtime). `gemini-3.1-flash-live-
# preview` acepta turnos texto+imagen y responde con voz (PCM 24 kHz). El
# streaming continuo de audio/video agota su cuota por minuto (~25 msg), así
# que la app envía una captura fresca por orden, no un vídeo continuo.
MODEL_NAME = "gemini-3.1-flash-live-preview"
API_VERSION = "v1alpha"               # Canal Live usado por defecto. En v1beta se
                                      # observaron cortes 1008 mucho más frecuentes
                                      # (~20-30 s) con este modelo, así que por
                                      # defecto se usa v1alpha (sesiones estables
                                      # de ~2,5 min). Puedes probar v1beta en
                                      # Settings si quieres experimentar.
API_BASE_URL = "generativelanguage.googleapis.com"


# ---------------------------------------------------------------------------
# REGISTRO ÚNICO DE MOTORES DE BÚSQUEDA
# ---------------------------------------------------------------------------
# Este diccionario es la ÚNICA lista de motores que existe en la app. De él
# salen a la vez: las opciones del menú de Ajustes, la comprobación de "hace
# falta clave", la petición real, la etiqueta de la nota y el nombre que el
# modelo oye. Antes cada uno de esos sitios repetía su propia lista, y eso
# hacía que el motor realmente usado acabara siendo distinto del que la IA
# decía haber usado: el fallo que motivó este registro.
#
#   key        identificador técnico (el valor de web_search_provider)
#   label      lo que se ve en el menú
#   spoken     cómo se pronuncia en voz alta (corto: sin paréntesis)
#   note       cómo se identifica dentro de la nota de resultados
#   key_field  atributo de Settings con la clave; None = no hace falta
#   key_env    variable de entorno donde vive la clave
WEB_ENGINES = {
    "duckduckgo": {
        "label": "DuckDuckGo (gratis, sin clave)",
        "spoken": "DuckDuckGo",
        "note": "DuckDuckGo",
        "key_field": None,
        "key_env": None,
    },
    "serper": {
        "label": "serper.dev (2.500/mes gratis, con clave)",
        "spoken": "serper.dev",
        "note": "serper.dev (búsqueda en Google)",
        "key_field": "serper_api_key",
        "key_env": "SERPER_API_KEY",
    },
}

# Motor por defecto. DuckDuckGo es el prioritario: no pide cuenta ni clave.
DEFAULT_WEB_ENGINE = "duckduckgo"


def normalize_web_engine(value: Optional[str]) -> str:
    """Normaliza a una clave válida del registro (si no, el motor por defecto).

    Todo lo que venga de Ajustes, de un JSON de preferencias o de una variable
    de entorno pasa por aquí, así que un valor desconocido o de un proveedor
    eliminado (tavily, serpapi, brave, "auto"…) cae en DuckDuckGo en vez de
    dejar la app en un estado inconsistente sin motor.
    """
    v = (value or "").strip().lower()
    return v if v in WEB_ENGINES else DEFAULT_WEB_ENGINE


def engine_label(key: str) -> str:
    return WEB_ENGINES[key]["label"]


def engine_spoken(key: str) -> str:
    return WEB_ENGINES[key]["spoken"]


def engine_note_name(key: str) -> str:
    return WEB_ENGINES[key]["note"]


def engine_needs_key(key: str) -> bool:
    return WEB_ENGINES[key]["key_field"] is not None


def engine_key_field(key: str) -> Optional[str]:
    return WEB_ENGINES[key]["key_field"]


def engine_key_env(key: str) -> Optional[str]:
    return WEB_ENGINES[key]["key_env"]


def _default_data_dir() -> str:
    """Directorio de trabajo por defecto, con degradación si no se puede crear.

    Si el directorio del usuario no se puede crear (permisos raros, disco
    lleno), se recurre a la carpeta temporal para que la app siga arrancando en
    vez de morir en la construcción de ``Settings``.
    """
    try:
        return str(user_data_dir(create=True))
    except OSError:
        return os.path.join(tempfile.gettempdir(), "mindvoice")


@dataclass
class Settings:
    """Parámetros globales del asistente."""

    # -- Gemini -------------------------------------------------------------
    # La clave se resuelve en este orden: GEMINI_API_KEY del entorno,
    # MINDVOICE_API_KEY del entorno y, por último, el almacén local cifrado
    # que rellena el asistente de primera ejecución. Así el usuario normal no
    # tiene que tocar variables de entorno para usar la app instalada.
    api_key: str = field(default_factory=credenciales.get_api_key)
    model: str = MODEL_NAME
    api_version: str = API_VERSION
    # Reanudación de sesión (experimental). Con va1pha el servidor no emite
    # handles fiables y, en pruebas, las sesiones reanudadas se cortaban con
    # 1008 a los ~20-30 s. Off por defecto; actívalo y observa si las
    # reconexiones conservan el contexto sin acortar las sesiones.
    session_resumption: bool = False
    # Voz de síntesis de voz. Necesaria para que los modelos Live devuelvan
    # audio (sin una voz configurada responden solo en texto).
    voice: str = "Puck"
    # Idioma de la transcripción de audio (entrada y salida). Cambiable en
    # caliente desde los Ajustes del overlay; se aplica al reconectar la sesión.
    transcript_lang: str = "es-ES"

    # -- Audio de entrada (micrófono) --------------------------------------
    # Entrada por voz opcional (botón/modo voz del overlay). El streaming usa
    # realtime audio con un gate de silencio para no agotar cuota con ruido.
    input_rate: int = 16000          # Hz de muestreo que espera Gemini
    input_channels: int = 1
    input_chunk_ms: int = 200        # duración de cada fragmento de voz
    mic_device_name: Optional[str] = None

    # -- Audio de salida (altavoces) ---------------------------------------
    output_rate: int = 24000         # Hz del PCM que devuelve Gemini
    output_channels: int = 1
    # Nombre exacto del dispositivo de salida elegido por el usuario
    # (None = predeterminado de Windows). Se puede cambiar en caliente.
    output_device_name: Optional[str] = None
    output_volume: float = 1.0       # ganancia aplicada al PCM (0.0-1.5)

    # -- Pantalla -----------------------------------------------------------
    screen_enabled: bool = True         # False para desactivar el envío de imagen
    screen_fps: float = 1.0          # fotogramas por segundo enviados
    # Lado máximo tras reducir la resolución.
    # La lectura de texto depende por completo de esto: a 1024 px una pantalla
    # de 1920x1080 llegaba al modelo como 1024x576 y el texto pequeño (terminal,
    # navegador, PDF) se volvía ilegible. 2048 px es el lado largo máximo que
    # Gemini admite al trocear la imagen, así que se reduce lo justo y se
    # conserva el detalle nativo de un monitor 1080p/1440p.
    screen_max_size: int = 2048      # lado máximo tras reducir resolución
    screen_quality: int = 95         # calidad JPEG (1-95); 95 es casi sin pérdida
    screen_monitor: int = 1          # índice de monitor (1 = pantalla principal)

    # -- Teclas rápidas -----------------------------------------------------
    # Modo de silencio: "push_to_talk" (mantén pulsada la tecla para hablar)
    # o "toggle" (pulsa una tecla para conmutar silencio <-> activo).
    mute_mode: str = "push_to_talk"
    ptt_key: str = "right ctrl"
    # En modo alterno, esta tecla conmuta el silencio (Mute toggle).
    toggle_key: str = "f9"
    # Combinaciones que cierran la aplicación.
    quit_keys: str = "esc"

    # -- Duración del turno de voz ------------------------------------------
    # True  = VAD MANUAL: el servidor NO cierra el turno por silencio; solo se
    #         cierra cuando el usuario suelta el botón (se mandan activity_start
    #         / activity_end). Permite hablar indefinidamente sin que la IA
    #         conteste a mitad de una frase por una pausa al pensar.
    # False = VAD AUTOMÁTICO (comportamiento anterior): el servidor decide el
    #         fin de turno por ``_VAD_SILENCE_MS`` de silencio.
    voice_manual_vad: bool = True

    # -- Reconexión ---------------------------------------------------------
    reconnect_max_delay: float = 30.0   # tope de espera entre reintentos (s)
    reconnect_base_delay: float = 1.0   # espera inicial entre reintentos (s)
    # Watchdog: si hay un turno en curso (hablando o esperando respuesta a una
    # orden/voz) y no llegan datos del servidor durante este tiempo, la sesión
    # se recicla sola en vez de quedarse muda para siempre. 30 s evita que las
    # respuestas largas (que empiezan a generar con algo de retardo) se corten
    # justo al final; una sesión de verdad congelada aún se recupera rápido.
    response_stall_timeout: float = 30.0  # segundos sin datos que disparan reset

    # -- Overlay HUD (Windows 11) ------------------------------------------
    # Atajo global que muestra/oculta el overlay (formato "Ctrl+Shift+Z").
    overlay_hotkey: str = "Ctrl+Shift+Z"
    overlay_monitor: int = 1           # índice de monitor (1 = principal)
    overlay_opacity: float = 0.7       # opacidad del panel (0.3-0.95)
    overlay_max_lines: int = 60        # líneas del historial en la ventana
    overlay_show_on_start: bool = True # mostrar al abrir la app
    # Escucha continua opcional. Por defecto la voz es push-to-talk: se activa
    # solo mientras mantienes pulsado el botón "Voz" del overlay.
    overlay_voice_on_start: bool = False

    # -- Experiencia --------------------------------------------------------
    # Respuestas del modelo: ["AUDIO"] devuelve voz; puedes añadir "TEXT".
    response_modalities: List[str] = field(
        default_factory=lambda: ["AUDIO"]
    )
    # Guardar en "sessions/AAAA-MM-DD.txt" (dentro de data_dir) cada turno
    # de la conversación (Tú/IA/Web), con marca de hora. Sirve para repasar
    # qué se habló cualquier día sin depender de la memoria del modelo.
    save_transcripts: bool = True
    system_instruction: str = (
        "Eres MindVoice, un asistente de IA afilado, con criterio y con "
        "voz. El usuario te habla de lo que está viendo en su escritorio y "
        "te da órdenes por teclado. Reglas:\n"
        "1. Calibra la extensión a la pregunta: lo trivial y simple (sí/no, "
        "un dato, una cifra, una confirmación, una orden breve) respóndelo "
        "DIRECTO y resumido en una o dos frases, sin rodeos ni desarrollo. "
        "Si el tema tiene sustancia (una idea, un plan, una rutina, una "
        "lista, una explicación, una comparativa o un análisis), desarrójalo "
        "COMPLETO y entero: pasos numerados, sub-listas, detalles, ejemplos "
        "y matices, tantas oraciones como haga falta para cerrar el tema. "
        "No alargues lo simple ni recortes lo importante; sin relleno, "
        "saludos ni muletillas.\n"
        "2. Eres consciente del contexto visual de la pantalla del usuario: "
        "en órdenes de texto puede aparecer como nota '(Contexto visual: …)' "
        "o, si la app no pudo leer la pantalla, como nota '(Sin contexto "
        "visual: …)'. Usa la primera EN SILENCIO (no digas 'estoy viendo tu "
        "pantalla', ni describas lo que ves, ni lo anuncies): simplemente "
        "responde como quien lo tiene delante. Ante la segunda NO tienes "
        "visión: no afirmes qué se ve, no cites pantallas, ventanas ni "
        "programas, y no deduzcas lo que el usuario podría estar viendo. Si te "
        "preguntan por la pantalla, dilo con esas palabras y pide que te lo "
        "describan. NUNCA inventes lo que ves, ni apps, ni ventanas, ni "
        "contenido, ni horas.\n"
        "3. Obedece la orden LITERALMENTE. Haz exactamente lo que te piden, "
        "nada más: si piden un dato, el dato; si piden una lista, la lista; si "
        "piden que hagas algo, hazlo. No conviertas un encargo en un "
        "consejo, no amplíes el alcance, no preguntes lo que ya te han dicho y "
        "no sustituyas la tarea por una explicación sobre la tarea. Cuando "
        "sepas hacer algo, entrégalo.\n"
        "4. Si la pregunta NO es sobre la pantalla (ideas, código, análisis, "
        "planes), respóndela con sustancia y razonamiento, ignorando la nota."
        "\n"
        "5. Mantente en el tema exacto de la orden. No divagues ni repitas "
        "lo ya dicho.\n"
        "6. Lleva el hilo de la conversación: usa lo hablado antes y las "
        "interacciones previas como contexto. Cuando el usuario retome un "
        "tema anterior ('lo que me decías antes', 'esas capacidades de "
        "Lain'), recupera el hilo y continúa donde quedó, sin pedirle que "
        "repita lo ya dicho.\n"
        "7. Si un turno incluye una nota de 'Resultado de búsqueda en "
        "internet' o un dato de clima o temporizador, úsalo SIEMPRE como "
        "base para responder sobre datos actuales, con tus palabras y de "
        "forma natural hablada. NUNCA digas que no tienes acceso a internet: "
        "la app busca por ti. Si te piden información reciente y el turno NO "
        "trae nota de búsqueda, responde con tu conocimiento y ofrece "
        "buscarlo ('¿quieres que lo busque?').\n"
        "8. Habla de corrido y sin cortes: cuando arranques una respuesta "
        "desarrójala entera (según la regla 1), no te detengas a preguntar "
        "si seguir ni interrumpas tus propias respuestas largas a mitad. La "
        "extensión depende del tema: resumida si es trivial, completa si "
        "es sustantivo.\n"
        "9. Cuando el turno traiga un dato etiquetado como confirmado (nota "
        "'Cálculo exacto', 'Resultado de búsqueda' o 'clima …'), trátalo "
        "como verdad irrefutable: úsalo y no lo recalcules, cuestiones ni lo "
        "repitas literalmente; integra el dato en tu respuesta con "
        "naturalidad.\n"
        "10. La hora y la fecha locales llegan en la cabecera de cada orden, "
        "entre corchetes (p. ej. '[mar 23/09 14:31:07]'). Úsalas para todo lo "
        "que dependa del momento (qué hora es, cuánto falta, cuándo es) y no "
        "calcules la hora por tu cuenta."
    )
    # Nombres propios y términos técnicos que el usuario dice en voz y que el
    # reconocedor autocorrige a otra cosa. Sin esto, "OpenCode" se transcribía
    # como "OpenCog" (o al revés) y toda búsqueda posterior salía mal: con el
    # nombre cambiado, el modelo además respondía sobre el otro programa.
    # Se inyecta en la instrucción del sistema como ortografía LITERAL.
    speech_vocabulary: str = (
        "OpenCode, Tetravex, GraphBoost, OpenCog, MindVoice"
    )

    # Transcripciones, memoria de largo alcance y cualquier archivo derivado.
    # Van al directorio de datos del usuario (%LOCALAPPDATA%\MindVoice en
    # Windows), nunca a la carpeta temporal: allí el sistema puede borrarlos sin
    # aviso y se perdían al reiniciar.
    data_dir: str = field(default_factory=_default_data_dir)

    # -- Búsqueda en internet (opcional) ------------------------------------
    # El modelo *-live-preview no navega en vivo. A cambio, cuando la orden
    # implica información actual, la app hace una consulta grounding de Google
    # Search (generateContent aparte, con el mismo API key) y inyecta el
    # resumen como nota al turno, igual que la descripción de pantalla. OJO: el
    # grounding consume una cuota propia del plan de tu API key; si da 429
    # (RESOURCE_EXHAUSTED), la búsqueda no trae datos y el modelo responde con
    # su propio conocimiento avisando de que no pudo verificar en línea.
    web_search_enabled: bool = True
    # Detector "inteligente" de búsqueda web: cuando la orden NO lleva una frase
    # explícita ("busca en internet", "/web …"), una llamada rápida de IA decide
    # si la pregunta necesita datos actuales y propone la consulta. Sin esto,
    # "¿quién ganó ayer el partido?" pasaba sin buscar. Desactívala si prefieres
    # solo los disparadores explícitos (algo más rápido, menos ávido).
    web_smart_detect: bool = True
    # Modelo REST (generateContent) usado para buscar y para el detector
    # inteligente. ``None`` = resolver automáticamente: la app recorre los
    # nombres vigentes (``gemini-3.x-flash``…) y los modelos reales que
    # devuelve tu API (``models.list``) usando el primero de texto que
    # responde, y lo recuerda para el resto de la sesión. Pon uno concreto
    # (p. ej. "gemini-3.1-flash-lite") si quieres forzar uno. En 2026 los
    # ``gemini-2.5-flash``/``gemini-2.0-flash`` dan 404 "no longer available
    # to new users": no los uses como override.
    web_search_model: Optional[str] = None
    # Proveedor real de los resultados web. El "grounding de Google Search"
    # (generateContent con la herramienta) es cómodo pero consume una CUOTA
    # Motor de búsqueda. Los valores válidos son las claves de ``WEB_ENGINES``
    # (abajo). El menú, la petición real, la nota y el anuncio al modelo salen
    # TODOS de ese registro, así que no pueden discrepar entre sí.
    web_search_provider: str = DEFAULT_WEB_ENGINE
    # Proxy opcional SOLO para las búsquedas web.
    # Útil cuando tu proveedor bloquea tu país. Déjalo en ``None`` para usar los
    # proxies del entorno (HTTPS_PROXY/ALL_PROXY) o conexión directa. Formato:
    # una URL tipo "http://127.0.0.1:8080" o "socks5://user:pass@host:1080".
    # Se lee de la variable de entorno MINDVOICE_WEB_PROXY (o se pone aquí).
    web_proxy: Optional[str] = field(
        default_factory=lambda: os.environ.get("MINDVOICE_WEB_PROXY")
    )
    # Clave de serper.dev (2.500 consultas/mes gratis, variable SERPER_API_KEY).
    serper_api_key: Optional[str] = field(
        default_factory=lambda: os.environ.get("SERPER_API_KEY")
    )
    # serper.dev: 2.500 consultas/mes gratis sin tarjeta, resultados de Google.
    # Se añadió porque, medido desde esta IP, Tavily y Exa devuelven 403 por
    # país mientras serper.dev responde bien. La clave va en SERPER_API_KEY.
    # Tope de caracteres de la nota de búsqueda que recibe el modelo.
    web_note_max_chars: int = 700


def get_settings() -> Settings:
    """Devuelve una instancia de configuración (singleton ligero)."""
    return Settings()