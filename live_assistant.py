"""Bucle en vivo: conexión WebSocket bidireccional con Gemini Live.

Coordina varios flujos concurrentes sobre la sesión Live:

1. ``_send_command_loop``: por cada orden del usuario DESCRIBE la pantalla
   actual con una mini-sesión Live de visión y envía la orden junto a esa
   descripción como un turno de usuario de TEXTO (``send_client_content``).
   La imagen NUNCA entra en la sesión de voz: probado en vivo que, en cuanto
   un turno de ``send_client_content`` contiene una imagen inline, el servidor
   deja de responder a los turnos de audio realtime posteriores (la sesión se
   queda muda). La pantalla se "traduce" a texto en una sesión aparte y ese
   texto se inyecta como nota de contexto.
   (El canal ``send_realtime_input(video=…)`` tampoco vale: aceptaba el JPEG
   pero el modelo NO lo procesaba y confabulaba aplicaciones inexistentes.)
2. ``_receive_loop``: recibe el audio (PCM 24 kHz) y lo reproduce.
3. ``_voice_loop``: entrada opcional por micrófono. Solo transmite cuando el
   usuario activa la voz (``set_voice``) y con un gate de silencio para no
   agotar cuota reenviando ruido de fondo. Al hablar se interrumpe
   automáticamente (barge-in) la respuesta en curso del modelo.

El streaming continuo de video se evita: los modelos ``*-live-preview`` de
esta API agotan su cuota por minuto con reenvíos continuos, así que la app
solo captura un fotograma al describir la pantalla (por orden de texto o turno
de voz), no un vídeo permanente.

Los modelos ``*-live-preview`` solo devuelven *audio* (no texto), así que la
sesión habilita transcripción de salida y de entrada: lo que el modelo dice se
transcribe y se entrega como texto (``on_text``) y tu voz se transcribe
(``on_user_text``) para que la conversación se vea en el chat.

Incluye reconexión automática con retroceso exponencial ante caídas de red,
y distingue errores transitorios de errores permanentes (clave inválida,
cuotas agotadas, etc.).
"""

import ast
import asyncio
import ctypes
import enum
import json
import logging
import math
import os
import re
import struct
import tempfile
import threading
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Callable, NamedTuple, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
from google import genai
from google.genai import types

from config import (
    Settings,
    WEB_ENGINES,
    WEB_SEARCH_MODEL,
    engine_key_env,
    engine_key_field,
    engine_needs_key,
    normalize_web_engine,
)
from hotkeys import HotkeyController
from media import AudioPlayer, MicrophoneCapture, ScreenCapture
from perf_instr import PERF as _perf
from rutas import data_file, ensure_data_dir

logger = logging.getLogger(__name__)

class AssistantState(enum.Enum):
    """Estados de la máquina de estados del motor Live.

    Reflejan lo que el motor está haciendo en cada momento y se publican al
    overlay (``on_state``) sin ambigüedad:

    - ``IDLE``: sin turno en curso y sin micrófono transmitiendo.
    - ``LISTENING``: el micrófono está activo (push-to-talk o escucha continua).
    - ``PROCESSING``: orden/voz enviada; el modelo todavía no habla.
    - ``SPEAKING``: el modelo está emitiendo audio/transcripción.
    - ``ERROR``: sesión caída o en reconexión; el motor se recupera solo.
    """
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"
    ERROR = "ERROR"

# Ventana y umbral para escalar el reset: con 3+ turnos congelados en 2 minutos
# (watchdog disparándose repetido) se reinicia también el pipeline de salida
# (reopen del stream), no solo la sesión de red. Es el "hard reset" de la FASE 2.
_RESET_WINDOW_S = 120.0
_HARD_RESET_ESCALATE = 3

# Mensaje que se envía al modelo para cortar una respuesta en curso SIN cerrar
# la sesión (soft reset). Con gemini-3.1-flash-live-preview los mensajes en
# mitad de sesión solo se aceptan como realtime_input de texto.
#
# ADVERTENCIA (medido contra la API): mandar este texto mientras el modelo aún
# NO está emitiendo nada (p. ej. con ``_awaiting_turn`` activo, es decir, solo
# procesando la voz que acabas de soltar) deja la sesión MUERTA: el servidor
# deja de transcribir y de responder para siempre, sin error ni go_away, y la
# app se queda "escuchando" hasta que el watchdog reinicia la sesión. Por eso
# solo se usa para un Cancelar explícito o cuando el modelo está HABLANDO, y
# nunca como parte del barge-in routine (ver ``_voice_loop``).
_INTERRUPT_TEXT = (
    "(Interrupción) Detén tu respuesta y espera la siguiente orden. No añadas nada."
)

# Silencio digital que se envía al final de cada turno de voz, justo antes de
# ``audio_stream_end``. El VAD del servidor decide que el usuario terminó de
# hablar por la pausa que PERCIBE en el audio recibido: el gate de micrófono
# descarta los fragmentos silenciosos, así que sin esta cola el servidor recibe
# voz Pegada sin huecos, no detecta el final de la frase y el turno se queda
# abierto (sin transcripción y sin respuesta) hasta que el watchdog reinicia la
# sesión. Medido: 200 ms de cola = turno muerto; 400-600 ms = respuesta normal.
_VOICE_TAIL_S = 0.8

# Silencio LOCAL que da una frase por terminada aunque el micrófono siga
# abierto (modo alterno / escucha continua). Antes el turno se cerraba solo al
# CERRAR la sesión de micrófono, así que con el micro abierto el modelo nunca
# recibía ``audio_stream_end`` y se quedaba sin responder: la transcripción de
# entrada llegaba ("Tú: hola") pero la respuesta no. Ahora, tras estos segundos
# sin audio por encima del gate se envía la cola de silencio +
# ``audio_stream_end`` y el micrófono sigue escuchando la frase siguiente.
# 1,0 s: por debajo se parte la frase en dos turnos; muy por encima llega la
# pausa natural que ya hace el usuario.
_VOICE_SEGMENT_SILENCE_S = 1.0

# El servidor deja de vez en cuando un turno de voz SIN responder (ni
# transcripción, ni audio, ni error, ni go_away: la sesión se queda muda). Se
# detecta por ausencia total de datos y se recupera sola: se reabre la sesión y
# se reenvía el audio ya capturado (una sola vez por turno). Antes el usuario
# se quedaba "escuchando" hasta 30 s y tenía que repetir la frase. 25 s es el
# margen: medidas respuestas reales de 5-10 s y casos muertos sin límite.
_VOICE_DEAD_S = 25.0
_VOICE_REPLAY_MAX_S = 25.0

# Silencio (ms) que el servidor exige para cerrar un turno de voz. Por debajo de
# ~300 ms el VAD no daba el turno por terminado y la frase se quedaba abierta.
_VAD_SILENCE_MS = 300

# Respuestas largas: el servidor recorta silenciosamente el turno cuando el
# audio excede su tope por turno, dejando la frase "colgada". Para no dejar así
# la conversación, si el texto del turno parece CORTADO se pide al modelo que
# continúe exactamente donde se quedó (cada turno de usuario admite unas pocas
# rondas de autocompletado y las rondas se cortan si completan limpiamente).
_CONTINUE_MAX_ROUNDS = 3
_CONTINUE_MIN_CHARS = 350
_CONTINUE_TEXT = (
    "(El usuario pidió una respuesta completa y aquí se cortó. Continúa "
    "EXACTAMENTE donde te quedaste, termina el punto que desarrollabas y cierra "
    "con un punto. No repitas ni resumas lo ya dicho.)"
)

# Motivos de cierre de turno (TurnCompleteReason) que indican que la generación
# fue RECHAZADA o BLOQUEADA (seguridad del modelo), no un corte natural. En esos
# casos nociones como "pedir la continuación" no tienen sentido: se avisa al
# usuario y se cierra limpio. ``NEED_MORE_INPUT`` (ausente aquí) es el cierre
# normal: el modelo terminó y espera más voz del usuario.
_TURN_REJECT_REASONS = frozenset(
    (
        "RESPONSE_REJECTED",
        "MAX_REGENERATION_REACHED",
        "GENERATED_CONTENT_SAFETY",
        "GENERATED_AUDIO_SAFETY",
        "GENERATED_VIDEO_SAFETY",
        "GENERATED_CONTENT_PROHIBITED",
        "GENERATED_CONTENT_BLOCKLIST",
        "GENERATED_IMAGE_SAFETY",
        "GENERATED_IMAGE_PROHIBITED",
        "GENERATED_IMAGE_CELEBRITY",
        "GENERATED_IMAGE_PROMINENT_PEOPLE_DETECTED_BY_REWRITER",
        "GENERATED_IMAGE_IDENTIFIABLE_PEOPLE",
        "GENERATED_IMAGE_MINORS",
        "GENERATED_OTHER",
    )
)

# Integración de cálculo: reconoce cuentas escritas en español ("cuánto es 15% "
# "de 890", "14 por 3", "8 entre 2") y las resuelve en la app ANTES de enviar la
# orden, inyectando el resultado como dato confirmado (el modelo nunca inventa
# resultados aritméticos). Sustituciones de mayor a menor longitud.
_MATH_SUBST = (
    ("dividido entre", "/"),
    ("dividido por", "/"),
    ("dividido", "/"),
    ("multiplicado por", "*"),
    ("resultado de", ""),
    ("elevado a la", "**"),
    ("elevado al", "**"),
    ("elevado a", "**"),
    ("por ciento de", "% de"),
    ("porciento de", "% de"),
    ("multiplicado", "*"),
    ("multiplicar", "*"),
    ("más", "+"),
    ("mas", "+"),
    ("menos", "-"),
    ("resta", "-"),
    ("suma", "+"),
    ("entre", "/"),
    ("por", "*"),
)
_MATH_PREFIX_RE = re.compile(
    r"(?i)^(?:cu[aá]nto(?:s)?(?: es| ser[aá]a| da| dan| son| sale| hace| vale)?"
    r"|calcula(?:r|me)?|resuelve|resolver|dime|suma|resta|multiplica|divide"
    r"|qu[eé] es|cu[aá]l es|cu[aá]l|resultado de)"
    r"\s*(?:el|la|los|las)?\s*"
)

# ---------------------------------------------------------------------------
# Integraciones locales (sin internet): clima, temporizador/alarma, volumen del
# sistema y portapapeles. Se ejecutan EN LA APP (no las "simula" el modelo) y
# el resultado se inyecta al turno como nota de dato confirmado; si el usuario
# pide una integración que la app no reconoce, la orden sigue su curso normal.
# ---------------------------------------------------------------------------

# Clima: consulta directa a wttr.in (gratis, sin clave) antes de la búsqueda
# web genérica cuando la consulta habla del tiempo. wttr.in usa tu IP si no se
# da ciudad.
#
# "qué tiempo" va seguido de un verbo de clima (hace/está/…) porque "¿qué tiempo
# tengo para acabar esto?" NO es una pregunta del clima y dispararía una
# búsqueda inútil y una latencia de ~1 s en cada una.
_WEATHER_RE = re.compile(
    r"(?i)\b(?:clima|lluvia|llueve|llovizna|temperatura|grados|celsius|"
    r"fahrenheit|pron[oó]stico|precipitaciones|nieva|nevar|"
    r"hace\s+(?:calor|fr[íi]o)|hace\s+mucho\s+(?:calor|fr[íi]o)|"
    r"va\s+a\s+(?:llover|nevar|llueve)|"
    r"qu[eé]\s+tiempo\s+(?:hace|hay|est[áa]|va|ser[áa])|"
    r"el\s+tiempo\s+en|tiempo\s+est[áa]\s+de)\b"
)
_LOCATION_RE = re.compile(
    r"(?i)\ben\s+([a-záéíóúñü]{2,20}(?:\s+[a-záéíóúñü]{2,20}){0,2})"
)
# Ciudad de referencia para el clima cuando la orden no la trae ("/clima",
# "dame el clima"). Vacía = la que resuelva wttr.in por IP, que con VPN o
# proxy puede dar una ciudad equivocada; se fija con MINDVOICE_WEATHER_CITY.
WEATHER_CITY = (os.environ.get("MINDVOICE_WEATHER_CITY") or "").strip()

# Temporizadores y alarmas.
_TIMER_RE = re.compile(
    r"(?i)\b(?:temporizador|alarma|timer|av[íi]same|avisame|recu[eé]rdame|"
    r"recordame)\b[^0-9]{0,30}?(\d{1,4})\s*"
    r"(minutos?|min|segundos?|seg|horas?|hrs?)"
)
_TIMER_AT_RE = re.compile(
    r"(?i)\b(?:alarma|av[íi]same|avisame|temporizador|suena|recu[eé]rdame)\b"
    r"[^0-9]{0,30}?(\d{1,2}):(\d{2})"
)
_TIMER_LIST_RE = re.compile(
    r"(?i)(?:qu[eé]\s+(?:temporizadores|alarmas)|lista\s+(?:de\s+)?"
    r"(?:temporizadores|alarmas)|temporizadores\s+activos|alarmas\s+activas)"
)
# Parsing temporal ampliado (E2): "en 10 minutos", "dentro de 2 horas", una
# alarma a la hora sin palabra clave, y locuciones coloquiales.
_TIMER_IN_RE = re.compile(
    r"(?i)\b(?:en|dentro\s+de|dentro)\s+(\d{1,4})\s*"
    r"(minutos?|min|segundos?|seg|horas?|hrs?)\b"
)
_TIMER_AT_BARE_RE = re.compile(
    r"(?i)\b(?:pon|ponme|activa|act[ií]vame|programa|agenda|despi[eé]rtame|"
    r"despertador|suelta)\b[^0-9]{0,20}?(?:a\s+las|para\s+las)\s+"
    r"(\d{1,2}):(\d{2})\b"
)
_TIMER_HALF_RE = re.compile(r"(?i)\b(?:en|dentro\s+de)\s+(?:una\s+)?media\s+hora\b")
_TIMER_HOURHALF_RE = re.compile(
    r"(?i)\b(?:en|dentro\s+de)\s+(?:una\s+)?hora\s+y\s+media\b"
)
_TIMER_QUARTER_RE = re.compile(
    r"(?i)\b(?:en|dentro\s+de)\s+un\s+cuarto\s+de\s+hora\b"
)
_TIMER_COUPLE_RE = re.compile(r"(?i)\b(?:en|dentro\s+de)\s+un\s+par\s+de\s+horas\b")
_TIMER_ONE_UNIT_RE = re.compile(
    r"(?i)\b(?:en|dentro\s+de)\s+una\s+(hora|minuto)\b"
)
# Cancelar temporizadores/alarmas activos.
_TIMER_CANCEL_RE = re.compile(
    r"(?i)\b(?:cancela|cancelar|cancel[oó]|quita|quitar|retira|retirar|"
    r"elimina|eliminar|borra|detenga|detener|suelta)\s+"
    r"(?:todos?\s+(?:los|las)\s+|todas\s+(?:las\s+)?|"
    r"el\s+|la\s+|los\s+|las\s+|mis\s+|al\s+)?"
    r"(?:temporizador(?:es)?|alarma(?:s)?|timers?)\b"
)

# Volumen del sistema (winmm). Siempre requiere la palabra "volumen" (o
# "silencio") para no confundir "sube el brillo" con "sube" el volumen.
_VOL_PCT_RE = re.compile(r"(?i)\bvolumen\s+al\s+(\d{1,3})\s*%")
_VOL_MUTE_RE = re.compile(r"(?i)\b(?:silencio|silenciar|silencia|mute)\b")
_VOL_UP_RE = re.compile(r"(?i)\b(?:sube|sub[íi]|aumenta|aumentar|subir)\b")
_VOL_DOWN_RE = re.compile(r"(?i)\b(?:baja|baj[áa]|bajar|disminuye|disminuir)\b")

# Portapapeles (Clipboard Unicode).
_CLIP_READ_RE = re.compile(
    r"(?i)(?:portapapeles|clipboard|qu[eé]\s+hay\s+(?:copiado|pegado)|"
    r"tengo\s+(?:copiado|pegado)|contenido\s+copiado|qu[eé]\s+tengo\s+copiado)"
)
_CLIP_COPY_RE = re.compile(r"(?i)\b(?:copia|copiame)\s+(.+?)\s*$")
_CLIP_MEH_FIRST = ("de", "la", "el", "los", "las", "un", "una", "mi", "tu")

# --- Órdenes sobre la propia memoria (Fase 2) -------------------------------
# Tres órdenes que antes no existían porque una lista plana no podía
# contestarlas: olvidar un tema (no todo), decir qué recuerda sobre algo, y de
# dónde sale un dato. El orden dentro de cada una es importante: primero la
# forma larga y después la corta, y "todo" queda EXCLUIDO a propósito porque eso
# lo lleva el reset de toda la vida.
_PROCEDENCIA_RE = re.compile(
    r"(?i)\b(?:por\s+qu[eé]\s+(?:me\s+)?(?:dices|dijiste|sabes|crees)|"
    r"de\s+d[oó]nde\s+(?:sabes|saca|lo\s+sacas)|seg[uú]n\s+qu[eé]|"
    r"en\s+qu[eé]\s+te\s+bases)\s*(?:tienes\s+|esa\s+|esto\s+|lo\s+que\s+me\s+dijiste\s+de\s+)?(.+)?$"
)
_RECUERDA_RE = re.compile(
    r"(?i)\b(?:qu[eé]\s+(?:recuerdas|sabes|te\s+acuerdas)|de\s+qu[eé]\s+sabes|"
    r"te\s+acuerdas\s+de)\s*(?:de|sobre|acerca\s+de)?\s*(.+)?$"
)
# ``(?!todo\b)`` es lo que separa este forget del de toda la vida: "olvida todo"
# tiene que seguir llegando al reset, no a interpretarse como borrar lo que
# cuelgue de la palabra "todo".
_FORGET_TOPIC_RE = re.compile(
    r"(?i)\b(?:olvida|olv[íi]date\s+de|borra|elimina|descarta)\s+"
    r"(?!todo\b)(?:lo\s+(?:de|sobre)|el\s+(?:tema|asunto)|mi\s+|tus\s+|"
    r"todo\s+lo\s+(?:de|sobre)\s+|los\s+recuerdos\s+(?:de|sobre)\s+)?(.+?)\s*$"
)


def _habla_de(texto: str, tema: str) -> bool:
    """¿Este texto habla de este tema?

    Solapamiento de palabras de contenido, no la cadena entera: "olvida lo de
    Python" tiene que tocar los recuerdos que mencionan Python aunque la frase
    no sea idéntica. Se apoya en el tokenizador de ``memory.base``, que ya
    quita acentos y palabras vacías, así que el criterio es el mismo que usa el
    grafo para puntuar.
    """
    try:
        from memory.base import normalizar, palabras
    except Exception:  # noqa: BLE001 - sin memoria, no se filtra nada
        return False
    terminos = palabras(tema, min_len=3)
    if not terminos:
        return False
    return bool(terminos & palabras(texto, min_len=3))


# Umbral RMS mínimo (sobre muestras PCM 16 bits) para no reenviar silencio
# ambiente: los modelos *-live-preview agotan cuota con reenvíos continuos.
# El gate real es adaptativo: se persigue el suelo de ruido del micrófono y se
# transmite con RMS >= max(2.0 * suelo, mínimo). El suelo está acotado para que
# el primer fragmento no lo dispare: si el usuario ya está hablando cuando se
# abre el micrófono, el suelo mínimo de la pre-escucha nunca supera
# _VOICE_GATE_FLOOR_MAX, así el gate no se ancla en la voz y no se pierden los
# primeros fragmentos de cada frase.
_VOICE_GATE_MIN = 15.0
_VOICE_GATE_FLOOR_MAX = 80.0
_VOICE_GATE_PREROLL = 3

# Watchdog de sesión: si hay un turno en curso (el modelo hablando o esperando
# respuesta a una orden/voz) y no llega NINGÚN dato del servidor durante
# ``response_stall_timeout`` segundos, la sesión se corta y se reconecta sola.
# Sin esto, una respuesta congelada dejaba al asistente mudo para siempre.
_WATCHDOG_INTERVAL = 1.0
# Margen para que una respuesta sea considerada en marcha. 30 s permite a las
# respuestas largas empezar/terminar de generar con calma (pensadas del modelo,
# carga del servidor) sin que el watchdog las corte a mitad: 15 s mataba turnos
# legítimos justo al final de frases largas (corte de audio). Si una sesión se
# quedara de verdad congelada, 30 s es aún una recuperación rápida.
_RESPONSE_STALL_TIMEOUT = 30.0
# Tope ABSOLUTO de una sesión de voz (PTT mantenido o micrófono activado) sin
# que se cierre un solo turno. Mientras hay voz, el watchdog general se apaga a
# propósito (el servidor no envía datos mientras escucha), y así un PTT que se
# queda "pegado" o un micrófono abierto que nadie cierra dejaban el micrófono
# subiendo audio para siempre (el 21/09 se midieron 440 MB enviados y 0
# recibidos) sin que NADA recycling la sesión: el asistente ya no volvía a
# hablar y el HUD se quedaba en "escuchando". Cinco minutos de voz sin un solo
# turno cerrado no es uso normal, así que se fuerza el cierre.
_VOICE_MAX_OPEN_S = 300.0

# Margen para considerar que "el audio sigue saliendo". Con VAD manual la base
# del tope de arriba se refresca mientras se habla, porque hablar es una
# actividad de duración indefinida por diseño; pero si esa mano se extiende a
# un micrófono abierto y MUDO (el usuario se fue con el turno a medias) el
# watchdog queda anulado justo en el atasco que debe atrapar, y el micro sigue
# subiendo audio hasta que el servidor responde 1011 "Resource has been
# exhausted". Solo se refresca si en los últimos 2 s pasó audio de verdad.
_VOICE_FLOW_GRACE_S = 2.0

# Margen de gracia tras el último dato de contenido de la respuesta (audio,
# texto o transcripción de salida). Si el servidor NO envía ``turn_complete`` a
# pesar de que el modelo ya terminó de hablar (turno RECORTADO por el tope de
# audio del servidor, o sesión a punto de caerse), el watchdog cierra el turno
# por su cuenta a los ``_OUTDONE_GRACE_S`` segundos de silencio: entrega el texto
# parcial al cliente y decide si hay que pedir continuación. Sin esto, la frase
# quedaba "colgada" y el texto acumulado se descartaba al esperar los 30 s del
# watchdog general.
#
# Este margen se consumía en CASI TODOS los turnos (el servidor rara vez manda
# ``turn_complete`` con este modelo), así que era tiempo muerto que se le sumaba
# a cada respuesta: con 6 s eran ~4 s por turno que el usuario perceive como
# "tarda mucho". El audio ya está escrito en el reproductor y la transcripción
# llega entremezclada con los trozos de voz, así que 2 s de silencio total del
# servidor es más que suficiente para saber que terminó, y sin truncar nada.
_OUTDONE_GRACE_S = 2.0

# Longitud máxima de la descripción inyectada al turno de texto. Con 600
# caracteres se cortaba de golpe el detalle que la captura de alta resolución
# sí consigue (la transcripción del texto de la pantalla). 1400 caben de sobra
# en el presupuesto de contexto y siguen siendo pocos tokens frente al audio.
_SCREEN_NOTE_MAX_CHARS = 1400
# Tope de espera de la descripción dentro del turno. Con el precalentado al
# enfocar la barra (ver ``prefetch_description``) lo normal es que la nota ya
# esté lista, así que este tope solo actúa como red de seguridad del caso peor.
# Medido el 25/09: la descripción tarda 2,6-3,3 s de verdad, y con el tope de
# 3,0 s se cancelaba justo la mitad de las veces (el turno se quedaba sin
# contexto visual sin avisar). 6,0 s deja margen sin alargar el turno.
_SCREEN_DESC_TIMEOUT = 6.0
# Frescura máxima (s) de la caché de descripción reutilizable: preguntas
# seguidas ("¿y ahora qué ves?") reutilizan la última descripción si es reciente.
_DESC_CACHE_TTL = 8.0
# Tope de espera de la nota de pantalla al CERRAR un turno de voz. La nota se
# inyecta con el turno de audio aún abierto (``turn_complete=False``), así que
# si la visión no llega a tiempo el audio se cierra igualmente: la voz nunca
# queda bloqueada esperando la pantalla.
_SCREEN_NOTE_TIMEOUT = 2.5

# Memoria breve de la app: las últimas interacciones se reinyectan al abrir
# cada sesión. Sin esto, cada reconexión (rotación del servidor en v1alpha,
# ~cada 2,5 min) borraba todo el contexto y el modelo "olvidaba" la
# conversación. Se persiste en ``_MEMORY_FILE`` (directorio de datos del
# usuario) para que sobreviva al cierre de la app y para que siga siendo
# escribible cuando la app está instalada en "Program Files"; el usuario puede
# borrarla con un comando de reset (``_MEMORY_RESET_TRIGGERS``).
_MEMORY_MAX = 24
_MEMORY_MAX_CHARS = 400
# Presupuesto total de caracteres para el bloque "[Breve]" inyectado al system
# prompt (E8): el número de entradas está limitado por ``_MEMORY_MAX``, pero
# también conviene acotar el bloque para no restarle presupuesto de contexto al
# turno en curso (cada entrada se trocea a 400 caracteres como máximo).
_MEMORY_BUDGET = 6000
_MEMORY_FILE = data_file("memory.json")
_MEMORY_RESET_TRIGGERS = (
    "olvídate de todo",
    "olvidate de todo",
    "olvida el contexto",
    "olvida todo",
    "borra la memoria",
    "borra memoria",
    "reset de memoria",
    "reinicia la memoria",
)

# Memoria a largo plazo (E3): cuando la memoria breve se llena, lo que "sale por
# arriba" se ARCHIVA como un resumen conciso en ``_PERMANENT_FILE`` en vez de
# perderse (preferiblemente resumido con el mismo modelo REST barato que usa
# la búsqueda web, p. ej. flash-lite; si no responde, se comprime en local). Al
# construir el contexto de cada sesión esos resúmenes se reinyectan junto a la
# memoria breve, de modo que el usuario NO "olvida" a largo plazo lo que pidió
# dejar de usar en la conversación inmediata. Lo que escribes aquí se guarda
# SIEMPRE, incluso cuando pides un reset ("olvida todo").
_PERMANENT_FILE = data_file("memory-long.json")
_PERMANENT_MAX = 30
_PERMANENT_MAX_CHARS = 600
# Cada cuántos recuerdos nuevos se fuerza un resumen ANTICIPADO (aunque la breve
# aún no se haya llenado), para que la larga no se construya solo con lo que el
# desbordamiento desechó. El archivo periódico solo "sacrifica" tramos viejos
# cuando la breve tiene margen para no quedarse sin contexto reciente.
_PERMANENT_EVERY = 8
_PERMANENT_MIN_DEPTH = 10
_SUMMARY_TIMEOUT = 8.0
_SUMMARY_PROMPT = (
    "Resume en español, en hasta 4 frases concisas y separadas, los datos y "
    "preferencias personales que el usuario contó a MindVoice. NO resumas "
    "órdenes puntuales, búsquedas web ni respuestas impersonales; conserva "
    "nombre, temas que le interesan, proyectos, gustos, decisiones tomadas y "
    "cualquier información que pueda servir en conversaciones futuras. Usa "
    "tono factual y salta lo trivial. Conversación a resumir:\n"
)

# Temporizadores y alarmas (E2): la lista de pendientes se persiste en
# ``_TIMERS_FILE`` (directorio de datos del usuario, como memory.json) para que
# las alarmas "en marcha" sobrevivan al reinicio de la app. Se guarda la hora
# objetivo como época (``end_epoch``) y al restaurar se recalcula el tiempo
# restante.
_TIMERS_FILE = data_file("timers.json")
_TIMERS_MAX = 12

# Depuración de visión: cada vez que la app describe la pantalla vuelca la
# captura JPEG exacta y la nota inyectada a archivos para comparar "lo que
# vio". Solo si la variable de entorno MINDVOICE_DEBUG está activa; por
# defecto no se escribe nada en el sistema de archivos por cada orden.
_DEBUG_DIR = os.path.dirname(os.path.abspath(__file__))
_DEBUG_VISTA = os.path.join(_DEBUG_DIR, "debug_vista.jpg")
_DEBUG_NOTA = os.path.join(_DEBUG_DIR, "debug_nota.txt")
_DEBUG_ENABLED = (
    os.environ.get("MINDVOICE_DEBUG", "").strip().lower()
    in ("1", "true", "yes", "on")
)

# Búsqueda en internet: el modelo *-live-preview no navega, así que la app
# busca fuera (grounding de Google Search, ver ``_web_search``) e inyecta el
# resumen como nota al turno, igual que la descripción de pantalla. Se activa
# por tres vías, y NUNCA en comandos que no la requieren:
#
#   1. Frases explícitas o el prefijo "/web" (camino rápido, sin llamada extra).
#   2. El detector inteligente por IA (``_classify_web_query``, opcional con
#      ``Settings.web_smart_detect``) para órdenes naturales que piden datos
#      actuales sin usar ninguna frase de la lista.
#   3. Para texto, la nota viaja DENTRO del mismo turno (una única respuesta
#      informada). Para voz, la transcripción llega con el turno de audio ya
#      cerrado: ver ``_launch_voice_web`` (interrumpe la respuesta "vacía" y
#      reenvía el turno con la nota, para que el usuario oiga UNA sola respuesta
#      ya con los datos, y ésta también salga hablada).
_WEB_SEARCH_TIMEOUT = 12.0
# Presupuesto global de UNA búsqueda, por encima de los topes de cada pieza
# (proveedor + modelo + respaldo). Garantiza que ninguna orden se quede
# esperando: la nota web es una mejora, nunca un bloqueo.
_WEB_TOTAL_TIMEOUT = 9.0
# El detector inteligente es una llamada EXTRA antes de poder enviar la orden,
# así que su tope va ceñido: 6 segundos eran casi media respuesta y, además, se
# colgaba con los 429/503 del plan. Con 2,5 s o decide o se renuncia (la orden
# sale igual, sin nota) en vez de alargar la espera del usuario.
_WEB_CLASSIFY_TIMEOUT = 2.5
# Tope de la "prueba" de un modelo REST candidato durante la resolución
# automática (ver ``_web_model_name``).
_WEB_MODEL_PROBE_TIMEOUT = 5.0
# Longitud máxima de una consulta propuesta por el detector inteligente (si el
# modelo devuelve un párrafo, se descarta: no es una consulta de búsqueda).
_WEB_MAX_QUERY_LEN = 150
_WEB_TRIGGERS = (
    "/web ",
    "busca en internet",
    "búscalo en internet",    "buscalo en internet",
    "busca en la web",
    "búscalo en la web",
    "buscalo en la web",
    "busca en google",
    "búscalo en google",
    "buscalo en google",
    "googlea",
    "googlealo",
    "investiga",
    "investigalo",
    "qué ha pasado",
    "que ha pasado",
    "noticias de hoy",
    "últimas noticias",
    "ultimas noticias",
    "última hora",
    "ultima hora",
    "lo último",
    "lo ultimo",
    "resultado de hoy",
    "resultado del partido",
    "resultado de la jornada",
    "marcador del",
    "quién ganó",
    "quien gano",
    "quien va ganando",
    "cuánto vale",
    "cuanto vale",
    "cuánto cuesta",
    "cuanto cuesta",
    "cuánto está",
    "cuanto esta",
    "a cómo está",
    "a cuanto está",
    "precio de",
    "precios de",
    "valor de",
    "cotización de",
    "cotizacion de",
    "pronóstico del tiempo",
    "pronostico del tiempo",
    "el tiempo en",
    "el tiempo de hoy",
    "el clima de",
    "qué pasa en el mundo",
    "que pasa en el mundo",
    "en internet",
    "en la web",
    "averigua",
    "averigua algo",
    "averigualo",
    "averígualo",
    "averíguame",
    "averiguame",
    "consulta en internet",
    "consúltalo en internet",
    "consultalo en internet",
    "mira en internet",
    "míralo en internet",
    "miralo en internet",
    "échatelo en google",
    "lo más reciente",
    "lo mas reciente",
    "qué dicen de",
    "que dicen de",
    "qué se sabe de",
    "que se sabe de",
    "dime las últimas",
    "entérate de",
    "enterate de",
    "esta al día",
    "est� al d�a",
    # Formas NATURALES que antes no disparaban nada. El detector exigía frases
    # exactas ("busca en internet"), así que un "busca el precio del bitcoin"
    # normal o un "búsqueda web sobre X" se quedaban sin buscar en silencio, y
    # el usuario veía al modelo responder de memoria sin saber que no hubo
    # búsqueda. Van al FINAL a propósito: al final del bucle gana el primer
    # disparador que produzca consulta, y estos son los genéricos.
    "busca ",
    "buscame ",
    "búscame ",
    "busqueda web",
    "búsqueda web",
    "busquedas",
    "búsquedas",
    "hazme una busqueda",
    "haz una busqueda",
)
# Un disparador solo cuenta si aparece como PALABRA COMPLETA. Sin esto, buscar
# la subcadena "investiga" la encuentra dentro de "investigación" y la consulta
# web se queda cortada a media palabra ("ción con integración de código…").
# ``(?<!\w)``/``(?!\w)`` también impiden que "busca" case dentro de "buscame".
_WEB_TRIGGER_RE_CACHE: dict = {}

# Los nombres de motor ya NO viven aquí: salen de ``config.WEB_ENGINES``, que es
# el registro único. Si añades un motor, se añade ahí y en ningún otro sitio.
_ENGINE_LABELS = {
    k: cfg["note"] for k, cfg in WEB_ENGINES.items()
}
_ENGINE_SPOKEN = {
    k: cfg["spoken"] for k, cfg in WEB_ENGINES.items()
}

def _WEB_TRIGGER_RE(trigger: str):
    """Regex con límites de palabra para un disparador (con caché).

    El límite solo se pone en el lado que toca: si el disparador acaba en
    espacio ("buscame "), un ``(?!\\w)`` detrás miraría la letra siguiente y
    haría fallar SIEMPRE el casteo. Igual en el lado izquierdo con los que
    empiezan por espacio (" en internet").
    """
    rx = _WEB_TRIGGER_RE_CACHE.get(trigger)
    if rx is None:
        left = r"(?<!\w)" if trigger[:1].isalnum() or trigger[:1] == "_" else ""
        right = r"(?!\w)" if trigger[-1:].isalnum() or trigger[-1:] == "_" else ""
        rx = re.compile(left + re.escape(trigger) + right)
        _WEB_TRIGGER_RE_CACHE[trigger] = rx
    return rx

# Palabras/conectores introductorios que se descartan al construir la consulta
# extraída después de un disparador ("dime cuánto vale el café" → "café").
_WEB_BIGRAM_FILLERS = frozenset((
    "por favor", "puedes buscar", "podrías buscar", "podrias buscar",
    "necesito saber", "quiero saber", "acerca de", "respecto a",
    "que es", "qué es", "que fue", "qué fue", "cuál es", "cual es",
    "cuál fue", "cual fue",
))
_WEB_UNIGRAM_FILLERS = frozenset((
    "dime", "cuéntame", "cuentame", "puedes", "podrías", "podrias",
    "necesito", "quiero", "busca", "buscar",
    "búsqueda", "busqueda", "googlea", "investiga", "sobre", "acerca",
    "que", "qué", "cual", "cuál",
))
# Prompt del detector inteligente de intención web (fase de clasificación
# rápida, sin grounding: solo decide SI hace falta buscar y con qué consulta).
_WEB_CLASSIFY_PROMPT = (
    'Usuario: "{text}"\n'
    "¿Necesita esta petición información ACTUAL o RECIENTE de internet que un "
    "modelo sin acceso a la red no respondería con fiabilidad (noticias, "
    "precios, resultados deportivos, clima, eventos, estrenos, novedades, "
    "política, cifras del momento…)?\n"
    "Si NO la necesita (pregunta general, de código, de razonamiento, de la "
    "pantalla del usuario o de conocimiento fijo), responde EXACTAMENTE: NO\n"
    "Si SÍ la necesita, responde SOLO con la consulta de búsqueda, corta y en "
    "el idioma del usuario, sin comillas ni explicaciones.\n"
    "Respuesta:"
)

# Nombres de reserva (orden de preferencia) para la búsqueda web y el detector
# inteligente, por si ``models.list`` no ayuda. OJO en 2026: los ``-2.5-flash``
# y ``-3.1-flash`` (sin sufijo) dan 404 ("no longer available to new users");
# la resolución automática (``_web_model_name``) recorre los candidatos hasta
# encontrar el primero que de verdad responde texto. OJO: los ``*-preview`` de la
# serie 3.1 están APAGADOS (dejaron de servirse en 2026) y devolvían 404/503
# una y otra vez, así que fuera.
#
# ORDEN POR CALIDAD, NO POR DISPONIBILIDAD DE UNA KEY. Antes esta lista ponía
# ``-lite`` PRIMERO porque con la clave del autor los ``3.5-3.8-flash`` devolvían
# 429; consecuencia: un clon limpio (o el mismo, con otra cuota) arrancaba en el
# modelo más barato y respondía peor, sin ningún aviso. Ahora el pin documentado
# va primero y la red barata queda AL FINAL, solo si nada más sirve.
_WEB_MODEL_FALLBACKS = (
    WEB_SEARCH_MODEL,
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-flash-latest",
    # Red de seguridad barata: solo si el plan no sirve ningún flash GA.
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
)
# Presupuesto total para resolver el modelo REST (probes en serie). La
# resolución corre en segundo plano al arrancar (pre-warm), pero el límite
# evita que una primera búsqueda en frío se coma todo el turno.
_WEB_MODEL_RESOLVE_DEADLINE = 12.0
_WEB_MODEL_MAX_PROBES = 6
# Caché EN DISCO del modelo resuelto. Antes solo vivía en memoria, y como el
# watchdog recrea el LiveAssistant en cada reinicio (el 25/09 fueron ~170), la
# resolución se repetía constantemente. Un archivo en disco la resuelve una sola
# vez por semana.
#
# Va en el directorio de datos del usuario (``rutas.data_file``) y NO junto al
# código: instalada en "C:\Program Files" la carpeta de la app no se puede
# escribir, el guardado fallaría con PermissionError y el ``except`` de abajo se
# lo tragaría en silencio, así que cada arranque repetiría la resolución completa.
# Es el mismo motivo por el que ``user_prefs.json`` vive fuera del código.
_WEB_MODEL_CACHE_FILE = data_file("web_model_cache.json")
_WEB_MODEL_CACHE_TTL = 7 * 24 * 3600.0

# Proveedores externos de resultados web (cuando el grounding de Google Search
# no tiene cuota en el plan de la API key). Cada uno devuelve resultados como
# texto plano que el modelo resume. Tiene que caber dentro del presupuesto del
# turno: 10 s como máximo, con 6 resultados como mucho.
_WEB_PROVIDER_TIMEOUT = 10.0
_WEB_PROVIDER_MAX_RESULTS = 6

# Throttle/caché de las búsquedas web (E6). Cada búsqueda de internet cuesta
# cuota (grounding de Google) o cuota de proveedor (tavily/serpapi/brave), así
# que: la misma consulta pedida dos veces en ``_WEB_CACHE_TTL`` se sirve de la
# caché sin llamar a ningún proveedor, y dos búsquedas DISTINTAS no pueden
# pisarse antes de ``_WEB_MIN_INTERVAL`` (la segunda dentro de la ventana se
# descarta con aviso en lugar de agotar cuota de golpe).
_WEB_CACHE_TTL = 60.0
_WEB_MIN_INTERVAL = 15.0

# DuckDuckGo (endpoint HTML) es el proveedor gratuito y sin clave: funciona
# también cuando terceros bloquean el país de salida (p. ej. Tavily/SerpAPI
# fuera de Venezuela dan 403). Se parsea el HTML con la stdlib (sin
# dependencias extra); el enlace real va codificado en el parámetro ``uddg=``.
_DDG_HTML_URL = "https://html.duckduckgo.com/html/"
# Versión "lite" de DuckDuckGo: HTML distinto (tabla de resultados) y suele no
# estar limitada cuando el endpoint clásico devuelve la página de verificación
# humana. Se usa como segundo intento automático (ver ``_duckduckgo_search``).
_DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
_DDG_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# DuckDuckGo no bloquea con 403: responde **HTTP 202** con la página
# "bots use duckduckgo / challenge". El 202 es 2xx, así que
# ``raise_for_status()`` NO lo detecta y la página de aviso pasaba por HTML
# normal: el parser sacaba cero resultados y el HUD lo SST mostraba como un
# "fallo de IP" que en realidad era un límite de peticiones.
_DDG_BLOCK_TOKENS = (
    "anomalous",
    "anomaly",
    "captcha",
    "challenge",
    "bots use duckduckgo",
    "unusual traffic",
)
_DDG_BLOCK_STATUS = 202
# Estado COMPARTIDO (a nivel de módulo, no de instancia) del bloqueo por IP de
# DuckDuckGo. Se recuerda entre reinicios del watchdog porque, de nuevo, con
# estado por instancia el mismo aviso saltaba una y otra vez.
#
# La espera ya NO es una espera plana de 30 min: el bloqueo de DDG se levanta solo
# en ~1-2 min, así que una espera fija tan larga mataba la búsqueda web media
# hora de cada vez que una petición tocaba el límite. Ahora es una espera
# CRECIENTE por golpes (30 s, 60 s, 120 s... hasta 5 min) que se reinicia en
# cuanto una búsqueda vuelve a devolver resultados.
_DDG_BLOCKED_UNTIL = 0.0
_DDG_BLOCK_STRIKES = 0
_DDG_BLOCK_BACKOFF = (30.0, 60.0, 120.0, 240.0, 300.0)
_DDG_BLOCK_WARNED = False
# Separación mínima entre peticiones a DDG. Es la causa real del bloqueo: DDG
# tolera pocas peticiones seguidas y con ráfaga marca la IP al instante (medido:
# 5 GET seguidos con 1,5 s de gap -> los 5 con HTTP 202 y challenge). Aquí se
# espacian de verdad y además se serializan con un lock, para que dos turnos
# simultáneos no se pisen.
_DDG_MIN_INTERVAL = 9.0
_DDG_LAST_REQ_TS = 0.0
_DDG_VQD = ""
_DDG_LOCK = asyncio.Lock()
# Tope del GET a DuckDuckGo: es el proveedor gratuito "de emergency", así que
# se le da menos margen que a una API con clave (antes compartía los 10 s de
# ``_WEB_PROVIDER_TIMEOUT`` y una búsqueda muerta alargaba el turno).
_DDG_TIMEOUT = 6.0


# Palabras sin contenido de búsqueda usadas para descartar cláusulas-muletilla
# ("si no sabes", "si puedes", "por favor"...) al extraer la consulta.
_WEB_JUNK_WORDS = frozenset((
    "si", "no", "sabes", "sabe", "sé", "se", "puedo", "puedes", "pueda",
    "puede", "podría", "podria", "podrías", "podrias", "quiero", "quieres",
    "quieras", "necesito", "por", "favor", "acaso", "bueno", "bien", "ok",
    "vale", "igual", "claro", "obvio", "más", "mas", "para", "cuando",
    "cómo", "como", "dime", "o", "a", "al", "de", "del", "el", "la", "lo",
    "los", "las", "un", "una", "unos", "unas", "en", "y", "que",
))


class _DdgResult(NamedTuple):
    title: str
    url: str
    snippet: str


class _DdgParser(HTMLParser):
    """Extrae título, URL y resumen de los resultados de DuckDuckGo HTML.

    La página de resultados usa ``<a class="result__a">Título</a>`` (con el
    enlace real dentro del redirect ``//duckduckgo.com/l/?uddg=<url>``) y
    ``<a class="result__snippet">…</a>`` para el resumen. Sin dependencias.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[_DdgResult] = []
        self._url: str = ""
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._in_title = False
        self._in_snippet = False

    @staticmethod
    def _real_url(href: str) -> str:
        """El href es ``//duckduckgo.com/l/?uddg=<real>&rut=…``: extrae ``uddg``."""
        if not href:
            return ""
        if href.startswith("//"):
            href = "https:" + href
        parsed = urlparse(href)
        uddg = (parse_qs(parsed.query).get("uddg") or [""])[0]
        return unquote(uddg) if uddg else href

    def _finalize(self) -> None:
        if not self._url and not self._title_parts:
            return
        title = " ".join(" ".join(self._title_parts).split())
        snippet = " ".join(" ".join(self._snippet_parts).split())
        if title:
            self.results.append(
                _DdgResult(title=title, url=self._url, snippet=snippet)
            )
        self._url = ""
        self._title_parts = []
        self._snippet_parts = []
        self._in_title = False
        self._in_snippet = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "a":
            return
        classes = (dict(attrs).get("class") or "").split()
        if "result__a" in classes:
            self._finalize()  # resultado anterior sin snippet pendiente
            self._url = self._real_url(dict(attrs).get("href") or "")
            self._in_title = True
        elif "result__snippet" in classes:
            self._snippet_parts = []
            self._in_snippet = True

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        elif self._in_snippet:
            self._snippet_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a":
            return
        if self._in_title:
            self._in_title = False
        elif self._in_snippet:
            self._in_snippet = False
            self._finalize()


class _DdgLiteParser(HTMLParser):
    """Parser tolerante para la versión 'lite' de DuckDuckGo (sin dependencias).

    La página lite es una tabla por filas: cada resultado es un enlace directo
    (``<a href="http…">Título</a>``) seguido poco después de una celda de
    resumen (``<td class*="snippet">…``). Se recogen todos los enlaces directos
    y la primera celda de resumen posterior se asigna al último resultado.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[_DdgResult] = []
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._url: str = ""
        self._in_title = False
        self._in_snippet = False

    def _pop_title(self) -> None:
        if not self._in_title:
            return
        title = " ".join(" ".join(self._title_parts).split())
        self._title_parts = []
        self._in_title = False
        if title:
            self.results.append(_DdgResult(title=title, url=self._url, snippet=""))

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs = dict(attrs)
        if tag == "a":
            href = attrs.get("href") or ""
            # Solo enlaces reales de resultados: directos http(s), no la navegación
            # interna ni el redirect ``//duckduckgo.com/l/?uddg=``.
            if href.startswith("http") and "duckduckgo.com" not in href:
                self._pop_title()
                self._in_title = True
                self._url = href
        elif tag == "td":
            classes = (attrs.get("class") or "").split()
            if any("snippet" in c for c in classes):
                self._in_snippet = True
                self._snippet_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        elif self._in_snippet:
            self._snippet_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._pop_title()
        elif tag == "td" and self._in_snippet:
            self._in_snippet = False
            text = " ".join(" ".join(self._snippet_parts).split())
            if text and self.results:
                prev = self.results[-1]
                self.results[-1] = _DdgResult(
                    title=prev.title, url=prev.url, snippet=text
                )

# Idiomas para la transcripción de entrada (tu voz) y de salida (lo que dice
# el modelo). Se configuran desde los Ajustes del overlay (``transcript_lang``)
# y se leen en ``_transcript_langs()`` en cada nueva sesión.

# Errores de la API que no se resuelven reintentando.
# OJO: ``RESOURCE_EXHAUSTED`` (429, cuota agotada) NO está aquí a propósito. Con
# él dentro, un 429mataba el motor para siempre (``run()`` hacía ``return``) y,
# como el watchdog del overlay deja de reiniciar tras varias caídas seguidas, la
# app quedaba MUERTA hasta que la reiniciaras a mano. La cuota es un fallo
# TRANSITORIO: se reintenta tras un enfriamiento largo. Ver ``_is_quota_error``.
_FATAL_ERROR_TOKENS = (
    "UNAUTHENTICATED",
    "PERMISSION_DENIED",
    "INVALID_ARGUMENT",
    "API_KEY_INVALID",
)

# Errores de cuota/límite de tasa (HTTP 429). El reintento con el retroceso
# normal (1 s, 2 s, 4 s...) solo lograría burningar cuota: se espera un
# enfriamiento fijo y largo antes de volver a intentarlo.
_QUOTA_ERROR_TOKENS = (
    "RESOURCE_EXHAUSTED",
    "429",
    "QUOTA_EXCEEDED",
    "RATE_LIMIT",
)
_QUOTA_COOLDOWN_S = 90.0


def _is_quota_error(exc: Exception) -> bool:
    """``True`` si el error es de cuota o límite de tasa (HTTP 429)."""
    message = str(exc).upper()
    return any(token in message for token in _QUOTA_ERROR_TOKENS)


def _is_fatal(exc: Exception) -> bool:
    """Devuelve ``True`` si el error no merece la pena reintentarlo."""
    message = str(exc).upper()
    return any(token in message for token in _FATAL_ERROR_TOKENS)


# Errores que indican un fallo TRANSITORIO de red (DNS caído, Wi-Fi sin salir,
# timeout...), NO que el modelo no exista. No deben desactivar la búsqueda web
# para toda la sesión: se reintentan en la siguiente búsqueda.
_NETWORK_ERROR_TOKENS = (
    "getaddrinfo",
    "gaierror",
    "name resolution",
    "name or service not known",
    "connectionattempted",
    "connectionreseterror",
    "connection refused",
    "connection aborted",
    "connection error",
    "timedout",
    "timed out",
    "readtimeout",
    "writetimedout",
    "network is unreachable",
    "no route to host",
    "temporary failure",
    "service unavailable",
)


def _is_network_error(exc: Exception) -> bool:
    """Devuelve ``True`` si el error parece de red (transitorio, reintentable)."""
    message = str(exc).lower()
    return any(token in message for token in _NETWORK_ERROR_TOKENS)


def _load_cached_web_model() -> Optional[str]:
    """Modelo web resuelto en una ejecución anterior, si sigue en vigor."""
    try:
        with open(_WEB_MODEL_CACHE_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        name = str((data or {}).get("model") or "").strip()
        ts = float((data or {}).get("ts") or 0.0)
    except Exception as exc:  # noqa: BLE001 - la caché es opcional
        logger.debug("No se pudo leer la caché del modelo web: %s", exc)
        return None
    if not name:
        return None
    if (time.time() - ts) > _WEB_MODEL_CACHE_TTL:
        logger.debug("Caché del modelo web caducada (%s).", name)
        return None
    return name


def _save_cached_web_model(name: str) -> None:
    """Guarda el modelo web resuelto para no repetir la resolución al arrancar."""
    if not name:
        return
    try:
        ensure_data_dir()
        temp = f"{_WEB_MODEL_CACHE_FILE}.tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump({"model": name, "ts": time.time()}, handle)
        os.replace(temp, _WEB_MODEL_CACHE_FILE)
    except Exception as exc:  # noqa: BLE001 - sin caché, se resuelve igual
        logger.debug("No se pudo guardar la caché del modelo web: %s", exc)


def _clear_cached_web_model() -> None:
    """Borra la caché del modelo web (quedó inservible: p. ej. sin cuota)."""
    try:
        os.remove(_WEB_MODEL_CACHE_FILE)
    except FileNotFoundError:
        pass
    except OSError as exc:  # noqa: BLE001 - la caché es opcional
        logger.debug("No se pudo borrar la caché del modelo web: %s", exc)


class _SessionStalled(RuntimeError):
    """Un turno quedó congelado sin datos del servidor: la sesión debe reciclarse."""


def _live_message_is_empty(message) -> bool:
    """``True`` si el mensaje del servidor no trae absolutamente nada.

    ``session.receive()`` entrega un ``LiveServerMessage`` vacío cuando el
    websocket ya está cerrado (el SDK no distingue "cerrado" de "sin datos" y
    además INSISTIRÍA en el bucle ``while`` de ``_receive``). Detectarlo aquí
    evita un bucle al 100 % de CPU y permite cerrar la sesión con normalidad
    para que ``run()`` la reabra.
    """
    return (
        getattr(message, "server_content", None) is None
        and getattr(message, "data", None) is None
        and getattr(message, "text", None) is None
        and getattr(message, "go_away", None) is None
        and getattr(message, "usage_metadata", None) is None
        and getattr(message, "session_resumption_update", None) is None
    )


async def _live_messages(session):
    """Itera TODOS los mensajes de la sesión, no solo los del primer turno.

    ``AsyncSession.receive()`` es un generador asíncrono de UN SOLO turno: sale
    (``break``) en cuanto el servidor manda ``turn_complete`` o
    ``interaction_status=IDLE``. Recorrerlo una única vez con ``async for``
    dejaba el socket sin leer a partir del primer turno: el servidor seguía
    enviando audio, pero nadie lo consumía, así que desde el SEGUNDO turno el
    asistente se quedaba mudo (y el watchdog reiniciaba la sesión 30 s después
    con la respuesta perdida). Este envoltorio relanza el generador en cada
    ``turn_complete`` para que la sesión sirva para muchos turnos seguidos.

    Termina (sin relanzar) cuando el websocket se cierra o falla, que es justo
    lo que ``run()`` necesita para reconectar.
    """
    while True:
        try:
            async for message in session.receive():
                if _live_message_is_empty(message):
                    logger.info("El servidor cerró el flujo de la sesión Live.")
                    return
                yield message
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - cierre de socket: se reconecta
            logger.info("Flujo de la sesión Live cerrado por el servidor: %s", exc)
            return
        # El generador terminó tras un ``turn_complete``: la sesión sigue viva,
        # así que se relanza para atender el siguiente turno. Si no llegó a
        # entregar NADA (y no era un mensaje vacío), es que el flujo ya no da
        # más de sí: se sale en vez de quedarse girando.
        await asyncio.sleep(0.05)


class LiveAssistant:
    """Orquesta la sesión Live completa con reconexión automática."""

    def __init__(
        self,
        settings: Settings,
        hotkeys: HotkeyController,
        on_text: Optional[Callable[[str], None]] = None,
        on_user_text: Optional[Callable[[str], None]] = None,
        on_meta: Optional[Callable[[str], None]] = None,
        on_web: Optional[Callable[[str], None]] = None,
        on_turn_complete: Optional[Callable[[], None]] = None,
        on_interrupted: Optional[Callable[[], None]] = None,
        on_voice_level: Optional[Callable[[float], None]] = None,
        on_tokens: Optional[Callable[[int, int], None]] = None,
        on_state: Optional[Callable[[str], None]] = None,
        on_timers: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._settings = settings
        self._hotkeys = hotkeys
        self.on_text = on_text or (lambda text: print(text, flush=True))
        self.on_user_text = on_user_text or self.on_text
        self.on_meta = on_meta or (lambda text: print(f"[Sys] {text}", flush=True))
        self.on_web = on_web or self.on_meta
        self.on_voice_level = on_voice_level
        self.on_turn_complete = on_turn_complete or (
            lambda: print("— Fin de turno —", flush=True)
        )
        self.on_interrupted = on_interrupted or (lambda: None)
        self.on_tokens = on_tokens or (lambda prompt, response: None)
        self.on_state = on_state or (lambda state: None)
        self.on_timers = on_timers or (lambda summary: None)
        self._token_prompt = 0      # acumulado de la sesión (prompt)
        self._token_response = 0    # acumulado de la sesión (respuesta)
        self.quit_event = asyncio.Event()
        # Motivo de la última parada (solo diagnóstico; se escribe en el log
        # cuando ``run()`` termina).
        self._stop_reason = "sin_iniciar"
        self._command_queue: Optional[asyncio.Queue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Serializa las descripciones de pantalla: mss no es seguro entre hilos
        # y dos llamadas concurrentes (voz + texto) colgaban la captura.
        self._describe_lock = asyncio.Lock()
        # Caché de la última descripción de pantalla (texto) y su edad, para no
        # repetir la mini-sesión de visión en preguntas seguidas.
        self._desc_cache: Optional[str] = None
        self._desc_cache_ts = 0.0
        # Descripción "calentada" en segundo plano: el HUD avisa al enfocar la
        # barra de órdenes para tenerla lista antes de que el usuario escriba,
        # que es cuando se paga el coste real de la mini-sesión de visión.
        self._desc_prefetch_task: Optional[asyncio.Task] = None
        self._screen_ref: Optional[ScreenCapture] = None
        # Memoria breve entre sesiones: [{role, text}]. Sobrevive a las
        # reconexiones automáticas (la sesión del servidor no se reanuda), se
        # reinyecta en el system prompt de cada nueva sesión y se persiste en
        # disco (memory.json) para que también sobreviva al cierre de la app.
        self._memory = self._load_memory()
        # Memoria a largo plazo (E3): resúmenes archivados; se cargan del disco
        # y los actualiza la tarea de archivo en segundo plano.
        self._permanent: list = self._load_permanent()
        self._memory_since_archive = 0
        self._archive_task: Optional[asyncio.Task] = None
        # Índice de recuperación en grafo (Fase 1). Es ADEMÁS de la memoria
        # plana, no la sustituye: ``self._memory`` sigue siendo la fuente de la
        # verdad y los dos JSON siguen escribiéndose igual. Si el índice no
        # existe (sin Graphify, o cualquier error al montarlo) todo lo de abajo
        # cae al bloque plano de siempre.
        self._memoria_indice = self._preparar_indice_memoria()
        # Memoria de trabajo (Fase 2): qué turnos sirvieron y cuáles no. Vive
        # junto al índice pero es independiente: si falla, el resto sigue.
        self._memoria_trabajo = self._preparar_memoria_trabajo()
        # Búsqueda web: la consulta vigente pedida POR VOZ. La transcripción
        # llega con el turno de audio cerrado, así que la nota no cabe dentro
        # del turno (como sí con las órdenes de texto): se interrumpe la
        # respuesta "vacía" que el modelo está a punto de dar y se reenvía el
        # turno con la nota (ver ``_launch_voice_web``).
        self._pending_web_query: Optional[str] = None
        # Tarea en vuelo que resuelve esa búsqueda (cortable si llega otra).
        self._web_search_task: Optional[asyncio.Task] = None
        # ``True`` mientras una interrupción al modelo la provocó NUESTRA propia
        # búsqueda (no un barge-in del usuario): la consulta pendiente se
        # conserva en vez de descartarse.
        self._web_preempting = False
        # Modelo REST (no Live) probado para buscar/clasificar, si lo hubo.
        # ``None`` sin agotar = aún no resuelto; ``None`` con flag = ya se
        # probó y nada funcionó (no reintentar en cada orden).
        self._web_model: Optional[str] = None
        self._web_model_exhausted = False
        # Proveedores de terceros cuyas claves ya avisamos que fallan (para no
        # repetir el aviso en el overlay con cada búsqueda).
        self._provider_warned: set = set()
        # Proveedores vetados por pais en ESTA sesion (403 sin JSON). Se apagan
        # en caliente para no pagar un 403 en cada busqueda.
        self._provider_geo_blocked_set: set = set()
        # Pre-warm (resolución en segundo plano al arrancar) del modelo web.
        self._web_warm_task: Optional[asyncio.Task] = None
        # Resolución en vuelo (``_web_model_name``): dos llamadas concurrentes
        # esperan a la primera en vez de repetir los probes.
        self._web_resolve_fut: Optional[asyncio.Future] = None
        # Secuencia de tareas de búsqueda web (para que el finally de una tarea
        # vieja no desactive la pre-interrupción de una más reciente).
        self._web_task_seq = 0
        # Caché de la última búsqueda web (E6): evita repetir llamadas caras.
        self._web_cache_query: Optional[str] = None
        self._web_cache_note: Optional[str] = None
        self._web_cache_ts: float = 0.0
        self._last_web_ts: float = 0.0
        # Guión de conversación (E9): archivo de texto al que se apéndice cada
        # interacción recordada, para consultarlo fuera del chat del overlay.
        self._script_path: Optional[str] = None
        # Acumuladores de transcripción (entrada/salida). Se reinician por
        # sesión en ``_run_session``; con estos valores iniciales cualquier
        # llamada (p. ej. la búsqueda web de voz) es segura antes de conectar.
        self._out_acc: list = []
        self._in_acc: list = []
        # Transcripción completa del turno de respuesta EN CURSO (a diferencia
        # de ``_out_acc``, que se vacía al mostrar en el chat y no sirve para
        # detectar cortes en ``turn_complete``). Se acumula SIEMPRE y se usa
        # para decidir si una respuesta larga se cortó a mitad por el tope del
        # servidor; se limpia al arrancar un turno nuevo (nueva orden, barge-in
        # o al terminar de evaluar el turno).
        self._turn_text = ""
        # Pregunta del turno en curso (Fase 2). Es lo que ``_end_turn`` registra
        # en la memoria de trabajo para poder aprender de cómo acabó. Sin esto,
        # el registro de turnos se quedaba siempre vacío.
        self._pregunta_turno: str = ""
        # La pregunta del turno YA cerrado. Vive aparte porque ``_pregunta_turno``
        # se vacía al cerrar: sin esto, detectar que el usuario está corrigiendo
        # el turno anterior no tendría con qué compararse.
        self._ultima_pregunta: str = ""        # Métricas de latencia por turno (E4): marca del inicio del turno y del
        # primer contenido de respuesta recibido. Se miden con ``time.monotonic``
        # y se reportan por log en ``_end_turn``.
        self._turn_begin: Optional[float] = None
        self._turn_first_audio: Optional[float] = None
        # Reintento automático de un turno de voz que el servidor se quedó sin
        # contestar (aun sin error ni go_away). Se guarda el PCM ya enviado y, si
        # el turno sigue sin recibir NADA, se reabre la sesión y se reenvía una
        # sola vez: el primer turno de una sesión nueva es siempre el que mejor
        # se procesa. Ver ``_watchdog_loop`` y ``_replay_voice_turn``.
        self._voice_replay: Optional[bytes] = None
        self._voice_replay_rate: int = 16000
        self._voice_replay_tries: int = 0
        self._replay_pending: bool = False
        # Respuesta interrumpida por una caída de sesión a mitad de habla: se
        # pide continuarla nada más reconectar (espiga final de fluidez).
        self._resume_mid_answer: Optional[str] = None
        # Rondas de autocompletado que quedan para el turno de usuario actual.
        self._cont_remaining = 0
        # Temporizadores/alarmas activos ([[seconds, label, end_str, done]]); solo
        # se tocan desde el bucle asyncio.
        self._timers: list = []
        self._session = None
        self._session_handle: Optional[str] = None  # reanudación de sesión
        self._player: Optional[AudioPlayer] = None
        self._responding = False
        self._awaiting_turn = False      # se mandó una orden/voz y falta respuesta
        # ``True`` solo mientras el micrófono ESTÁ ENVIANDO audio de verdad. Con
        # el micrófono abierto pero el segmento ya cerrado (esperando respuesta)
        # el bucle de voz descarta los chunks, así que no hay tráfico y el
        # watchdog debe poder detectar el estancamiento. Sin esta distinción el
        # watchdog refrescaba ``_last_data_ts`` indefinidamente y nunca cerraba
        # ni reciclaba el turno. Ver ``_watchdog_loop``.
        self._voice_sending = False
        # Marca de "última actividad de contenido de la respuesta" (audio/texto/
        # transcripción de salida). El watchdog la usa para cerrar el turno por su
        # cuenta si el servidor no envía ``turn_complete`` en ``_OUTDONE_GRACE_S``.
        # ``None`` = sin respuesta en marcha que finalizar.
        self._outdone_ts: Optional[float] = None
        # Guarda contra dobles cierres del mismo turno (receive loop + watchdog).
        self._closing_turn = False
        # El usuario pidió cambiar voz/idioma: la reconexión se programa para
        # aplicarla cuando el canal esté libre (tras cerrar el turno en curso).
        self._reconnect_wanted = False
        self._last_data_ts: Optional[float] = None  # watchdog: último dato recibido
        self._state = AssistantState.IDLE
        self._reset_count = 0
        self._reset_window_start = time.monotonic()
        self._voice_wants = False      # bajo self._lock, sobrevive a reconexiones
        # True si el turno lo abrió una pulsación del usuario (turno acotado) y
        # False si es escucha continua. Solo el primero puede cerrarse solo por
        # silencio; la escucha continua espera a que se apague a mano.
        self._voice_toggle = False
        self._voice_event: Optional[asyncio.Event] = None
        # Momento (loop.time) en que se abrió la voz actual; ``None`` si no hay
        # voz. Lo usa el watchdog para el tope absoluto ``_VOICE_MAX_OPEN_S``.
        self._voice_opened_ts: Optional[float] = None
        # Último instante (loop.time) en que salió audio real por encima del
        # gate. Distingue "está hablando" de "micrófono abierto y mudo", que es
        # lo que el watchdog necesita para no refrescarse eternamente.
        self._voice_last_audio_ts: Optional[float] = None
        self._lock = threading.Lock()
        self._client = genai.Client(
            api_key=settings.api_key,
            http_options={"api_version": settings.api_version},
        )

    def submit_command(self, text: str) -> bool:
        """Envía una orden de texto al asistente (seguro desde otro hilo).

        Devuelve ``True`` si se aceptó la orden o ``False`` si el asistente
        no está en marcha todavía.
        """
        text = (text or "").strip()
        if not text or self._loop is None or self._command_queue is None:
            return False
        self._loop.call_soon_threadsafe(self._command_queue.put_nowait, text)
        return True

    def set_memory_enabled(self, activo: bool) -> bool:
        """Enciende o apaga la memoria en caliente, sin reiniciar la app.

        Apagada significa que no se escribe nada nuevo y que la recuperación no
        inyecta recuerdos: es lo que quiere quien dice "no quiero que se acuerde
        de esto". El índice se suelta en vez de vaciarse, así que volver a
        encenderla recupera lo que hubiera.
        """
        activo = bool(activo)
        with self._lock:
            if bool(self._settings.memory_enabled) == activo:
                return False
            self._settings.memory_enabled = activo
            self._memoria_indice = None if not activo else self._preparar_indice_memoria()
        logger.info(
            "Memoria %s.", "activada" if activo else "apagada"
        )
        return True

    def set_voice(self, active: bool, *, toggle: bool = False) -> bool:
        """Activa o desactiva la entrada por micrófono (seguro desde otro hilo).

        El estado queda guardado para las reconexiones de la sesión. Mientras
        está activa, la voz transmitida interrumpe (barge-in) la respuesta en
        curso del modelo automáticamente.

        *toggle* distingue una pulsación del usuario (turno acotado, que puede
        cerrarse solo por silencio) de la escucha continua (abierta hasta que
        se apague). Sin esa distinción, con ``voice_manual_vad`` el turno solo
        se cerraba al pulsar por segunda vez y el micrófono se quedaba abierto
        sin avisar: el usuario hablaba y no recibía respuesta nunca.
        """
        with self._lock:
            self._voice_wants = bool(active)
            self._voice_toggle = bool(active and toggle)
            event = self._voice_event
            loop = self._loop
        if event is not None and loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._apply_voice_flag, bool(active))
        return True

    def set_output_device(self, name: Optional[str]) -> bool:
        """Cambia en caliente el dispositivo de salida de audio (otro hilo).

        Reabre el reproductor sin reiniciar la sesión ni perder la voz.
        """
        with self._lock:
            player = self._player
            loop = self._loop
        if loop is None or player is None:
            return False

        def _apply() -> None:
            try:
                player.reopen(device_name=name or None)
                self._settings.output_device_name = name or None
                logger.info("Dispositivo de salida cambiado a: %s", name or "predet.")
            except Exception as exc:  # noqa: BLE001 - el usuario verá el fallo fuera
                logger.warning("No se pudo cambiar la salida de audio: %s", exc)
                self._safe_call(self.on_meta, f"(Audio) No se pudo cambiar la salida: {exc}")

        loop.call_soon_threadsafe(_apply)
        return True

    def set_volume(self, volume: float) -> bool:
        """Ajusta el volumen de salida aplicado al PCM del modelo (0.0-1.5)."""
        volume = max(0.0, min(1.5, float(volume)))
        with self._lock:
            player = self._player
        self._settings.output_volume = volume
        if player is not None:
            player.set_volume(volume)
        return True

    def set_voice_name(self, name: str) -> bool:
        """Cambia la voz de síntesis del modelo (otro hilo; se aplica al reconectar).

        La nueva voz entra en la próxima sesión Live. Si hay una respuesta en
        marcha se reconecta justo al cerrar el turno, sin perder la memoria
        breve ni las órdenes pendientes.
        """
        name = (name or "").strip()
        if not name:
            return False
        with self._lock:
            self._settings.voice = name
            self._reconnect_wanted = True
            loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._maybe_reconnect)
        return True

    def set_transcript_lang(self, code: str) -> bool:
        """Cambia el idioma de transcripción de audio (otro hilo; al reconectar)."""
        code = (code or "").strip()
        if not code:
            return False
        with self._lock:
            self._settings.transcript_lang = code
            self._reconnect_wanted = True
            loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._maybe_reconnect)
        return True

    def set_response_modalities(self, modalities: list) -> bool:
        """Cambia el modo de salida (solo voz / voz+texto; al reconectar)."""
        clean = ["AUDIO"] if not modalities else ["AUDIO", "TEXT"] if "TEXT" in modalities else ["AUDIO"]
        with self._lock:
            self._settings.response_modalities = list(clean)
            self._reconnect_wanted = True
            loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._maybe_reconnect)
        return True

    def set_screen_enabled(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        with self._lock:
            self._settings.screen_enabled = enabled
            self._reconnect_wanted = True
            loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._maybe_reconnect)
        return True

    def _maybe_reconnect(self) -> None:
        """Si se pidió cambiar voz/idioma y el canal está libre, aplica la nueva
        configuración cerrando la sesión actual (``run()`` reconecta solo)."""
        if not self._reconnect_wanted:
            return
        # Esperamos a que no haya turno/orden en curso para no cortar nada vivo.
        if self._awaiting_turn or self._voice_active() or self._closing_turn:
            return  # se aplicará al cerrar el turno (ver ``_end_turn``)
        self._reconnect_wanted = False
        session = self._session
        if session is not None:
            asyncio.ensure_future(self._close_for_reconnect(session))

    async def _close_for_reconnect(self, session) -> None:
        """Cierra la sesión Live actual con elegancia; el bucle de ``run()``
        reabre con la configuración nueva sin perder la memoria breve."""
        try:
            await session.close()
        except Exception as exc:  # noqa: BLE001 - la reconexión lo cubre
            logger.debug("Cierre de sesión para reconectar: %s", exc)

    def cancel(self) -> bool:
        """Interrumpe la respuesta en curso y descarta lo pendiente (otro hilo).

        Si el modelo está hablando, se envía un turno de interrupción para que
        corte la generación; en cualquier caso se vacía la cola de órdenes y se
        limpia el búfer de audio de salida.
        """
        if self._loop is None:
            return False
        # _cancel_now es una corrutina: programarla con call_soon dejaría un
        # RuntimeWarning y el botón "Cancelar" no haría nada.
        try:
            asyncio.run_coroutine_threadsafe(self._cancel_now(), self._loop)
        except RuntimeError:  # el bucle ya se está cerrando
            return False
        return True

    def _apply_voice_flag(self, active: bool) -> None:
        if self._voice_event is not None:
            if active:
                if not self._voice_event.is_set():
                    # Marca de apertura de la sesión de voz: sirve para el tope
                    # absoluto del watchdog (una voz que no cierra ningún turno
                    # durante minutos se corta).
                    try:
                        self._voice_opened_ts = asyncio.get_running_loop().time()
                    except RuntimeError:  # sin bucle: no hay vigilancia que hacer
                        self._voice_opened_ts = None
                self._voice_event.set()
            else:
                self._voice_event.clear()
                self._voice_opened_ts = None

    @property
    def state(self) -> AssistantState:
        """Estado actual de la máquina de estados (lectura externa)."""
        return self._state

    def _set_state(self, state: AssistantState) -> None:
        """Transición de estado: solo notifica si cambia.

        Corre en el hilo del bucle asyncio; el overlay se entera mediante
        ``on_state`` (invocado a prueba de fallos por ``_safe_call``).
        """
        if state is self._state:
            return
        self._state = state
        self._safe_call(self.on_state, state.value)

    def _remember(self, role: str, text: str) -> None:
        """Guarda una interacción en la memoria breve (corre en el bucle).

        Si la breve se llena, las entradas más viejas salen ARCHIVADAS a la
        memoria a largo plazo (resumen en segundo plano, ver E3) en vez de
        perderse. Cada ``_PERMANENT_EVERY`` recuerdos se fuerza un archivo
        anticipado del tramo más antiguo (si hay margen para no dejar la breve
        sin contexto reciente).
        """
        text = (text or "").strip()
        if not text:
            return
        self._memory.append({"role": role, "text": text})
        self._log_script(role, text)
        # El índice de grafo es adicional: si falla, la memoria plana sigue
        # avanzando igual.
        self._indexar_recuerdo(text, role, "brief")
        evicted = []
        if len(self._memory) > _MEMORY_MAX:
            overflow = len(self._memory) - _MEMORY_MAX
            evicted = self._memory[:overflow]
            del self._memory[:overflow]
            self._save_memory()
        self._memory_since_archive += 1
        need_archive = bool(evicted) or self._memory_since_archive >= _PERMANENT_EVERY
        if need_archive:
            if evicted:
                self._archive_to_permanent(evicted)
            elif len(self._memory) >= _PERMANENT_MIN_DEPTH:
                take = self._memory[: min(_PERMANENT_EVERY, len(self._memory))]
                del self._memory[: len(take)]
                self._save_memory()
                self._archive_to_permanent(take)
            else:
                # Sin margen para "sacrificar" contexto: solo se reanuda la
                # cadencia del archivo periódico.
                self._memory_since_archive = 0

    # ------------------------------------------------------------------
    # Índice de recuperación en grafo (Fase 1)
    # ------------------------------------------------------------------
    def _preparar_indice_memoria(self):
        """Monta el índice de grafo y migra lo que ya hubiera.

        Nunca lanza: si el grafo no está disponible se devuelve ``None`` y la
        memoria sigue siendo la plana de siempre. Es la única vía por la que la
        app puede quedarse sin índice sin romperse.
        """
        try:
            from memory.migrate import get_backend, migrar

            if not getattr(self._settings, "memory_enabled", True):
                logger.info("Memoria desactivada en Ajustes; se usa solo la plana.")
                return None
            base = (self._settings.data_dir or "").strip()
            if not base:
                logger.info("Sin directorio de datos; la memoria va solo en plano.")
                return None
            indice = get_backend(base, _MEMORY_FILE, _PERMANENT_FILE)
            if indice.nombre == "plano":
                return None
            migrar(indice, _MEMORY_FILE, _PERMANENT_FILE)
            self._podar_memoria(indice)
            logger.info("Índice de memoria en grafo activo (%s).", indice.nombre)
            return indice
        except Exception as exc:  # noqa: BLE001 - la memoria nunca tumba la app
            logger.warning("Sin índice de grafo para la memoria (%s); se usa el plano.", exc)
            return None

    def _podar_memoria(self, indice) -> None:
        """Recorta el grafo al arrancar, con los topes de Ajustes.

        Va aquí y no en cada recuerdo porque podar es lo que reescribe el fichero
        entero; hacerlo en cada escritura lo convertiría en un coste por turno.
        Una vez al abrir la app es suficiente para que no crezca sin freno.
        """
        try:
            topes = int(getattr(self._settings, "memory_max_nodes", 0) or 0)
            dias = int(getattr(self._settings, "memory_retention_days", 0) or 0)
            if topes <= 0 and dias <= 0:
                return
            fuera = indice.podar(max_nodos=topes or None, max_dias=dias or None)
            if fuera:
                logger.info("Memoria podada al arrancar: %d recuerdos fuera.", fuera)
        except AttributeError:
            # Backend sin poda (el plano): la retención la aplica él solo.
            logger.debug("El backend de memoria no admite poda.")
        except Exception as exc:  # noqa: BLE001 - podar nunca debe tumbar el arranque
            logger.warning("No se pudo podar la memoria: %s", exc)

    def _preparar_memoria_trabajo(self):
        """Monta el registro de turnos. Nunca lanza: es una capa opcional."""
        try:
            from memory.work_memory import MemoriaTrabajo

            base = (self._settings.data_dir or "").strip()
            if not base:
                return None
            return MemoriaTrabajo(os.path.join(base, "mindvoice-memory"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Sin memoria de trabajo (%s); no afecta a la memoria.", exc)
            return None

    def registrar_giro(self, pregunta: str, resultado: str = "useful", correccion: str = "") -> None:
        """Anota cómo acabó un turno para aprender de él.

        Es la entrada de la reflexión de la Fase 2. Si no hay registro, el turno
        se guarda igual en la memoria: esta llamada no puede perder nada.
        """
        registro = self._memoria_trabajo
        if registro is None:
            return
        try:
            registro.registrar(pregunta, resultado, correccion=correccion)
        except Exception as exc:  # noqa: BLE001
            logger.debug("No se pudo registrar el turno: %s", exc)

    def _indexar_recuerdo(self, text: str, role: str, kind: str = "brief") -> None:
        """Mete un recuerdo en el índice. Silencioso si no hay índice."""
        indice = self._memoria_indice
        if indice is None:
            return
        try:
            indice.remember(
                text,
                role=role,
                kind=kind,
                importance=0.7 if kind == "permanent" else 0.5,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("No se pudo indexar el recuerdo: %s", exc)

    def _detectar_disputa(self, texto: str) -> str:
        """¿Lo que el usuario dice ahora choca con un recuerdo guardado?

        Se llama ANTES de enviar el turno, y esa es toda la razón de que sea un
        método aparte y no un efecto secundario de ``_remember``: el recuerdo se
        guarda DESPUÉS de ``send_client_content``, así que si se detectara ahí la
        nota llegaría un turno tarde, que es justo cuando ya no sirve de nada.

        Devuelve la nota para el prompt, o ``""`` si no hay choque. Nunca lanza
        y nunca elige ganador: solo levanta la bandera ``contested`` y le pide al
        modelo que pregunte.
        """
        try:
            from memory.work_memory import detectar_chocque

            return detectar_chocque(self._memoria_indice, texto)
        except Exception as exc:  # noqa: BLE001 - la memoria no tumba el turno
            logger.debug("No se pudo revisar contradicciones: %s", exc)
            return ""

    def _marcar_correccion(self, texto: str) -> None:
        """Si el usuario rectifica, el turno anterior queda como ``corrected``.

        Sin esto el resultado "corrected" de la reflexión no se producía nunca:
        el motor solo sabía registrar "útil" o "callejón sin salida", y una
        corrección no es ninguna de las dos cosas. Es además la señal más
        valiosa de las tres: es el único caso en que el usuario dice
        explícitamente que la app se equivocó.
        """
        anterior = (self._ultima_pregunta or "").strip()
        if not anterior:
            return
        try:
            from memory.work_memory import es_correccion

            if not es_correccion(texto):
                return
            self.registrar_giro(anterior, "corrected", correccion=texto[:200])
            logger.info("Memoria: el usuario corrigió el turno anterior.")
        except Exception as exc:  # noqa: BLE001
            logger.debug("No se pudo registrar la corrección: %s", exc)

    def _accion_memoria(self, text: str) -> Optional[str]:
        """Órdenes que hablan de la propia memoria.

        Tres cosas que la memoria en grafo sabe contestar y antes no se podían
        ni intentar: olvidar un tema concreto (no todo), decir qué recuerda
        sobre algo, y de dónde salió un dato.

        Va ANTES que el resto de integraciones y devuelve ``None`` en cuanto ve
        que la orden era de otro sitio, para que "olvida el temporizador" siga
        canceling el temporizador y no borre recuerdos del temporizador.
        """
        t = (text or "").strip()
        low = t.lower()
        if not t:
            return None

        # --- De dónde sale un dato -------------------------------------------
        m = _PROCEDENCIA_RE.search(low)
        if m:
            return self._nota_procedencia((m.group(1) or "").strip())

        # --- Qué recuerdas de algo -------------------------------------------
        m = _RECUERDA_RE.search(low)
        if m:
            return self._nota_recuerda((m.group(1) or "").strip())

        # --- Olvidar un tema (no todo) ---------------------------------------
        m = _FORGET_TOPIC_RE.search(low)
        if m:
            # Si la orden mentions algo que es integración local, no es memoria.
            if (
                _TIMER_CANCEL_RE.search(t)
                or _TIMER_LIST_RE.search(t)
                or "volumen" in low
                or _CLIP_COPY_RE.search(t)
                or _CLIP_READ_RE.search(low)
            ):
                return None
            return self._olvidar_tema((m.group(1) or "").strip())
        return None

    def _nota_procedencia(self, tema: str) -> str:
        """Contesta "de dónde sabes eso" con la procedencia, sin inventar nada."""
        indice = self._memoria_indice
        if indice is None or not tema:
            return (
                "(Memoria: ahora mismo solo hay memoria plana, sin procedencia "
                "por recuerdo. Se guarda todo lo que dices en la conversación, "
                "pero no hay forma de decir de qué turno salió cada cosa.)"
            )
        try:
            encontradas = indice.buscar(tema, limite=4)
        except Exception as exc:  # noqa: BLE001
            logger.debug("No se pudo buscar en la memoria: %s", exc)
            return ""
        if not encontradas:
            return f"(Memoria: no hay ningún recuerdo guardado sobre «{tema}».)"
        partes = []
        for e in encontradas:
            fuente = "lo dijiste tú" if e.role == "user" else "te lo dije yo"
            marca = ", y lo has corregido después" if e.contested else ""
            partes.append(
                f"«{e.text[:160]}» ({fuente} el {e.created_at[:10]}{marca})"
            )
        return (
            "(Procedencia de lo que recuerdo sobre "
            f"«{tema}»: " + "; ".join(partes) + ".)"
        )

    def _nota_recuerda(self, tema: str) -> str:
        """Contesta "qué recuerdas de X" con lo que hay, marcado y sin rellenar."""
        if not tema:
            return "(Dime de qué quieres que te diga qué recuerdo.)"
        indice = self._memoria_indice
        if indice is None:
            return (
                "(Memoria: tengo la conversación reciente y los resúmenes largos, "
                "pero sin índice por temas. Guarda lo que digamos y úsalo a partir "
                "de ahora.)"
            )
        try:
            encontradas = indice.buscar(tema, limite=6)
        except Exception as exc:  # noqa: BLE001
            logger.debug("No se pudo buscar en la memoria: %s", exc)
            return ""
        if not encontradas:
            return f"(Memoria: no tengo nada guardado sobre «{tema}».)"
        lineas = []
        for e in encontradas:
            quien = "dijiste" if e.role == "user" else "dije"
            sufijo = " (esto lo corregiste después, está en disputa)" if e.contested else ""
            lineas.append(f"- {quien}: {e.text[:200]}{sufijo}")
        return (
            f"(Lo que recuerdo sobre «{tema}», con la fuente de cada cosa:\n"
            + "\n".join(lineas)
            + ")"
        )

    def _olvidar_tema(self, tema: str) -> str:
        """Olvida lo que hable de ``tema``. Es el "olvida" fino, no el de todo."""
        if not tema or len(tema) < 2:
            return "(Dime qué tema quieres que olvide.)"
        borrados: list[str] = []
        indice = self._memoria_indice
        if indice is not None:
            try:
                borrados = indice.forget_matching(tema, limite=12)
            except Exception as exc:  # noqa: BLE001
                logger.warning("No se pudo olvidar el tema en el grafo: %s", exc)
        # La memoria plana es la fuente de verdad del turno, así que también se
        # filtra aquí. Sin esto, lo borrado del grafo volvería en el bloque plano.
        antes = len(self._memory) + len(self._permanent)
        self._memory = [e for e in self._memory if not _habla_de(e.get("text", ""), tema)]
        self._permanent = [
            e for e in self._permanent if not _habla_de(e, tema)
        ]
        if antes != len(self._memory) + len(self._permanent):
            self._save_memory()
        plano = antes - (len(self._memory) + len(self._permanent))
        total = len(borrados) + plano
        if self._memoria_trabajo is not None:
            try:
                self._memoria_trabajo.forget(tema)
            except Exception as exc:  # noqa: BLE001
                logger.debug("No se pudo limpiar la memoria de trabajo: %s", exc)
        if not total:
            return f"(Memoria: no tenía nada guardado sobre «{tema}», así que no hay nada que olvidar.)"
        muestra = "; ".join(t[:80] for t in (borrados or [])[:3])
        extra = f" Por ejemplo: {muestra}." if muestra else ""
        logger.info("Memoria: olvidado el tema %r (%d recuerdo(s)).", tema, total)
        return (
            f"(Memoria: olvidado «{tema}»: {total} recuerdo(s) borrados.{extra} "
            "No vuelvas a sacarlo salvo que el usuario vuelva a hablar de ello.)"
        )

    def _log_script(self, role: str, text: str) -> None:
        """Apéndice el guión de la conversación a un archivo en ``data_dir`` (E9).

        Cada interacción recordada (usuario o MindVoice) se escribe con su
        hora como ``[HH:MM:SS] Rol: texto`` en ``guion-conversacion-<fecha>.txt``
        dentro del directorio de datos. Nunca lanza: si el archivo no se puede
        crear/escribir (permisos, unidad desmontada), solo se registra.
        """
        if self._script_path is None:
            try:
                basedir = (self._settings.data_dir or "").strip()
                if basedir:
                    os.makedirs(basedir, exist_ok=True)
                else:
                    basedir = tempfile.gettempdir()
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                self._script_path = os.path.join(
                    basedir, f"guion-conversacion-{stamp}.txt"
                )
            except Exception as exc:  # noqa: BLE001 - no romper el turno por el guión
                logger.debug("Guión no preparado (%s)", exc)
                self._script_path = False
        if not self._script_path:
            return
        try:
            when = datetime.now().strftime("%H:%M:%S")
            who = {"user": "Tú", "assistant": "MindVoice"}.get(role, role)
            with open(self._script_path, "a", encoding="utf-8") as fh:
                fh.write(f"[{when}] {who}: {text}\n")
        except Exception as exc:  # noqa: BLE001 - nunca lanza
            logger.debug("Guión no escrito (%s)", exc)

    def _load_memory(self) -> list:
        """Carga la memoria breve persistida (o ``[]`` si no hay/corrupta)."""
        try:
            with open(_MEMORY_FILE, encoding="utf-8") as fh:
                stored = json.load(fh)
            memory = []
            for entry in stored if isinstance(stored, list) else []:
                if (
                    isinstance(entry, dict)
                    and entry.get("role") in ("user", "assistant")
                    and isinstance(entry.get("text"), str)
                    and entry["text"].strip()
                ):
                    memory.append(
                        {"role": entry["role"], "text": entry["text"].strip()}
                    )
            return memory[-_MEMORY_MAX:]
        except (OSError, ValueError) as exc:
            logger.debug("Memoria persistida no disponible: %s", exc)
            return []

    def _save_memory(self) -> None:
        """Persiste la memoria breve (best-effort; nunca debe tumbar nada)."""
        try:
            with open(_MEMORY_FILE, "w", encoding="utf-8") as fh:
                json.dump(self._memory, fh, ensure_ascii=False, indent=2)
        except (OSError, TypeError) as exc:
            logger.warning("No se pudo guardar la memoria: %s", exc)

    # ------------------------------------------------------------------
    # Memoria a largo plazo (E3): archivo de resúmenes + reinyección
    # ------------------------------------------------------------------
    def _load_permanent(self) -> list:
        """Carga la memoria a largo plazo persistida (o ``[]`` si no hay/corrupta)."""
        try:
            with open(_PERMANENT_FILE, encoding="utf-8") as fh:
                stored = json.load(fh)
        except (OSError, ValueError) as exc:
            logger.debug("Memoria a largo plazo no disponible: %s", exc)
            stored = []
        if isinstance(stored, list):
            return [
                (s if isinstance(s, str) else "").strip()[:_PERMANENT_MAX_CHARS]
                for s in stored
                if isinstance(s, str) and s.strip()
            ][-_PERMANENT_MAX:]
        logger.debug("Memoria a largo plazo con formato inesperado; se ignora.")
        return []

    def _archive_to_permanent(self, entries: list) -> None:
        """Archiva unas entradas de memoria breve como resumen (async, E3).

        Lanza la tarea en segundo plano (nunca bloquea la respuesta); si ya hay
        un archivo en curso se descarta el nuevo (la cadencia ya se da). Se
        invoca desde el bucle asyncio únicamente.
        """
        entries = [
            e for e in entries if (e or {}).get("text", "").strip()
        ]
        if not entries:
            return
        task = self._archive_task
        if task is not None and not task.done():
            return
        self._memory_since_archive = 0
        self._archive_task = asyncio.ensure_future(self._do_archive(entries))

    async def _do_archive(self, entries: list) -> None:
        """Resume ``entries`` y lo añade a la memoria a largo plazo en disco."""
        summary = await self._summarize_memories(entries)
        if not summary:
            return
        stored = self._load_permanent()
        stored.append(summary[: _PERMANENT_MAX_CHARS])
        stored = stored[-_PERMANENT_MAX:]
        self._permanent = stored
        self._indexar_recuerdo(summary[:_PERMANENT_MAX_CHARS], "system", "permanent")
        try:
            with open(_PERMANENT_FILE, "w", encoding="utf-8") as fh:
                json.dump(stored, fh, ensure_ascii=False, indent=2)
        except (OSError, TypeError) as exc:
            logger.warning("No se pudo guardar la memoria a largo plazo: %s", exc)

    async def _summarize_memories(self, entries: list) -> str:
        """Resume unas entradas de memoria breve (REST barato; fallback local).

        Reusa la resolución de modelo de la búsqueda web (flash-lite si existe)
        con un timeout corto; si la llamada falla o no hay modelo, comprime en
        LOCAL los textos (plan B silencioso). Nunca lanza.
        """
        chunks = []
        for entry in entries:
            who = "usuario" if entry.get("role") == "user" else "MindVoice"
            text = (entry.get("text") or "").strip()[:_MEMORY_MAX_CHARS]
            if text:
                chunks.append(f"- {who}: {text}")
        joined = "\n".join(chunks)[-4000:]
        if not joined:
            return ""
        model = await self._web_model_name()
        summary = ""
        if model:
            try:
                response = await asyncio.wait_for(
                    self._client.aio.models.generate_content(
                        model=model,
                        contents=_SUMMARY_PROMPT + joined,
                        config=types.GenerateContentConfig(
                            temperature=0.3, max_output_tokens=300
                        ),
                    ),
                    timeout=_SUMMARY_TIMEOUT,
                )
                summary = (getattr(response, "text", None) or "").strip()
            except Exception as exc:  # noqa: BLE001 - fallback local
                logger.debug("Resumen por IA no disponible: %s", exc)
        if not summary:
            # Compresión local: un limbo legible con lo último de cada entrada,
            # sin duplicados evidentes.
            seen = set()
            brief = []
            for chunk in chunks:
                text = chunk[:_PERMANENT_MAX_CHARS]
                key = text[:60].lower()
                if key in seen:
                    continue
                seen.add(key)
                brief.append(text)
            summary = " · ".join(brief[:6])
        summary = (summary or "").strip().rstrip(".,;: ")
        return summary[:_PERMANENT_MAX_CHARS]

    def _ampliar_bloque_memoria(self, bloque: str) -> str:
        """Añade lecciones y preferencias al bloque ya construido.

        Nunca quita nada: si esta capa falla, se queda el bloque de memoria tal
        cual estaba, que es el comportamiento conocido.
        """
        registro = self._memoria_trabajo
        if registro is None:
            return bloque
        try:
            from memory.work_memory import nota_ampliada

            entradas = list(self._memory) + list(self._permanent)
            return nota_ampliada(
                bloque,
                registro=registro,
                grafo=self._memoria_indice,
                entradas=entradas,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("No se pudo ampliar el bloque de memoria: %s", exc)
            return bloque

    def _build_memory_block(self, query: str = "") -> str:
        """Renderiza la memoria previa como nota para el system prompt.

        Con índice de grafo se manda solo lo relevante a ``query``; sin índice,
        el bloque es exactamente el de siempre (secciones larga plazo + breve y
        presupuesto de ``_MEMORY_BUDGET``).
        """
        indice = self._memoria_indice
        if indice is not None:
            try:
                bloque = indice.block(query, presupuesto=_MEMORY_BUDGET)
                # Un índice recién vacío no puede dejar al modelo sin memoria.
                if bloque.strip():
                    return self._ampliar_bloque_memoria(bloque)
            except Exception as exc:  # noqa: BLE001 - degradar al bloque plano
                logger.warning("La recuperación por grafo falló (%s); se manda la memoria completa.", exc)

        sections = []
        long_lines = []
        for summary in self._permanent:
            text = summary if len(summary) <= _PERMANENT_MAX_CHARS else (
                summary[:_PERMANENT_MAX_CHARS] + "…"
            )
            long_lines.append(f"- {text}")
        if long_lines:
            sections.append("[A largo plazo (hechos persistentes resumidos)]\n" + "\n".join(long_lines))
        if self._memory:
            lines = []
            for entry in self._memory:
                text = entry["text"]
                if len(text) > _MEMORY_MAX_CHARS:
                    text = text[:_MEMORY_MAX_CHARS] + "…"
                who = "usuario" if entry["role"] == "user" else "MindVoice"
                lines.append(f"- {who}: {text}")
            # Presupuesto de caracteres (E8): aunque quepan ``_MEMORY_MAX``
            # entradas, la nota inyectada no puede exceder ``_MEMORY_BUDGET``
            # caracteres; se descartan los tramos más antiguos hasta ajustarse.
            while len("\n".join(lines)) > _MEMORY_BUDGET and len(lines) > 1:
                lines.pop(0)
            sections.append("[Breve (conversación reciente)]\n" + "\n".join(lines))
        return "\n\n".join(sections)

    def _voice_active(self) -> bool:
        """``True`` si la entrada por voz está transmitiendo en este momento."""
        return self._voice_event is not None and self._voice_event.is_set()

    def _clear_turn_state(self) -> None:
        self._responding = False
        self._awaiting_turn = False
        self._outdone_ts = None
        self._out_acc.clear()
        self._in_acc.clear()
        self._turn_text = ""
        self._turn_begin = None
        self._turn_first_audio = None
        self._cont_remaining = 0
        # El contador de reenvíos pertenece al turno que se acaba de abandonar.
        # Sin este rearme, ``_watchdog_loop`` (que exige ``< 1``) se quedaba sin
        # reenvío de voz para TODOS los turnos siguientes de la sesión: el primer
        # estancamiento gastaba el único intento y la única recuperación existente
        # quedaba muerta hasta reiniciar la app.
        self._voice_replay_tries = 0
        self._set_state(
            AssistantState.LISTENING
            if self._voice_active()
            else AssistantState.IDLE
        )

    async def _cancel_now(self) -> None:
        command_queue = self._command_queue
        if command_queue is not None:
            while not command_queue.empty():
                try:
                    command_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        player = self._player
        if player is not None:
            player.flush()
        session = self._session
        if session is not None and self._responding:
            try:
                # La interrupción se manda como realtime_input de texto
                # (send_client_content iría como "turno completo" y no corta la
                # voz en curso). Solo con el modelo HABLANDO: si lo único que
                # hay es un turno pendiente de procesar (``_awaiting_turn``),
                # mandar este texto deja la sesión muerta sin error (el modelo
                # contesta "Entendido, paro aquí" y tu frase se pierde), así
                # que basta con limpiar el estado y dejar que el turno acabe o
                # que el watchdog lo reinicie.
                await session.send_realtime_input(text=_INTERRUPT_TEXT)
            except Exception as exc:  # noqa: BLE001 - sesión a punto de cerrarse
                logger.debug("Interrupción no enviada: %s", exc)
        # Cancelar también descarta la búsqueda web de voz que estuviera en vuelo.
        if self._web_search_task is not None and not self._web_search_task.done():
            self._web_search_task.cancel()
        self._web_search_task = None
        self._pending_web_query = None
        self._web_preempting = False
        self._clear_turn_state()
        self._resume_mid_answer = None

    # ------------------------------------------------------------------
    # Construcción de la sesión
    # ------------------------------------------------------------------
    def _connect_config(self) -> types.LiveConnectConfig:
        """Devuelve la configuración de la sesión Live."""
        # Contexto horario fresco (evita que el modelo invente horas): se
        # inyecta en cada reconexión y las preguntas de hora/fecha se
        # responden con el dato real del equipo.
        stamp = datetime.now().strftime("%A %d de %B de %Y, %H:%M:%S")
        system_instruction = (
            f"{self._settings.system_instruction}\n"
            f"[Contexto: fecha y hora actuales del sistema del usuario: "
            f"{stamp}. Úsalas para responder cualquier pregunta sobre la hora "
            "o el día actual.]"
        )
        memory = self._build_memory_block()
        if memory:
            system_instruction += (
                "\n[Memoria previa (conversación anterior a esta sesión; no "
                "describe la pantalla actual). Úsala solo para mantener el "
                f"hilo cuando el usuario lo pida]:\n{memory}"
            )
        # Motor de búsqueda: el modelo tiene que saberlo, no solo recibir datos.
        # Sin esto la nota llegaba como texto plano y no podía atribuírsela a
        # nadie, así que hablaba de "he buscado" sin saber dónde lo había hecho.
        # Se pide que lo MENCIONE en voz alta al buscar, que es lo que el
        # usuario quiere oír; el nombre hablado va corto, sin el paréntesis.
        try:
            motor = self._engine_spoken()
        except Exception:  # noqa: BLE001 - es contexto, nunca debe romper la sesión
            motor = "DuckDuckGo"
        system_instruction += (
            f"\n[Motor de búsqueda: ahora mismo busca en internet con {motor}. "
            f"Cada vez que hagas una búsqueda, DILO EN VOZ ALTA: nombra {motor} "
            "al empezar la respuesta, de forma breve y natural ('lo he buscado "
            f"en {motor}', 'esto lo he encontrado en {motor}'), y después das los "
            "datos. Una mención por búsqueda basta: no lo repitas en cada frase. "
            "Cada nota de resultado te dirá su motor entre corchetes: ése es el "
            "que trae los datos de ese turno, así que si cambia (porque el "
            "usuario lo haya cambiado en Ajustes) nombra el de la NOTA. No te "
            "atribuyas búsquedas que no hayas hecho nunca.]"
        )
        # Vocabulario del usuario: nombres propios y términos técnicos que su
        # voz produce mal y el reconocedor "arregla" por su cuenta. El caso real
        # era "OpenCode" transcrito como "OpenCog": no era solo una falta de
        # ortografía, la búsqueda y la respuesta downstream iban sobre el
        # programa equivocado. Se inyecta como ortografía LITERAL.
        vocab = (getattr(self._settings, "speech_vocabulary", "") or "").strip()
        if vocab:
            system_instruction += (
                "\n[Ortografía literal — transcribe y escribe EXACTAMENTE así "
                "estos términos, con estas mayúsculas y minúsculas, aunque te "
                "suenen parecidos a otros. No los autocorrijas, no los "
                "normalices a otra marca y no los sustituyas por un término "
                f"parecido: {vocab}. Cuando el usuario pronuncie uno de ellos, "
                "ese es el nombre correcto del programa, proyecto o producto, y "
                "todo lo que digas o busques sobre él se refiere a este.]"
            )
        return types.LiveConnectConfig(
            response_modalities=self._settings.response_modalities,
            system_instruction=system_instruction,
            # Tope de tokens de salida POR TURNO: el servidor recorta en
            # silencio el turno (corta el audio a mitad de frase = "voz que se
            # pega") cuando se supera su tope por defecto. Subirlo permite que
            # las respuestas largas de la regla 1 terminen de verdad.
            max_output_tokens=16384,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self._settings.voice
                    )
                )
            ),
            # Los *-live-preview solo devuelven audio; la transcripción de
            # salida convierte lo que dice el modelo en texto para el chat.
            output_audio_transcription=types.AudioTranscriptionConfig(
                language_codes=self._transcript_langs()
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(
                language_codes=self._transcript_langs()
            ),
            # Reanudación de sesión (opt-in; experimental). Con v1alpha el
            # servidor no publica handles fiables y las sesiones reanudadas se
            # cortaron con 1008 a los ~20-30 s, así que va desactivada por
            # defecto (config.session_resumption).
            session_resumption=(
                types.SessionResumptionConfig(handle=self._session_handle)
                if self._settings.session_resumption
                else None
            ),
            # Cierre de turno por voz.
            #
            # MODO MANUAL (``voice_manual_vad``, el habitual): se DESACTIVA la
            # detección del servidor y el cierre de turno lo decide el usuario
            # al soltar el botón (``activity_start`` / ``activity_end``). Sin
            # esto el servidor cerraba el turno a los 300 ms de silencio
            # (``_VAD_SILENCE_MS``) y contestaba a mitad de frase cada vez que
            # el usuario hacía una pausa natural al pensar.
            #
            # MODO AUTOMÁTICO (el anterior, ``voice_manual_vad=False``): con los
            # valores por defecto el VAD tardaba decenas de segundos (o se
            # olvidaba del turno) en dar por terminada una frase y el usuario se
            # quedaba esperando: medido, 3-6 s por turno con estos valores frente
            # a turnos que no llegaban a responder en 30 s. ``silence_duration_ms``
            # es el silencio que el servidor exige para cerrar el turno y
            # ``end_of_speech`` alto hace que ese cierre sea más rápido.
            #
            # ``start_of_speech`` se deja como venga (sin tocar): con
            # START_SENSITIVITY_HIGH el servidor respondía al primer turno y a
            # partir del SEGUNDO se quedaba igual de mudo dentro de la misma
            # sesión, que es justo el fallo que se quiere eliminar.
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=(
                    types.AutomaticActivityDetection(disabled=True)
                    if self._settings.voice_manual_vad
                    else types.AutomaticActivityDetection(
                        silence_duration_ms=_VAD_SILENCE_MS,
                        end_of_speech_sensitivity=(
                            types.EndSensitivity.END_SENSITIVITY_HIGH
                        ),
                    )
                ),
            ),
        )

    # ------------------------------------------------------------------
    # Ciclo de vida principal
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Bucle principal: abre la sesión y se reconecta si se cae."""
        player: Optional[AudioPlayer] = None
        screen: Optional[ScreenCapture] = None
        attempt = 0
        self._loop = asyncio.get_running_loop()
        self._command_queue = asyncio.Queue()
        self._session_handle = None  # sesión fresca por arranque de la app
        # Restaurar temporizadores/alarmas que quedaron "en marcha" al cerrar.
        try:
            self._load_timers()
        except Exception:  # noqa: BLE001 - sin temporizadores: se sigue igual
            logger.debug("No se pudieron restaurar los temporizadores.", exc_info=True)
        try:
            try:
                player = AudioPlayer(
                    rate=self._settings.output_rate,
                    channels=self._settings.output_channels,
                    device_name=self._settings.output_device_name,
                )
                player.set_volume(self._settings.output_volume)
            except OSError as exc:
                logger.error("No hay dispositivo de salida de audio: %s", exc)
                self._stop_reason = "sin_audio"
                return

            if self._settings.screen_enabled:
                try:
                    screen = ScreenCapture(
                        monitor=self._settings.screen_monitor,
                        max_size=self._settings.screen_max_size,
                        quality=self._settings.screen_quality,
                        # Rate limiting: el lado corto de screen_fps marca la
                        # frecuencia máxima de captura por segundo.
                        min_interval=(
                            1.0 / self._settings.screen_fps
                            if self._settings.screen_fps > 0
                            else 0.0
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - mss lanza variados
                    logger.warning(
                        "No se pudo iniciar la captura de pantalla: %s", exc
                    )
                    screen = None

            # Pre-warm de la búsqueda web: resolver el modelo REST para
            # buscar/clasificar es un baile de probes (models.list + nombres de
            # reserva) que NO debe pagar la primera orden de texto en vivo. Se
            # lanza en segundo plano y queda cacheado en ``_web_model``; si la
            # primera búsqueda le gana, la resolución termina en paralelo o se
            # corta por su presupuesto (_WEB_MODEL_RESOLVE_DEADLINE), y la
            # orden continúa igual (sin nota).
            async def _prewarm_web_model() -> None:
                await self._web_model_name()

            self._startup_diagnostics()
            if self._settings.web_search_enabled:
                warm = asyncio.create_task(_prewarm_web_model())
                self._web_warm_task = warm
                active = self._active_provider()
                if active:
                    asyncio.create_task(self._probe_active_provider(active))

            while not self.quit_event.is_set():
                try:
                    session_started = time.monotonic()
                    await self._run_session(player, screen)
                    attempt = 0  # la sesión terminó limpiamente
                    if not self.quit_event.is_set():
                        # Pequeña pausa para no reconectar en bucle inmediato.
                        await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - reconexión
                    if self.quit_event.is_set():
                        break
                    if _is_fatal(exc):
                        logger.error("Error permanente: %s", exc)
                        self._set_state(AssistantState.ERROR)
                        self._stop_reason = f"error_fatal: {exc}"[:200]
                        return
                    # Sesiones que vivieron bastante (>= 60 s) se cortan por la
                    # rotación esperada del servidor (v1alpha: ~2,5 min), no por
                    # un fallo del motor: NO se escala el retroceso y la
                    # reconexión ocurre al instante (1 s). Solo una caída rápida
                    # (turno congelado, sesión rota) merece retroceso creciente.
                    if time.monotonic() - session_started >= 60.0:
                        attempt = 0
                    attempt += 1
                    self._set_state(AssistantState.ERROR)
                    if attempt >= 3 and self._session_handle is not None:
                        logger.warning(
                            "Varios intentos seguidos fallando: se descarta el "
                            "handle de reanudación y se abre sesión nueva."
                        )
                        self._session_handle = None
                    delay = min(
                        self._settings.reconnect_base_delay * (2 ** (attempt - 1)),
                        self._settings.reconnect_max_delay,
                    )
                    if _is_quota_error(exc):
                        # Cuota o límite de tasa: NO se aplica el retroceso
                        # exponencial (insistir cada 2 s solo empeora el 429).
                        # Se avisa al cliente y se espera un enfriamiento largo.
                        delay = _QUOTA_COOLDOWN_S
                        attempt = 0
                        logger.warning(
                            "Cuota de la API agotada o límite de tasa (%s). "
                            "Se espera %.0f s antes de reintentar.",
                            exc,
                            delay,
                        )
                        self._stop_reason = f"cuota_agotada: {exc}"[:200]
                    else:
                        logger.warning(
                            "Conexión perdida (%s). Reintento en %.1f s...",
                            exc,
                            delay,
                        )
                        self._stop_reason = f"conexion_perdida: {exc}"[:200]
                    # Un corte del servidor en pleno turno se veía como
                    # "la app dejó de escucharme". Se avisa SOLO si había
                    # interacción en curso: los recortes en reposo (la rotación
                    # normal de ~2,5 min) siguen siendo silenciosos.
                    if self._voice_active() or self._awaiting_turn or self._responding:
                        self._safe_call(
                            self.on_meta,
                            "Reconectando: el servidor cortó la sesión a mitad "
                            "de tu turno. Tu frase se reenvía sola.",
                        )
                    try:
                        await asyncio.wait_for(
                            self.quit_event.wait(), timeout=delay
                        )
                        break
                    except asyncio.TimeoutError:
                        continue
            self._stop_reason = (
                "parada_solicitada" if self.quit_event.is_set() else "bucle_salido"
            )
        finally:
            if screen is not None:
                screen.close()
            if player is not None:
                player.close()
            self._set_state(AssistantState.IDLE)
            # Motivo de la parada: sin esto, un "Asistente detenido" sin ningún
            # error previo era imposible de diagnosticar (el watchdog reiniciaba
            # y el bucle se repetía sin dejar rastro de qué lo.paró).
            logger.info(
                "Asistente detenido (quit_event=%s, modo=%s)",
                self.quit_event.is_set(),
                getattr(self._stop_reason, "value", self._stop_reason),
            )

    async def _run_session(
        self,
        player: AudioPlayer,
        screen: Optional[ScreenCapture],
    ) -> None:
        """Abre una sesión Live y dirige los flujos hasta que algo cierre."""
        async with self._client.aio.live.connect(
            model=self._settings.model,
            config=self._connect_config(),
        ) as session:
            logger.info(
                "Sesión Live conectada (id=%s, modelo=%s%s)",
                session.session_id,
                self._settings.model,
                (
                    f", reanudando contexto ({self._session_handle[:12]}…)"
                    if self._session_handle
                    else ""
                ),
            )
            self._session = session
            self._player = player
            self._responding = False
            self._awaiting_turn = False
            self._last_data_ts = asyncio.get_running_loop().time()
            self._out_acc: list = []
            self._in_acc: list = []
            # Sesión nueva: sin respuesta "acabada" pendiente de turn_complete.
            self._outdone_ts = None
            self._closing_turn = False
            # Nuevo turno de sesión: la transcripción acumulada de la respuesta
            # anterior ya no aplica (se evaluó/envió en el momento del corte).
            self._turn_text = ""
            # Si una búsqueda web por voz sobrevivió a la reconexión, se
            # conserva su referencia (se acaba sola) y su nota irá a ESTA
            # sesión: no regenerar ni pisar el token.
            if self._web_search_task is not None and self._web_search_task.done():
                self._web_search_task = None
            self._pending_web_query = None
            self._web_preempting = False
            self._voice_event = asyncio.Event()
            self._voice_opened_ts = None
            with self._lock:
                if self._voice_wants:
                    self._voice_event.set()
                    self._voice_opened_ts = asyncio.get_running_loop().time()
            if player is not None:
                player.flush()

            self._set_state(
                AssistantState.LISTENING
                if self._voice_active()
                else AssistantState.IDLE
            )

            guard = asyncio.create_task(self.quit_event.wait())
            command_task = asyncio.create_task(
                self._send_command_loop(session, player, screen)
            )
            recv_task = asyncio.create_task(
                self._receive_loop(session, player)
            )
            voice_task = asyncio.create_task(
                self._voice_loop(session, player, screen)
            )
            watchdog = asyncio.create_task(
                self._watchdog_loop(session, player)
            )

            # ¿Murió la sesión anterior a mitad de una respuesta hablada? Se
            # pide su continuación en cuanto la sesión nueva está viva para que
            # la frase quede cerrada con fluidez (en vez de cortada en seco).
            if self._resume_mid_answer:
                tail = self._resume_mid_answer
                self._resume_mid_answer = None
                self._turn_text = ""
                logger.info(
                    "Respuesta interrumpida por caída de sesión; se continúa "
                    "desde el último tramo (…%s)",
                    tail[-80:],
                )
                asyncio.create_task(
                    self._request_continuation(session, anchor=tail)
                )

            # El turno de voz anterior se quedó sin respuesta: se reenvía el
            # audio ya capturado en ESTA sesión (una sola vez) para que la
            # frase no se pierda y el usuario no tenga que repetirla.
            if self._replay_pending:
                self._replay_pending = False
                asyncio.create_task(self._replay_voice_turn(session))

            done, pending = await asyncio.wait(
                {guard, command_task, recv_task, voice_task, watchdog},
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancelamos el resto para cerrar la sesión de forma ordenada.
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            # La búsqueda web por voz corre al margen del ciclo de vida de las
            # tareas de esta generación. Si sigue EN VUELO al reciclar la sesión
            # no se cancela: si ya envió la petición al motor de búsqueda,
            # cancelarla habría
            # gastado el crédito sin entregar nada. La tarea termina y entrega
            # la nota a la sesión vigente (o se descarta sola: nueva orden,
            # barge-in o sin sesión viva al terminar).
            if self._web_search_task is not None and self._web_search_task.done():
                self._web_search_task = None
            self._pending_web_query = None
            self._web_preempting = False
            # Entregar el texto parcial de la respuesta que quedó sin flushear
            # (corte a mitad de flujo o sesión caída): así lo ya dicho nunca
            # desaparece del chat del cliente.
            self._flush_output()
            if self._responding and (self._turn_text or "").strip():
                # La sesión murió a mitad de una respuesta hablada (sin barge-in
                # ni watchdog, que habrían marcado _responding=False a tiempo):
                # se guarda la cola de lo dicho para continuarla tras reconectar.
                self._resume_mid_answer = (self._turn_text or "").strip()[-300:]
            with self._lock:
                self._voice_event = None
                self._session = None
                self._player = None

            # Re-lanzamos el primer error real (si no fue la señal de salida).
            if guard not in done:
                for task in done:
                    try:
                        exc = task.exception()
                    except asyncio.CancelledError:
                        continue
                    if exc is not None:
                        raise exc

    # ------------------------------------------------------------------
    # Flujos concurrentes
    # ------------------------------------------------------------------
    async def _send_command_loop(
        self,
        session,
        player: Optional[AudioPlayer],
        screen: Optional[ScreenCapture],
    ) -> None:
        """Describe la pantalla y envía cada orden como turno de texto.

        La pantalla se "traduce" a una descripción (mini-sesión de visión,
        ``_describe_screen``) y viaja como texto junto a la orden: la sesión de
        voz jamás recibe imágenes (probado: un ``send_client_content`` con
        imagen inline deja de responder al audio realtime posterior).
        """
        while not self.quit_event.is_set():
            text = await self._command_queue.get()
            if self.quit_event.is_set():
                return
            self._screen_ref = screen
            if player is not None:
                player.flush()
            # Nueva orden de usuario: la respuesta anterior ya no cuenta.
            self._turn_text = ""
            self._outdone_ts = None
            # La pregunta del turno, para el registro de la Fase 2.
            self._pregunta_turno = text
            # Fase 2: si esto es una rectificación, el turno anterior queda
            # registrado como ``corrected`` y entra en la reflexión.
            self._marcar_correccion(text)

            # Marca de tiempo mínima: el modelo responde la hora con el dato
            # real en vez de inventarla.
            stamp = datetime.now().strftime("%a %d/%m %H:%M:%S")
            # Comando de reset de memoria: se borra la memoria breve y se envía
            # la orden avisando al modelo de que se empieza de cero. Los resets
            # nunca disparan cálculo, integraciones locales ni búsqueda web.
            is_reset = any(
                token in (text or "").lower() for token in _MEMORY_RESET_TRIGGERS
            )
            if is_reset:
                # Un reset NO pierde a largo plazo: se archiva la breve como
                # resumen (E3) y luego se empieza de cero el hilo inmediato.
                if self._memory:
                    self._archive_to_permanent(self._memory)
                self._memory.clear()
                self._save_memory()
                # El índice también se limpia: si no, "olvida todo" dejaría los
                # recuerdos recuperables desde el grafo.
                if self._memoria_indice is not None:
                    try:
                        self._memoria_indice.reset()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("No se pudo limpiar el índice de memoria: %s", exc)
                logger.info("Memoria breve borrada por la orden del usuario.")
            cmd = f"[{stamp}] {text}"
            if is_reset:
                cmd = (
                    f"[{stamp}] (El usuario pide olvidar todo el contexto "
                    f"anterior; responde como si empezaras de cero.) {text}"
                )
            # Integración de cálculo: si la orden es una cuenta, se resuelve AQUÍ
            # (sin depender del modelo) y el resultado viaja como dato confirmado.
            math_note = self._maybe_math_note(text)
            # Integraciones locales (temporizador, volumen, portapapeles): se
            # ejecutan en la app; su nota reemplaza la búsqueda web del turno.
            local_note = self._local_action(text) if not is_reset else None
            # `note` y `web_note` se rellenan más abajo, DENTRO de un `if`
            # (solo si hay descripción de pantalla, y solo si hay búsqueda web).
            # Se inicializan aquí a "" porque la instrumentación de la Fase 0 las
            # mide al final del turno: sin esto, el primer turno sin visión
            # lanzaba NameError y el `except` de abajo descartaba la orden
            # entera, así que el usuario escribía y el asistente se callaba.
            note = ""
            web_note = ""
            # `note` y `web_note` se rellenan más abajo, DENTRO de un `if`
            # (solo si hay descripción de pantalla, y solo si hay búsqueda web).
            # Se inicializan aquí a "" porque la instrumentación de la Fase 0 las
            # mide al final del turno: sin esto, el primer turno sin visión
            # lanzaba NameError y el `except` de abajo descartaba la orden
            # entera, así que el usuario escribía y el asistente se callaba.
            # Nueva orden de usuario: se vuelven a permitir las rondas de
            # autocompletado (respuestas largas cortadas por el servidor).
            self._cont_remaining = _CONTINUE_MAX_ROUNDS
            try:
                # Notas de contexto del turno: descripción de pantalla
                # (opcional) + resultado de búsqueda web. Ambas se preparan en
                # PARALELO (mini-sesión de visión y detector de intención web)
                # para no encadenar esperas; la nota web se inyecta DENTRO de
                # este mismo turno, de modo que la respuesta es única, informada
                # y sale hablada.
                parts: list = []
                description = ""
                desc_fut = None
                classify_fut = None
                if self._settings.screen_enabled and screen is not None:
                    now = asyncio.get_running_loop().time()
                    if (
                        self._desc_cache is not None
                        and now - self._desc_cache_ts <= _DESC_CACHE_TTL
                    ):
                        description = self._desc_cache
                    elif (
                        self._desc_prefetch_task is not None
                        and not self._desc_prefetch_task.done()
                    ):
                        # El HUD ya avisó al enfocar la barra: esa descripción
                        # sigue en curso y solo hay que recogerla.
                        desc_fut = self._desc_prefetch_task
                    else:
                        desc_fut = asyncio.ensure_future(
                            self._describe_screen(screen)
                        )
                # Intención web: disparador explícito (rápido, sin llamada
                # extra) o detector inteligente por IA para frases naturales.
                # (Los resets de memoria nunca buscan.)
                explicit_query = (
                    self._web_query(text)
                    if (
                        self._settings.web_search_enabled
                        and not is_reset
                        and local_note is None
                    )
                    else None
                )
                if (
                    explicit_query is None
                    and self._settings.web_search_enabled
                    and self._settings.web_smart_detect
                    and self._looks_searchable(text)
                    and not is_reset
                    and local_note is None
                ):
                    classify_fut = asyncio.ensure_future(
                        self._classify_web_query(text)
                    )
                # Mientras se prepara la orden (visión + búsqueda web) hay un
                # turno en curso: se marca para que el watchdog no tildar la
                # sesión esperando datos del servidor.
                self._awaiting_turn = True
                self._last_data_ts = asyncio.get_running_loop().time()
                if desc_fut is not None:
                    try:
                        description = await asyncio.wait_for(
                            desc_fut, timeout=_SCREEN_DESC_TIMEOUT
                        )
                    except Exception:  # noqa: BLE001 - el turno sigue sin contexto
                        description = ""
                if not self._settings.screen_enabled:
                    description = ""
                description = (description or "").strip()
                if description:
                    logger.info(
                        "Nota de pantalla (orden de texto): %s",
                        description[:160],
                    )
                    note = description
                    if len(note) > _SCREEN_NOTE_MAX_CHARS:
                        note = note[:_SCREEN_NOTE_MAX_CHARS]
                    parts.append(
                        types.Part(
                            text=f"(Contexto visual: {note})"
                        )
                    )
                elif self._settings.screen_enabled:
                    # La descripción se canceló (o llegó vacía) y el turno se
                    # envía SIN contexto visual. Antes se mandaba la orden a
                    # pelo y el modelo se respaldaba en lo que "veía": en los
                    # logs llegaba a afirmar tres pantallas incompatibles en
                    # menos de un minuto. Se le dice explícitamente que AHORA no
                    # tiene visión, para que responda de lo que sabe o pida
                    # que le concreten lo que necesita, en vez de adivinar.
                    parts.append(
                        types.Part(
                            text="(Sin contexto visual: en este turno NO se ha "
                            "podido leer la pantalla del usuario, así que no "
                            "puedes afirmar qué se ve ni citarla. Si te preguntan "
                            "por lo que hay en pantalla, dilo con esas palabras "
                            "y pide que lo describan o que repitan la pregunta "
                            "con más detalle. No inventes ventanas, programas "
                            "ni contenido.)"
                        )
                    )
                if math_note:
                    parts.append(types.Part(text=math_note))
                if local_note:
                    parts.append(types.Part(text=local_note))
                parts.append(types.Part(text=cmd))
                # Memoria relevante a ESTA orden (Fase 1). Al conectar la sesión
                # ya se mandó un bloque de memoria, pero ese bloque se armó sin
                # saber qué iba a preguntar el usuario: con el índice de grafo
                # se puede traer solo lo que tiene que ver con la orden, que es
                # lo único que hace que la recuperación por pregunta valga.
                #
                # Solo se añade si es MUCHO más corta que la memoria completa:
                # manda menos contexto, pero sin repetir en cada turno lo que el
                # modelo ya tiene en el system prompt.
                try:
                    bloque_turno = self._build_memory_block(text)
                    if bloque_turno:
                        completa = self._build_memory_block()
                        if len(bloque_turno) <= len(completa) - 200:
                            parts.insert(
                                -1,
                                types.Part(
                                    text="(Recuerdos relacionados con esta "
                                    f"pregunta): {bloque_turno}"
                                ),
                            )
                except Exception as exc:  # noqa: BLE001 - es contexto, nunca crítico
                    logger.debug("No se pudo añadir la memoria del turno: %s", exc)
                # Aviso de contradicción (Fase 2). Va ANTES de la búsqueda web
                # porque manda más que ella: si el usuario acaba de corregir un
                # dato que el modelo iba a usar, ninguna búsqueda de internet lo
                # arregla. Solo se comprueba en órdenes de verdad, no en las de
                # memoria ("olvida X" no puede contradecir nada).
                if not is_reset and local_note is None:
                    try:
                        aviso_disputa = self._detectar_disputa(text)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("No se pudo revisar la memoria del turno: %s", exc)
                        aviso_disputa = ""
                    if aviso_disputa:
                        parts.insert(-1, types.Part(text=aviso_disputa))
                query = explicit_query
                if query is None and classify_fut is not None:
                    try:
                        query = await asyncio.wait_for(
                            classify_fut, timeout=_WEB_CLASSIFY_TIMEOUT
                        )
                    except Exception:  # noqa: BLE001 - sin detector, la orden sigue
                        query = None
                # Nota de búsqueda en internet (solo si la orden la pide y no
                # la resolvió una integración local).
                if query and local_note is None:
                    self._safe_call(
                        self.on_meta, f"(Búsqueda web de '{query[:60]}'…)"
                    )
                    # La búsqueda puede tardar hasta 12 s: base fresca del
                    # watchdog justo antes, para no confundir la espera con un
                    # turno estancado del servidor.
                    self._last_data_ts = asyncio.get_running_loop().time()
                    try:
                        web_note = await asyncio.wait_for(
                            self._web_search(query),
                            timeout=_WEB_SEARCH_TIMEOUT,
                        )
                    except Exception as exc:  # noqa: BLE001 - la orden continúa
                        logger.warning("Búsqueda web interrumpida: %s", exc)
                        web_note = ""
                    web_note = (web_note or "").strip()
                    if (
                        web_note
                        and len(web_note) > self._settings.web_note_max_chars
                    ):
                        web_note = (
                            web_note[: self._settings.web_note_max_chars] + "…"
                        )
                    if web_note:
                        parts.insert(
                            -1,
                            types.Part(
                                text=self._web_note_for(query, web_note)
                            ),
                        )
                        # Los resultados se PUBLICAN en el chat (etiqueta Web),
                        # no solo se dicen en voz: el usuario ve los datos.
                        self._safe_call(self.on_web, web_note)
                    else:
                        self._safe_call(
                            self.on_meta,
                            "(Búsqueda web sin resultados; el modelo responde "
                            "con su propio conocimiento.)",
                        )
                        # Sin datos en línea (p. ej. cuota de grounding 429) se le
                        # pide responder con su conocimiento y avisar de que no
                        # lo verificó, en vez de negarse con "no tengo acceso a
                        # internet".
                        parts.insert(
                            -1,
                            types.Part(
                                text=self._web_fallback_text(query)
                            ),
                        )
                # Instrumentación de rendimiento (Fase 0): cuánto contexto se
                # manda en este turno. Apagada salvo MINDVOICE_PERF=1.
                #
                # Va en su propio try/except a propósito: si la métrica falla,
                # la excepción caía en el `except` del turno, que descarta la
                # orden con un `continue` y el usuario se queda sin respuesta y
                # sin aviso. Medir el contexto nunca puede cortar la
                # conversación.
                # Instrumentación de rendimiento (Fase 0): cuánto contexto se
                # manda en este turno. Apagada salvo MINDVOICE_PERF=1.
                #
                # Va en su propio try/except a propósito: si la métrica falla,
                # la excepción caía en el `except` del turno, que descarta la
                # orden con un `continue` y el usuario se queda sin respuesta y
                # sin aviso. Medir el contexto nunca puede cortar la
                # conversación.
                try:
                    _perf.note_prompt(
                        len(self._build_memory_block()),
                        len(note or "") + len(web_note or "")
                        + len(math_note or "") + len(local_note or ""),
                        notes=int(bool(note)),
                    )
                except Exception as exc:  # noqa: BLE001 - es solo una métrica
                    logger.debug("No se pudo medir el prompt del turno: %s", exc)
                await session.send_client_content(
                    turns=[types.Content(role="user", parts=parts)],
                    turn_complete=True,
                )
                self._awaiting_turn = True
                self._stamp_turn_begin()
                # El turno TEXTO empieza aquí: se reinicia la base del watchdog.
                # Si no, tras unos segundos sin datos (usuario leyendo la
                # respuesta anterior), el watchdog contaba el silencio previo a
                # la orden como "estancamiento" y mataba la sesión justo después
                # de enviarla: el asistente solo respondía a la primera orden.
                self._last_data_ts = asyncio.get_running_loop().time()
                self._set_state(AssistantState.PROCESSING)
                if not is_reset:
                    self._remember("user", text)
            except Exception as exc:  # noqa: BLE001 - la sesión pudo cerrarse a mitad
                logger.warning(
                    "Envío de orden interrumpido (%s); el bucle de órdenes "
                    "sigue vivo para las siguientes.",
                    exc,
                )
                self._awaiting_turn = False
                continue

    # ------------------------------------------------------------------
    # Entrada por voz (micrófono → realtime_input con gate de silencio)
    # ------------------------------------------------------------------
    @staticmethod
    def _voice_tail_chunks(settings) -> int:
        """Número de fragmentos de silencio que cierran un turno de voz."""
        chunk_s = max(0.02, (getattr(settings, "input_chunk_ms", 200) or 200) / 1000.0)
        return max(1, int(round(_VOICE_TAIL_S / chunk_s)))

    async def _send_voice_tail(self, session, settings) -> None:
        """Envía el silencio de cola que el VAD del servidor necesita para dar
        el turno por terminado.

        El gate de micrófono descarta los fragmentos por debajo del umbral, así
        que el audio que llega al servidor es voz sin huecos: el detector de
        actividad no encuentra la pausa final y deja el turno abierto (sin
        transcripción, sin respuesta) hasta que el watchdog recicla la sesión.
        Medido contra la API: sin cola, o con 200 ms, el turno no se cierra;
        con 400-600 ms responde con normalidad. Aquí se envían ``_VOICE_TAIL_S``.
        """
        rate = int(getattr(settings, "input_rate", 16000) or 16000)
        chunk_ms = int(getattr(settings, "input_chunk_ms", 200) or 200)
        silence = b"\x00" * int(rate * chunk_ms / 1000 * 2)
        mime = f"audio/pcm;rate={rate}"
        for _ in range(self._voice_tail_chunks(settings)):
            await session.send_realtime_input(
                audio=types.Blob(data=silence, mime_type=mime)
            )
        logger.debug("Cola de silencio enviada (%.1f s).", _VOICE_TAIL_S)

    async def _activity_start(self, session) -> None:
        """Abre la ventana de actividad del usuario (VAD manual).

        Con ``automatic_activity_detection`` desactivado el servidor SOLO
        procesa el audio que llega dentro de una ventana de actividad, así que
        hay que abrirla explícitamente antes del primer fragmento de voz. Sin
        esto el modelo se quedaría mudo. No hace nada en modo automático, donde
        el servidor abre la ventana por su cuenta.
        """
        if not self._settings.voice_manual_vad:
            return
        await session.send_realtime_input(activity_start=types.ActivityStart())

    async def _activity_end(self, session) -> None:
        """Cierra la ventana de actividad: el servidor da el turno por terminado.

        En modo manual este es el ÚNICO fin de turno, y ocurre al soltar el
        botón de hablar, no por una pausa. No hace nada en modo automático.
        """
        if not self._settings.voice_manual_vad:
            return
        await session.send_realtime_input(activity_end=types.ActivityEnd())

    async def _replay_voice_turn(self, session) -> None:
        """Reenvía en la sesión actual el audio del último turno de voz que el
        servidor dejó sin contestar.

        El fallo era del servidor (se queda el turno sin transcripción ni
        respuesta, sin error ni go_away), así que la recuperación es rehacerlo
        en una sesión recién abierta, que es donde el primer turno sí se
        procesa. Solo se reintenta una vez: si tampoco se contesta, se avisa al
        usuario y se limpia el audio para no entrar en bucle.
        """
        pcm = self._voice_replay
        if not pcm or self.quit_event.is_set():
            return
        self._voice_replay_tries += 1
        self._voice_replay = None
        rate = int(self._voice_replay_rate or 16000)
        chunk = max(1, int(rate * (self._settings.input_chunk_ms or 200) / 1000 * 2))
        silence = b"\x00" * chunk
        mime = f"audio/pcm;rate={rate}"
        seconds = len(pcm) / max(1.0, rate * 2)
        logger.info(
            "Reenviando la frase por voz (%.1f s, intento %d) en la sesión nueva.",
            seconds,
            self._voice_replay_tries,
        )
        self._safe_call(
            self.on_meta,
            f"(MindVoice) No me escuché la primera vez; repito la frase "
            f"({seconds:.1f} s de audio).",
        )
        try:
            # VAD manual: ventana de actividad propia, porque este reenvío va
            # como un turno nuevo y completo.
            await self._activity_start(session)
            for offset in range(0, len(pcm), chunk):
                if self.quit_event.is_set() or self._session is not session:
                    return
                await session.send_realtime_input(
                    audio=types.Blob(
                        data=bytes(pcm[offset:offset + chunk]), mime_type=mime
                    )
                )
                await asyncio.sleep(0.01)
            for _ in range(self._voice_tail_chunks(self._settings)):
                await session.send_realtime_input(
                    audio=types.Blob(data=silence, mime_type=mime)
                )
            await self._activity_end(session)
            await session.send_realtime_input(audio_stream_end=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - la sesión pudo cerrarse
            logger.info("No se pudo reenviar la frase por voz: %s", exc)
            return
        self._awaiting_turn = True
        self._last_data_ts = asyncio.get_running_loop().time()
        self._stamp_turn_begin()
        self._set_state(AssistantState.PROCESSING)
        # Si este reenvío tampoco se contesta, el watchdog lo detecta (ya sin
        # audio guardado) y recicla normalmente, sin reintentos encadenados.
        self._voice_replay = None

    async def _voice_loop(
        self,
        session,
        player: Optional[AudioPlayer],
        screen: Optional[ScreenCapture],
    ) -> None:
        """Transmite el micrófono mientras el usuario tiene la voz activada.

        La pantalla seinjecta como NOTA de TEXTO dentro del turno de voz
        (``_voice_screen_note``), nunca como imagen: la imagen a mitad de un
        turno de audio dejaba la sesión muda. La nota entra con
        ``turn_complete=False`` antes de cerrar el audio, así que el modelo
        responde a la voz ya con el contexto visual, y si la visión no llega a
        tiempo el turno se cierra igualmente (la voz nunca espera a la
        pantalla).
        """
        mic_settings = self._settings
        while not self.quit_event.is_set():
            event = self._voice_event
            if event is None or not event.is_set():
                try:
                    await asyncio.wait_for(event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except Exception:  # noqa: BLE001 - sesión en cierre
                    return
                continue

            # Guardia anti hot-loop: no abrir el micrófono si la sesión no está
            # viva. Reabrir PyAudio en cada reintento (~150 ms) con la sesión
            # caída satura la CPU y producía el tartamudeo/clicks del log de la
            # escucha continua.
            if self._session is None:
                await self._sleep_responsive(0.5)
                continue

            chunk_queue: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=8)
            mic: Optional[MicrophoneCapture] = None
            try:
                mic = MicrophoneCapture(
                    rate=mic_settings.input_rate,
                    chunk_ms=mic_settings.input_chunk_ms,
                    device_name=mic_settings.mic_device_name,
                )
                mic.start(chunk_queue, asyncio.get_running_loop())
                if not (self._responding or self._awaiting_turn):
                    self._set_state(AssistantState.LISTENING)
            except OSError as exc:
                logger.error("Micrófono no disponible: %s", exc)
                self._safe_call(self.on_meta, f"(Micro) {exc}")
                self._set_state(AssistantState.ERROR)
                await self._sleep_responsive(3.0)
                continue

            floor_rms: Optional[float] = None
            gate = _VOICE_GATE_MIN
            preroll = 0
            session_max = 0.0
            sent = 0
            sent_total = 0
            dropped = 0
            send_failed = False
            voice_started = False
            # El silencio local solo corta el turno si ``auto_close_s`` dice que
            # sí (se calcula justo debajo): en escucha continua con VAD manual,
            # una pausa al pensar NO puede cerrar la frase.
            manual_vad = bool(self._settings.voice_manual_vad)
            # Silencio tras el cual se cierra el turno por su cuenta:
            #   - VAD automático: 1,0 s, el comportamiento de siempre.
            #   - VAD manual + turno abierto a pulsación: ``voice_toggle_silence``
            #     (2,5 s por defecto). Antes NO se cerraba nunca y el usuario
            #     pulsaba, hablaba y no pasaba nada hasta pulsar por segunda
            #     vez, sin ningún aviso. El HUD dice "suelta el botón" y en
            #     toggle soltar no hace nada: era un callejón sin salida.
            #   - VAD manual + escucha continua: nunca se cierra solo, que es
            #     justo para lo que existe el VAD manual (hablar indefinido).
            auto_close_s: Optional[float] = None
            if not manual_vad:
                auto_close_s = _VOICE_SEGMENT_SILENCE_S
            elif self._voice_toggle and float(self._settings.voice_toggle_silence) > 0:
                auto_close_s = float(self._settings.voice_toggle_silence)
            # Cierre de frase por silencio LOCAL (con el micrófono abierto):
            # momento del último fragmento por encima del gate y si el turno
            # actual ya se cerró solo esperando la respuesta del modelo.
            last_loud_ts: Optional[float] = None
            segment_closed = False
            # Descripción de pantalla en vuelo: se lanza al abrirse el
            # micrófono y se inyecta como nota ANTES de cerrar el turno de
            # audio, para que "¿qué ves en pantalla?" funcione hablando.
            desc_fut: Optional[asyncio.Future] = None
            note_sent = False
            # Reutiliza la descripción que ya esté en vuelo (el precalentado
            # del texto). Lanzar una segunda sesión de visión solo encadenaría
            # detrás por ``_describe_lock`` y el turno se quedaría sin
            # contexto visual por el doble de espera.
            if (
                self._desc_prefetch_task is not None
                and not self._desc_prefetch_task.done()
            ):
                desc_fut = self._desc_prefetch_task
                desc_fut_own = False
            elif screen is not None:
                try:
                    desc_fut = asyncio.ensure_future(
                        self._describe_screen(screen)
                    )
                    desc_fut_own = True
                except RuntimeError:
                    desc_fut, desc_fut_own = None, False
            else:
                desc_fut_own = False
            # Copia de lo enviado (con tope) por si este turno se queda sin
            # respuesta y hay que reenviarlo en una sesión nueva.
            buffer = bytearray()
            try:
                while (
                    not self.quit_event.is_set()
                    and self._voice_event is not None
                    and self._voice_event.is_set()
                ):
                    now = asyncio.get_running_loop().time()
                    # Frase cerrada por silencio local: se entrega al modelo y,
                    # mientras conteste, el micrófono se queda escuchando SIN
                    # enviar (si siguiera mandando audio, el turno volvería a
                    # quedar abierto y el modelo no respondería).
                    if segment_closed and not (
                        self._awaiting_turn or self._responding
                    ):
                        segment_closed = False
                    if not segment_closed and not send_failed:
                        # Una pausa al pensar NO debe cerrar la frase: por eso el umbral
                        # del VAD automático es de 1 s y el de toggle 2,5 s, y
                        # en escucha continua el silencio no cierra nada. Aquí
                        # es donde se colgaba antes: con 1 s la IA contestaba a
                        # mitad de lo que el usuario iba a decir.
                        if (
                            auto_close_s is not None
                            and sent > 0
                            and last_loud_ts is not None
                            and now - last_loud_ts >= auto_close_s
                        ):
                            try:
                                if not note_sent:
                                    await self._voice_screen_note(session, desc_fut)
                                    note_sent = True
                                await self._send_voice_tail(session, mic_settings)
                                await session.send_realtime_input(
                                    audio_stream_end=True
                                )
                                logger.info(
                                    "Silencio de %.1f s con el micrófono abierto: "
                                    "turno de voz cerrado (%.1f s de audio).",
                                    now - last_loud_ts,
                                    len(buffer)
                                    / max(
                                        1.0,
                                        mic_settings.input_rate * 2,
                                    ),
                                )
                                self._awaiting_turn = True
                                self._last_data_ts = now
                                self._set_state(AssistantState.PROCESSING)
                                self._voice_replay = bytes(buffer)
                                self._voice_replay_rate = int(
                                    mic_settings.input_rate
                                )
                                self._voice_replay_tries = 0
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "Cierre de turno de voz fallido: %s", exc
                                )
                                self._clear_turn_state()
                                send_failed = True
                            finally:
                                # Siguiente frase: contadores y umbral del gate
                                # se recalculan desde cero.
                                segment_closed = True
                                sent = 0
                                voice_started = False
                                buffer = bytearray()
                                floor_rms = None
                                preroll = 0
                                gate = _VOICE_GATE_MIN
                                last_loud_ts = None
                    try:
                        chunk = await asyncio.wait_for(
                            chunk_queue.get(), timeout=0.1
                        )
                    except asyncio.TimeoutError:
                        continue
                    rms = self._rms_level(chunk)
                    if rms is None:
                        continue
                    session_max = max(session_max, rms)
                    if self.on_voice_level is not None:
                        self._safe_call(self.on_voice_level, rms)
                    if segment_closed:
                        # Micrófono abierto a la espera de que conteste: el audio
                        # se descarta (el gate se recalcula igualmente para no
                        # anclar el suelo en el ruido de la habitación).
                        if floor_rms is None or rms < floor_rms:
                            floor_rms = rms
                        dropped += 1
                        continue
                    if floor_rms is None or rms < floor_rms:
                        floor_rms = rms
                    if preroll < _VOICE_GATE_PREROLL:
                        # Pre-escucha: el gate queda en el mínimo absoluto mientras
                        # se mide el suelo de ruido con los primeros fragmentos.
                        preroll += 1
                        gate = _VOICE_GATE_MIN
                    else:
                        # El suelo NUNCA se ancla en la voz: si el usuario ya está
                        # hablando al abrirse el micrófono, el suelo medido es
                        # grande y sin tope tumbaría todas las frases siguientes.
                        gate = max(
                            _VOICE_GATE_MIN,
                            min(floor_rms or 0.0, _VOICE_GATE_FLOOR_MAX) * 2.0,
                        )
                    if rms >= gate:
                        if not voice_started:
                            voice_started = True
                            # Barge-in del usuario: se calla el altavoz local y
                            # se limpia el estado, pero NO se manda
                            # ``_INTERRUPT_TEXT``. El propio servidor corta la
                            # respuesta al detectar voz nueva (``interrupted``) y
                            # ese corte llega solo. Mandar el texto de
                            # interrupción aquí era contraproducente: con
                            # ``_awaiting_turn`` (el modelo solo procesando lo
                            # que acabas de decir) la sesión quedaba MUERTA
                            # (sin transcripción ni respuesta, sin error) y el
                            # modelo llegó a contestar "Entendido, paro aquí"
                            # consumiendo tu frase.
                            if player is not None:
                                player.flush()
                            self._responding = False
                            self._awaiting_turn = False
                            self._outdone_ts = None
                            self._out_acc.clear()
                            self._in_acc.clear()
                            self._turn_text = ""
                            self._cont_remaining = _CONTINUE_MAX_ROUNDS
                            self._stamp_turn_begin()
                            self._set_state(AssistantState.LISTENING)
                            self._last_data_ts = asyncio.get_running_loop().time()
                            # VAD manual: se abre la ventana de actividad del
                            # usuario (barge-in) para que el servidor procese el
                            # audio que viene a continuación.
                            await self._activity_start(session)
                        try:
                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=chunk,
                                    mime_type=(
                                        f"audio/pcm;rate={mic_settings.input_rate}"
                                    ),
                                )
                            )
                            sent += 1
                            sent_total += 1
                            self._voice_sending = True
                            last_loud_ts = asyncio.get_running_loop().time()
                            self._voice_last_audio_ts = last_loud_ts
                            cap = int(
                                _VOICE_REPLAY_MAX_S
                                * mic_settings.input_rate
                                * mic_settings.input_chunk_ms
                                / 1000
                                * 2
                            )
                            if len(buffer) + len(chunk) <= cap:
                                buffer += chunk
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("Envío de voz interrumpido: %s", exc)
                            self._clear_turn_state()
                            send_failed = True
                            break
                    else:
                        dropped += 1
                        # Segmento cerrado o gate cerrado: no sale tráfico, así que
                        # el watchdog tiene que poder notar el estancamiento.
                        self._voice_sending = False
                if sent > 0 and not send_failed:
                    try:
                        # Contexto visual primero (turno de audio todavía
                        # abierto), luego la cola de silencio que cierra el turno.
                        if not note_sent:
                            await self._voice_screen_note(session, desc_fut)
                            note_sent = True
                        # Cola de silencio: el VAD del servidor cierra el turno
                        # por la pausa que oye, y el gate del micrófono se lleva
                        # los silencios. Sin esta cola, las frases cortas
                        # ("hola") llegaban al servidor sin final reconocible y
                        # el turno se quedaba abierto sin responder nunca.
                        await self._send_voice_tail(session, mic_settings)
                        # VAD manual: se cierra la ventana de actividad del
                        # usuario. Este es el cierre real del turno y ocurre al
                        # SOLTAR el botón, no por una pausa. En modo automático
                        # no se manda y el cierre lo decide el servidor.
                        await self._activity_end(session)
                        await session.send_realtime_input(audio_stream_end=True)
                        self._awaiting_turn = True
                        self._last_data_ts = asyncio.get_running_loop().time()
                        self._set_state(AssistantState.PROCESSING)
                        # Audio guardado por si este turno se queda sin
                        # respuesta (el watchdog lo reenvía en sesión nueva).
                        self._voice_replay = bytes(buffer)
                        self._voice_replay_rate = int(mic_settings.input_rate)
                        self._voice_replay_tries = 0
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Fin de turno de voz interrumpido: %s", exc)
                        self._clear_turn_state()
                        send_failed = True
                elif sent == 0 and not segment_closed:
                    logger.info("Sesión de voz sin voz detectable; turno no enviado.")
                    self._voice_replay = None
                    if not self._voice_active() and not self._responding:
                        self._set_state(AssistantState.IDLE)
            finally:
                self._voice_sending = False
                if desc_fut is not None and desc_fut_own and not desc_fut.done():
                    desc_fut.cancel()
                safely_close = mic.close if mic is not None else None
                if safely_close is not None:
                    try:
                        safely_close()
                    except Exception:  # noqa: BLE001
                        pass
                self._drain_chunks(chunk_queue)
                if session_max > 0:
                    logger.info(
                        "Sesión de voz: pico RMS %.0f (%d enviados, %d "
                        "descartados, gate %.0f)",
                        session_max,
                        sent_total,
                        dropped,
                        gate,
                    )
                if self.on_voice_level is not None:
                    self._safe_call(self.on_voice_level, 0.0)
            if send_failed:
                # Evita reabrir el micrófono en bucle inmediato tras un fallo de
                # envío (sesión en cierre): pequeño enfriamiento antes de volver.
                if self._voice_event is not None:
                    self._voice_event.clear()
                    self._voice_opened_ts = None
                await self._sleep_responsive(1.0)
            self._maybe_reconnect()
            # Vuelta al bucle: espera a que se vuelva a activar (o sesión cerrada)

    @staticmethod
    def _clean_web_query(raw: str) -> str:
        """Limpia la consulta extraída tras un disparador ("dime cuánto vale el
        café" → "café"). Solo descarta conectores INTRODUCTORIOS, no palabras
        del contenido: la parte sustancial de la frase se conserva intacta."""
        text = (raw or "").strip().strip("\"'").strip("¿?¡!.:;…")
        if not text:
            return ""
        words = text.split()
        i = 0
        while i < len(words):
            bigram = " ".join(words[i:i + 2]).lower().rstrip("¿?¡!.,:;")
            unigram = words[i].lower().rstrip("¿?¡!.,:;")
            if bigram in _WEB_BIGRAM_FILLERS:
                i += 2
            elif unigram in _WEB_UNIGRAM_FILLERS:
                i += 1
            else:
                break
        return " ".join(words[i:]).strip()

    @staticmethod
    def _tail_words(text: str, max_words: int = 9) -> str:
        """Recorta a las últimas palabras: el sujeto real suele estar al final
        ("¿Cuáles son todos los materiales del escudo de Ankh en Terraria?" →
        "materiales del escudo de Ankh en Terraria")."""
        words = (text or "").split()
        if len(words) <= max_words:
            return (text or "").strip()
        return " ".join(words[-max_words:]).strip()

    @staticmethod
    def _trim_query(text: str) -> str:
        """Ajusta la consulta a un tamaño razonable (cola de la frase)."""
        text = (text or "").strip()
        if len(text) <= _WEB_MAX_QUERY_LEN:
            return text
        return LiveAssistant._tail_words(text, 12)[:_WEB_MAX_QUERY_LEN].rstrip()

    @staticmethod
    def _concrete_score(clause: str) -> int:
        """Puntúa una cláusula según sirva como consulta de búsqueda.

        El fallo que motivó esto: la última cláusula de una frase hablada suele
        ser conversacional ("...como tú, por ejemplo, un agente de IA") y se
        llevaba la búsqueda, mientras el sujeto de verdad ("OpenCog", una cifra,
        un nombre propio) quedaba en una cláusula anterior y se perdía. Ahora
        se puntúa cada cláusula y se gana la más CONCRETA, no la última.
        """
        words = [
            w.strip(".,;:()[]\"'¿?!-–—")
            for w in (clause or "").split()
            if w.strip(".,;:()[]\"'¿?!-–—")
        ]
        if not words:
            return -99
        score = 0
        # Nombre propio o sigla en medio de la frase: el mejor sujeto posible.
        for w in words[1:]:
            if w[:1].isupper() and len(w) > 2 and w.lower() not in _WEB_JUNK_WORDS:
                score += 4
                break
        # Cifras: años, medidas, precios (muy buscables).
        if any(any(ch.isdigit() for ch in w) for w in words):
            score += 3
        # Ruido de segunda persona: "como tú", "por ejemplo", "para ti"...
        ruido = ("como tu", "como tú", "por ejemplo", "para ti", "para vos",
                 "tal cual", "lo mismo", "nuevos programas", "sistemas de")
        bajo = clause.lower()
        score -= sum(4 for r in ruido if r in bajo)
        # Longitud razonable: ni una palabra suelta ni un párrafo.
        n = len(words)
        if n < 3:
            score -= 5
        elif n > 14:
            score -= 3
        return score

    # Conectores por los que se parte una frase hablada sin puntuación. El
    # texto por voz casi nunca trae "¿...?" ni comas, así que sin esto la
    # consulta era la frase entera y el recorte final se comía el sujeto.
    _WEB_SPLIT_TOKENS = (
        " y ", " o ", " pero ", " entonces ", " ademas ", " además ",
        " ahora ", " dime ", " dime ", " busca ", " busca ", " quiero ",
        " necesito ", " puedes ", " podeas ", " seria ", " sería ",
        ",", ";", " por ejemplo ", " por ejemplo, ",
    )

    @classmethod
    def _candidates(cls, clause: str) -> list:
        """Consultas candidatas de una cláusula: entera, por trozos y con
       adh adjoining.

        "busca cómo integrar Leprechaun con una API de Python para automatizar
        el navegador" → la entera, "busca como integrar Leprechaun", "con una
        API de Python...", y las mixtures de dos trozos contiguos.
        """
        partes = [clause]
        for tok in cls._WEB_SPLIT_TOKENS:
            nuevas = []
            for p in partes:
                nuevas.extend(p.split(tok))
            partes = [x.strip() for x in nuevas if x and x.strip()]
        cands = []
        for p in partes:
            cands.append(p)
        # Uniones de trozos contiguos: el sujeto y su modificador suelen
        # quedar en dos trozos ("integrar Leprechaun" + "con una API").
        for i in range(len(partes) - 1):
            cands.append(f"{partes[i]} {partes[i + 1]}")
        return [c for c in cands if c]

    @classmethod
    def _best_subject(cls, text: str) -> str:
        """Extrae la consulta web más útil del texto previo al disparador.

        El fallo medido: se cogía la última cláusula y se recortaba a 9
        palabras, así que en una frase hablada (sin signos de puntuación) ganaba
        la coletilla conversacional y se perdía el sujeto real. Caso del log:
        "OpenCog API para agentes de IA, y dime integraciones para nuevos
        programas como tú" devolvía "IA, y dime integraciones para nuevos
        programas como tú" y la búsqueda no encontraba nada de OpenCog.
        """
        raw = (text or "").strip()
        if not raw:
            return ""
        clauses = []
        for chunk in raw.split("?"):
            for sub in chunk.split("!"):
                for seg in sub.split("."):
                    clause = cls._clean_web_query(seg)
                    if len(clause.split()) >= 3 and not cls._junk_clause(clause):
                        clauses.append(clause)
        if not clauses:
            return cls._tail_words(cls._clean_web_query(raw), max_words=9)
        # Todas las cláusulas y todos sus trozos se puntúan juntos: gana la
        # más concreta, no la última ni la más larga.
        mejor, mejor_score = "", -10**6
        for clause in clauses:
            for cand in cls._candidates(clause):
                score = cls._concrete_score(cand)
                if score > mejor_score or (score == mejor_score and len(cand) > len(mejor)):
                    mejor, mejor_score = cand, score
        return cls._trim_query(mejor)

    @staticmethod
    def _junk_clause(clause: str) -> bool:
        """¿Es la cláusula una muletilla sin contenido ("si no sabes", "si
        puedes") que no aporta nada a una búsqueda?"""
        words = [
            w.strip(".,;:()[]\"'¿?!-_")
            for w in (clause or "").lower().split()
            if w.strip(".,;:()[]\"'¿?!-_")
        ]
        if not words:
            return True
        junk = sum(1 for w in words if w in _WEB_JUNK_WORDS)
        return len(words) <= 5 and junk >= len(words) - 1

    @staticmethod
    def _web_query(text: str) -> Optional[str]:
        """Devuelve la consulta web si la orden pide información de internet.

        ``None`` si no hay intención web (el comando se envía sin nota). La
        extracción usa la parte de la frase tras el disparador; cuando el
        disparador cerró la frase ("...búscalo en la web"), el tema se toma de
        la parte anterior recortando a sus últimas palabras (el sujeto suele
        estar al final) y acotando la longitud para no enviar la frase entera.
        """
        text = (text or "")
        if not text.strip():
            return None
        lowered = text.lower()
        # El clima es un dato ACTUAL por definición, así que cualquier orden que
        # hable del tiempo entra por la vía rápida, sin depender del detector
        # inteligente por IA (que dependía de una llamada REST sujeta a cuota y
        # fallaba constantemente: wttr.in, que es gratis e instantáneo, no se
        # llegaba a consultar NI UNA VEZ). Se comprueba antes que los
        # disparadores para que "dame el clima" no necesite decir "busca en
        # internet"; ``_web_search_impl`` enruta luego a wttr.in.
        if _WEATHER_RE.search(text):
            return LiveAssistant._trim_query(text) or text[:_WEB_MAX_QUERY_LEN]
        if lowered.startswith("/web "):
            cleaned = LiveAssistant._clean_web_query(text[5:])
            return LiveAssistant._trim_query(cleaned) or text[5:].strip()[: _WEB_MAX_QUERY_LEN] or None
        mejor_idx, mejor_trigger, mejor_candidate = 10**9, "", ""
        hubo_trigger = False
        for trigger in _WEB_TRIGGERS:
            # LÍMITES DE PALABRA. Antes se usaba ``lowered.find(trigger)``, que
            # busca una SUBCADENA: el disparador "investiga" casaba DENTRO de
            # "investigación" (pos. 317) y cortaba la palabra por la mitad,
            # dejando la consulta "ción con integración de código…". Ahora solo
            # casa como palabra completa, así que "busca" no casa en "buscame" y
            # "investiga" no casa en "investigación".
            m = _WEB_TRIGGER_RE(trigger).search(lowered)
            if m is None:
                continue
            # Gana el disparador que aparece PRIMERO en la frase, no el primero
            # de la lista: antes "investiga" (pos. 317) ganaba a "en internet"
            # (pos. 19) solo por estar antes en la tupla, y la consulta salía de
            # la mitad equivocada del enunciado. A igual posición, el más largo.
            if m.start() > mejor_idx:
                continue
            idx = m.start()
            # A igual posición gana el disparador MÁS largo ("busca en internet"
            # sobre "busca"): es el que aporta más contexto de la orden.
            if idx == mejor_idx and len(trigger) <= len(mejor_trigger):
                continue
            before = text[:idx].strip()
            after = text[idx + len(trigger):].strip()
            cleaned_after = LiveAssistant._clean_web_query(after)
            if len(cleaned_after.split()) > 12:
                # El disparador abre la frase y lo que sigue es la ORACIÓN
                # ENTERA, no la consulta ("en internet si la IA Open Code…"): con
                # 60+ palabras, recortar la cola pierde el sujeto de verdad. Se
                # puntúan las cláusulas y se gana la más concreta.
                subject = LiveAssistant._best_subject(cleaned_after)
                if len(subject.split()) >= 3:
                    cleaned_after = subject
            if len(cleaned_after.split()) >= 3:
                # Tras el disparador va la consulta completa ("busca en internet
                # el precio de la leche" → "el precio de la leche").
                candidate = cleaned_after
            else:
                # El disparador está incrustado o cerró la frase ("búscalo en la
                # web"): el tema se toma de la parte anterior y, si no aporta,
                # de la frase completa, siempre recortado a su cláusula sustancial.
                subject = LiveAssistant._best_subject(before)
                if len(subject.split()) < 3:
                    subject = LiveAssistant._best_subject(text)
                if len(subject.split()) < 3:
                    subject = cleaned_after or subject
                candidate = subject
            candidate = LiveAssistant._trim_query(candidate)
            if candidate:
                # Se guarda el mejor y se sigue mirando: puede haber otro
                # disparador más temprano en la frase.
                mejor_idx, mejor_trigger, mejor_candidate = idx, trigger, candidate
                continue
            hubo_trigger = True
        if mejor_candidate:
            return mejor_candidate
        if hubo_trigger and text.strip():
            # Casó un disparador pero no salió una consulta usable: antes se
            # mandaba la frase entera recortada, que al menos tiene algo.
            return text.strip()[:_WEB_MAX_QUERY_LEN]
        return None

    @staticmethod
    def _looks_searchable(text: str) -> bool:
        """¿Merece la pena preguntar al detector de búsqueda web?

        Evita una llamada de IA extra (latencia + coste) en saludos y frases
        cortas ("hola", "adelante"): solo se clasifican las de al menos 3
        palabras, que es donde puede esconderse un dato que pide internet.
        """
        return len((text or "").split()) >= 3

    async def _web_model_name(self) -> Optional[str]:
        """Devuelve un modelo REST utilizable para buscar/clasificar (en caché).

        Primero usa ``web_search_model`` si está configurado por el usuario;
        si responde, se recuerda para toda la sesión. Si no, recorre la lista
        real de modelos que devuelve el API (``models.list``) eligiendo los de
        texto que no son Live, y por último los nombres de reserva. Nunca
        lanza: si nada funciona devuelve ``None`` y la orden continúa sin nota.
        Concurrente y acotado: si dos llamadas la lanzan a la vez (p. ej. el
        pre-warm contra la primera orden), la segunda espera a la primera en
        vez de repetir el baile de probes.
        """
        if self._web_model is not None:
            return self._web_model
        if self._web_model_exhausted:
            return None

        async def _resolve() -> None:
            def _push(candidate: str) -> None:
                if candidate and candidate not in candidates:
                    candidates.append(candidate)

            candidates: list = []
            try:
                tried = False
                if self._settings.web_search_model:
                    _push(self._settings.web_search_model)
                else:
                    # Modelo ya resuelto en una ejecución anterior: se reutiliza
                    # SIN hacer probe. Es lo que evita el baile de 5-6 llamadas a
                    # la API (cuatro con 429) en cada reinicio del watchdog. Si
                    # el modelo guardado dejara de responder, las búsquedas
                    # muestran su propio aviso y la siguiente resolución vuelve
                    # a probarlo.
                    cached = _load_cached_web_model()
                    if cached:
                        logger.info("Modelo web de la caché: %s", cached)
                        self._web_model = cached
                        # La caché evita 5-6 llamadas en cada arranque, pero se
                        # queda obsoleta si cambias de clave, de plan o de
                        # versión. Decirlo es la diferencia entre "mi clon suena
                        # peor" y "sé por qué".
                        if cached != WEB_SEARCH_MODEL:
                            self._safe_call(
                                self.on_meta,
                                f"(Web) Búsqueda con «{cached}» (modelo guardado "
                                f"en web_model_cache.json, NO el recomendado "
                                f"«{WEB_SEARCH_MODEL}»): si notas las respuestas "
                                "peores que antes, borra ese archivo y reinicia.",
                            )
                        return
                # Candidatos "curados" PRIORITARIOS: nombres que en 2026 siguen
                # respondiendo en este plan (los ``-2.5-flash``/``-3.1-flash``
                # dan 404 "no longer available to new users").
                for fallback in _WEB_MODEL_FALLBACKS:
                    _push(fallback)
                # Después, los nombres REALES que devuelve el API (descubrir
                # variantes nuevas que todavía no estén en la lista). OJO:
                # ``models.list()`` es una CORUTINA que devuelve un
                # ``AsyncPager``: hay que esperarla antes de iterar, si no se
                # "itera" la corutina sin consumirla (warning + 0 candidatos).
                try:
                    pager = await self._client.aio.models.list()
                    async for entry in pager:
                        name = getattr(entry, "name", None)
                        low = (name or "").lower()
                        if any(
                            token in low
                            for token in (
                                "live",
                                "embedding",
                                "audio",
                                "tts",
                                "image",
                                "veo",
                                "lyria",
                                "robotics",
                                "transcribe",
                                "computer-use",
                                "antigravity",
                                "deep-research",
                                "customtools",
                                "gaos",
                                "aqa",
                                "nano-banana",
                                "tunedmodel",
                            )
                        ):
                            continue
                        _push(str(name).rsplit("/models/", 1)[-1])
                except Exception as exc:  # noqa: BLE001 - se usan los nombres curados
                    logger.debug("No se pudo listar los modelos web: %s", exc)
                loop = asyncio.get_running_loop()
                deadline = loop.time() + _WEB_MODEL_RESOLVE_DEADLINE
                network_failed = False
                for name in candidates[: _WEB_MODEL_MAX_PROBES]:
                    tried = True
                    if loop.time() >= deadline:
                        break
                    try:
                        response = await asyncio.wait_for(
                            self._client.aio.models.generate_content(
                                model=name, contents="ping"
                            ),
                            timeout=_WEB_MODEL_PROBE_TIMEOUT,
                        )
                        # Solo sirve un modelo que devuelva TEXTO: los
                        # *-live-preview / *-native-audio responden (inline_data,
                        # audio) y luego la búsqueda devolvería "" siempre.
                        text = (getattr(response, "text", None) or "").strip()
                        if not text:
                            logger.debug(
                                "Modelo web sin texto de respuesta (%s): se sigue.",
                                name,
                            )
                            continue
                        self._web_model = name
                        _save_cached_web_model(name)
                        logger.info("Modelo web resuelto: %s", name)
                        # Degradación visible: si el pin documentado no está
                        # disponible para esta clave/plan, se avisa en pantalla.
                        # Antes solo quedaba en el log y el usuario no tenía
                        # forma de saber por qué su clon respondía peor.
                        if name != WEB_SEARCH_MODEL:
                            self._safe_call(
                                self.on_meta,
                                f"(Web) Tu API no sirve «{WEB_SEARCH_MODEL}» para "
                                f"búsqueda; se usa «{name}» como reserva"
                                + (
                                    " (más barato: puede responder peor)."
                                    if "lite" in name
                                    else "."
                                )
                                + " Si quieres el de serie, revisa tu plan o "
                                "pon web_search_model en Ajustes.",
                            )
                        return
                    except Exception as exc:  # noqa: BLE001 - siguiente candidato
                        if _is_network_error(exc):
                            # FALLO TRANSITORIO de red (DNS, Wi-Fi, timeout): el
                            # modelo puede existir perfectamente; NO se marca la
                            # resolución como agotada, se reintentará en la
                            # siguiente búsqueda/clasificación.
                            network_failed = True
                            logger.debug(
                                "Modelo web no accesible por red (%s): %s", name, exc
                            )
                            continue
                        logger.debug("Modelo web no usable (%s): %s", name, exc)
                if tried:
                    if network_failed:
                        logger.warning(
                            "Modelo web no resuelto ahora (fallos de red "
                            "temporales): se reintentará en la siguiente "
                            "búsqueda."
                        )
                        self._safe_call(
                            self.on_meta,
                            "(Web) No se pudo comprobar el modelo de búsqueda "
                            "(problema de red, no de clave): la búsqueda web "
                            "puede ir a ciegas hasta que vuelva la conexión.",
                        )
                        return
                    logger.warning(
                        "Ningún modelo REST respondió (búsqueda web desactivada "
                        "esta sesión). Configura 'web_search_model' con uno "
                        "válido."
                    )
                    self._safe_call(
                        self.on_meta,
                        "(Web) Ningún modelo respondió para buscar: la búsqueda "
                        "web queda DESACTIVADA esta sesión. Suele ser el plan de "
                        "tu clave. Revisa GEMINI_API_KEY o pon "
                        "web_search_model en Ajustes.",
                    )
                    self._web_model_exhausted = True
            except Exception as exc:  # noqa: BLE001 - sin búsqueda, la app sigue igual
                logger.warning("Resolución del modelo web falló: %s", exc)
                self._safe_call(
                    self.on_meta,
                    f"(Web) La resolución del modelo de búsqueda falló: {exc}",
                )
                # Un fallo de RED no agota la resolución: la red puede volver.
                if not _is_network_error(exc):
                    self._web_model_exhausted = True

        fut = self._web_resolve_fut
        if fut is not None:
            await fut
            return self._web_model
        self._web_resolve_fut = asyncio.ensure_future(_resolve())
        try:
            await self._web_resolve_fut
        finally:
            self._web_resolve_fut = None
        return self._web_model

    async def _classify_web_query(self, text: str) -> Optional[str]:
        """Detector inteligente: decide si la orden pide datos actuales.

        Se usa como complemento de ``_web_query`` cuando la frase no lleva un
        disparador explícito ("¿quién ganó anoche?"). Devuelve la consulta
        limpia, o ``None`` si la pregunta se responde sin internet. Nunca lanza
        y nunca se queda colgada: un fallo del detector equivale a "sin
        búsqueda".
        """
        model = self._settings.web_search_model
        if model is None:
            model = await self._web_model_name()
        if not model:
            logger.debug("No hay modelo REST: se omite el detector web.")
            return None
        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=model,
                    contents=_WEB_CLASSIFY_PROMPT.format(
                        text=(text or "")[:400]
                    ),
                ),
                timeout=_WEB_CLASSIFY_TIMEOUT,
            )
            answer = (getattr(response, "text", None) or "").strip().strip("\"'")
            if not answer or answer.upper() == "NO":
                return None
            answer = " ".join(answer.split())
            if len(answer) > _WEB_MAX_QUERY_LEN:
                logger.debug(
                    "Detector web devolvió un texto sin forma de consulta "
                    "(se ignora): %s…",
                    answer[:80],
                )
                return None
            logger.info("Detector web: '%s' → consulta '%s'", text[:80], answer[:80])
            return answer
        except Exception as exc:  # noqa: BLE001 - sin detector, la app sigue igual
            self._invalidate_web_model_on_quota(exc)
            logger.debug("Detector web no disponible: %s", exc)
            return None

    def _invalidate_web_model_on_quota(self, exc: Exception) -> None:
        """Olvida el modelo web cacheado si el fallo es de cuota (429).

        El modelo resuelto se reutiliza SIN probe durante 7 días (``_load_cached_
        web_model``) y nunca se descartaba: si ese modelo se queda sin cuota,
        TODAS las búsquedas fallaban con 429 durante días, con la web "activada"
        pero muda y sin que nada lo indicara. Aquí se suelta (en memoria y en
        disco) para que la siguiente resolución vuelva a probar la lista de
        candidatos y encuentre uno que aún tenga cuota.

        No se toca si el modelo lo fijó el usuario en los ajustes: ahí manda su
        elección y solo él puede cambiarla.
        """
        if not _is_quota_error(exc):
            return
        if self._settings.web_search_model:
            return
        if self._web_model is None:
            return
        logger.warning(
            "El modelo web cacheado (%s) devolvió error de cuota: se descarta "
            "y se volverá a resolver.",
            self._web_model,
        )
        self._web_model = None
        _clear_cached_web_model()

    def _web_note_for(self, query: str, web_note: str) -> str:
        """Formatea la nota de búsqueda de internet que se inyecta al turno.

        Pide al modelo que use los datos HABLANDO (la nota entra en la sesión
        Live de voz, así el contenido buscado también se escucha, no solo se ve).
        El motor se nombra aquí, en el propio turno, y sale de
        ``current_engine()``; por eso no puede desincronizarse de lo que se usó.
        """
        return (
            f"(Resultado de búsqueda en internet sobre '{query[:80]}' "
            f"[motor: {self.engine_label()}]: "
            f"{web_note}. Al responder, MENCIONA EN VOZ ALTA que has buscado en "
            f"{self.engine_spoken()}: dilo con tus palabras al empezar, breve y "
            "natural, y después da los datos. Responde en voz, con tus palabras: "
            "no leas la nota literalmente ni la repitas textualmente.)"
        )

    def engine_label(self) -> str:
        """Nombre del motor para la nota (como lo ve el modelo)."""
        return _ENGINE_LABELS[self.current_engine()]

    def engine_spoken(self) -> str:
        """Cómo se dice el motor en voz alta (nombre corto, sin paréntesis)."""
        return _ENGINE_SPOKEN[self.current_engine()]

    def _web_fallback_text(self, query: str) -> str:
        """Turno de repuesto cuando una búsqueda pedida por voz quedó vacía.

        Antes esta instrucción pedía "responde con tu propio conocimiento lo
        mejor que puedas", y el resultado era una parrafada segura e INVENTADA
        sobre el tema (caso real: Leprechaun, "marco muy flexible diseñado para
        crear sistemas de IA avanzados", todo falsehood, con un aviso breve
        antes de que el usuario se lo creyera). Pedir conocimiento sin marcarlo
        como dudoso es exactamente lo que produce fabricaciones.

        Ahora se le exige que diga en dos líneas que no tiene datos frescos, sin
        inventar datos concretos (cifras, fechas, nombres de API, versiones), y
        que ofrezca seguir o reintentar.
        """
        return (
            f"(El usuario pidió información de internet sobre '{query[:80]}', "
            "pero la búsqueda en línea NO pudo completarse: no hay datos "
            "verificados de este turno. Responde en DOS frases, en voz: 1) di "
            "con claridad que ahora mismo no tienes datos frescos de internet y "
            "que lo anterior viene de tu memoria, que puede estar equivocado o "
            "desactualizado; 2) NO inventes datos concretos (cifras, fechas, "
            "versiones, nombres de funciones o de API, enlaces). Si de verdad no "
            "recuerdas el tema, di que no lo recuerdas y ofrece reintentar la "
            "búsqueda. No rellenes con suposiciones presentadas como hechos.)"
        )

    @staticmethod
    def _provider_key(settings, name: str) -> Optional[str]:
        """Clave configurada de un motor, según el registro.

        El nombre del atributo sale de ``WEB_ENGINES`` (no de un f-string
        inventado aquí), y un motor sin clave declarada devuelve ``None``.
        """
        field_name = engine_key_field(name)
        if not field_name:
            return None
        return (getattr(settings, field_name, None) or "").strip() or None

    def _provider_geo_blocked(self, provider: str) -> bool:
        """¿Este motor está vetado por país para la IP de salida?

        Se recuerda en caliente para no repetir el 403 en cada búsqueda: sin
        esto, una clave puesta y bloqueada por el país gastaba un viaje
        perdido en cada consulta.
        """
        return provider in self._provider_geo_blocked_set

    def _note_geo_block(self, provider: str) -> None:
        """Registra el bloqueo por pais y lo dice una sola vez, sin culpar a la clave."""
        if provider in self._provider_geo_blocked_set:
            return
        self._provider_geo_blocked_set.add(provider)
        logger.error(
            "Motor '%s' bloqueado por pais (403 sin JSON): se desactiva en "
            "esta sesion.",
            provider,
        )
        if provider not in self._provider_warned:
            self._provider_warned.add(provider)
            self._safe_call(
                self.on_meta,
                f"(Web) {WEB_ENGINES[provider]['label']} no deja buscar desde tu "
                "conexión: bloquea por país, no por la clave (la clave puede ser "
                "válida). Como elegiste ese motor, no voy a buscar en otro sin "
                "decírtelo. Para usarlo hace falta salir por otro país con "
                "MINDVOICE_WEB_PROXY, o elige DuckDuckGo en Ajustes.",
            )

    def current_engine(self) -> str:
        """Motor EFECTIVO de este momento, ya normalizado.

        Fuente única de verdad. Todo lo demás (la petición real, la nota, lo que
        oye el modelo, lo que se ve en Ajustes) pasa por aquí, así que es
        imposible que la IA anuncie un motor distinto del que se usó: el valor
        se toma del menú y se normaliza contra ``WEB_ENGINES``, de modo que una
        opción vieja o borrada cae en DuckDuckGo en vez de quedar en limbo.
        """
        return normalize_web_engine(self._settings.web_search_provider)

    def _active_provider(self) -> Optional[str]:
        """Motor efectivo, o ``None`` si el elegido no puede usarse.

        ``None`` solo ocurre si el motor del menú necesita clave y no la tiene
        (o si está vetado por país). No se sustituye en silencio por otro: se
        avisa y se ofrece el cambio, que es justo lo que se pidió.
        """
        key = self.current_engine()
        if self._provider_geo_blocked(key):
            return None
        # "¿Hace falta clave?" lo decide el registro, no una comprobación a
        # ciegas: exigirle clave a DuckDuckGo (que no la necesita) lo dejaba
        # marcado como no disponible.
        if engine_needs_key(key) and not self._provider_key(self._settings, key):
            return None
        return key

    def _is_primary_provider(self, provider: str) -> bool:
        """¿Es este proveedor el que la config elegiría en primer lugar?"""
        return self.current_engine() == provider

    def _startup_diagnostics(self) -> None:
        """Deja por escrito con qué configuración arranca la app.

        Existe para que "mi IA suena distinta a la del autor" se pueda
        diagnosticar SIN adivinar: modelo de voz, versión de API, clave,
        motor de búsqueda y modelo de búsqueda. Va al log (para adjuntarlo a
        un bug) y al overlay (para verlo sin abrir ficheros).
        """
        clave = (getattr(self._settings, "api_key", "") or "").strip()
        motor = WEB_ENGINES[self.current_engine()]["label"]
        lineas = [
            f"(Arranque) Voz: {self._settings.model} · API "
            f"{self._settings.api_version}",
            "(Arranque) Clave de API: " + ("presente" if clave else "AUSENTE"),
            "(Arranque) Búsqueda web: "
            + ("activada" if self._settings.web_search_enabled else "desactivada")
            + f" · motor: {motor}",
            f"(Arranque) Modelo de búsqueda: {WEB_SEARCH_MODEL}",
        ]
        if not self._settings.web_search_enabled:
            lineas[-1] += " (no se usa mientras la búsqueda esté desactivada)"
        elif self.current_engine() == "duckduckgo":
            lineas[-2] += (
                " — sin clave: DuckDuckGo limita por IP y sus resultados suele "
                "ser peores; con SERPER_API_KEY se usa serper.dev"
            )
        if self._settings.web_search_model:
            lineas[-1] = (
                f"(Arranque) Modelo de búsqueda: {self._settings.web_search_model}"
                " (forzado en Ajustes, sustituye al pin de fábrica)"
            )
        for linea in lineas:
            logger.info(linea)
            self._safe_call(self.on_meta, linea)

    async def _probe_active_provider(self, provider: str) -> None:
        """Verifica al arrancar que la clave del proveedor web principal vale.

        Best-effort y en segundo plano (arranca con el pre-warm del modelo
        web): si la API rechaza la clave (401/403) se avisa de forma visible
        en el overlay, y si responde se confirma que el proveedor está listo.
        DuckDuckGo, al no usar clave y limitar por IP, NO se verifica al
        arrancar (evita gastar la cuota del minuto que luego falla en la
        primera búsqueda real): se confirma solo que queda habilitado.
        """
        if provider == "duckduckgo":
            # OJO: NO se ejecuta una búsqueda real al arrancar. DuckDuckGo
            # limita el endpoint HTML por IP: esa "verificación" gastaba la
            # cuota del minuto y la PRIMERA búsqueda real (segundos después)
            # llegaba bloqueada con "verificación humana", devolviendo 0
            # resultados y haciendo parecer que no servía pese al "listo". Solo
            # se confirma que queda habilitado; se valida en la primera búsqueda.
            logger.info(
                "Proveedor web 'duckduckgo' habilitado (verificación diferida "
                "a la primera búsqueda)."
            )
            self._safe_call(
                self.on_meta,
                "(Web) DuckDuckGo como motor de búsqueda (gratis, sin clave): "
                "se probará en tu primera búsqueda. Ojo: limita por IP y sus "
                "resultados suelen ser peores que los de serper.dev; si notas "
                "respuestas flojas al buscar, define SERPER_API_KEY (2.500/mes "
                "gratis) y reinicia.",
            )
            return
        key = self._provider_key(self._settings, provider)
        if not key:
            self._safe_call(
                self.on_meta,
                f"(Web) El motor elegido es {WEB_ENGINES[provider]['label']} y "
                f"necesita clave. Define {engine_key_env(provider)} y "
                "reinicia MindVoice; o elige DuckDuckGo en Ajustes para buscar "
                "sin clave.",
            )
            return
        try:
            async with httpx.AsyncClient(
                timeout=_WEB_PROVIDER_TIMEOUT,
                follow_redirects=True,
                # Proxy opcional para salir por un país permitido cuando el
                # proveedor bloquea el tuyo. None → hereda HTTPS_PROXY/ALL_PROXY
                # del entorno o conexión directa.
                proxy=(self._settings.web_proxy or None),
            ) as client:
                if provider == "serper":
                    resp = await client.post(
                        "https://google.serper.dev/search",
                        headers={"X-API-KEY": key, "Content-Type": "application/json"},
                        json={"q": "status", "num": 1},
                    )
                else:
                    return
            status = resp.status_code
        except Exception as exc:  # noqa: BLE001 - solo diagnóstico
            logger.warning("Probe del proveedor web '%s' falló: %s", provider, exc)
            return
        if status == 200:
            logger.info("Proveedor web '%s' verificado (HTTP 200).", provider)
            self._safe_call(
                self.on_meta,
                f"(Web) {provider.capitalize()} verificado y funcionando.",
            )
        elif status in (401, 403):
            logger.error(
                "Clave de '%s' rechazada en el arranque (HTTP %s).", provider, status
            )
            self._safe_call(
                self.on_meta,
                f"(Web) La clave de {WEB_ENGINES[provider]['label']} está "
                f"rechazada por la API (HTTP {status}). Genera una clave nueva "
                f"en serper.dev y actualiza {engine_key_env(provider)} "
                "antes de abrir MindVoice.",
            )

    @staticmethod
    def _provider_results(payload: dict, provider: str) -> str:
        """Convierte la respuesta JSON de un proveedor a notas de texto.

        Resulta en varias líneas "Título — URL — Resumen" que el modelo resume;
        vacío si el proveedor no encontró nada o devolvió un formato inesperado.
        """
        items: list = []
        if provider == "serper":
            for r in payload.get("organic") or []:
                items.append(
                    (r.get("title"), r.get("link"),
                     (r.get("snippet") or "").strip())
                )
        lines = []
        for title, url, snip in items:
            if not title and not snip:
                continue
            bits = [f"{title} — {url}: {snip}"] if snip else [f"{title} — {url}"]
            lines.append("\n".join(bits))
        return "\n".join(lines).strip()

    async def _search_via_provider(self, provider: str, query: str) -> str:
        """Busca en el motor con clave (serper.dev) → texto plano.

        Nunca lanza: si falta la clave, la red falla o el proveedor no devuelve
        nada, devuelve ``""`` y quien llama decide el siguiente paso.
        """
        key = self._provider_key(self._settings, provider)
        if not key:
            logger.warning(
                "El motor '%s' necesita clave (%s) para buscar '%s'.",
                provider,
                engine_key_env(provider),
                query[:60],
            )
            return ""
        try:
            async with httpx.AsyncClient(
                timeout=_WEB_PROVIDER_TIMEOUT,
                follow_redirects=True,
                # Proxy opcional para salir por un país permitido cuando el
                # proveedor bloquea el tuyo. None → hereda HTTPS_PROXY/ALL_PROXY
                # del entorno o conexión directa.
                proxy=(self._settings.web_proxy or None),
            ) as client:
                if provider == "serper":
                    response = await client.post(
                        "https://google.serper.dev/search",
                        headers={
                            "X-API-KEY": key,
                            "Content-Type": "application/json",
                        },
                        json={
                            "q": query[:500],
                            "num": _WEB_PROVIDER_MAX_RESULTS,
                            "gl": "es",
                            "hl": "es",
                        },
                    )
                else:
                    return ""
                response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - el fallback es la orden sin nota
            status = getattr(exc, "response", None)
            code = getattr(status, "status_code", None)
            # Un 403 con cuerpo HTML (nginx/CloudFront) NO es una clave mala: es
            # el proveedor rechazando el PAIS de salida. Medido: una clave
            # inventada y una clave real dan el mismo 403, y una peticion sin
            # clave da 401 JSON. Confundirlo hacia que el usuario regenere una
            # clave que probablemente es valida, una y otra vez.
            cuerpo = ""
            if status is not None:
                try:
                    cuerpo = (status.text or "")[:200].lower()
                except Exception:  # noqa: BLE001
                    cuerpo = ""
            es_json = "json" in str(
                getattr(status, "headers", {}) or {}
            ).lower() or cuerpo.lstrip().startswith("{")
            geo = code == 403 and not es_json
            if geo:
                self._note_geo_block(provider)
            elif code in (401, 403) and self._is_primary_provider(provider):
                # Clave inválida/revocada, no un fallo de red: avisamos una
                # vez de forma visible para que el usuario la renueve.
                logger.error(
                    "Proveedor '%s' rechazado por su API (HTTP %s): %s",
                    provider,
                    code,
                    exc,
                )
                if provider not in self._provider_warned:
                    self._provider_warned.add(provider)
                    self._safe_call(
                        self.on_meta,
                        f"(Web) La clave de {provider} no funciona (HTTP "
                        f"{code}); la API la rechaza. Regenera la clave en el "
                        "panel del proveedor y actualiza la variable de entorno "
                        "antes de abrir MindVoice.",
                    )
            else:
                logger.warning(
                    "Búsqueda por '%s' falló (%s): %s", provider, query[:60], exc
                )
            return ""
        text = self._provider_results(payload, provider)
        if not text:
            logger.info("Proveedor '%s' sin resultados para '%s'.", provider, query[:60])
        else:
            logger.info(
                "Búsqueda web por '%s' lista (%s): %s…",
                provider,
                query[:60],
                text[:120].replace("\n", " "),
            )
        return text

    async def _duckduckgo_search(self, query: str) -> str:
        """Busca gratis en DuckDuckGo → notas de texto.

        Motor por defecto: no pide cuenta ni clave. Nunca lanza y nunca se
        queda colgada: ante captcha o cero resultados devuelve ``""``.

        Lo que fallaba (medido, no supuesto): DDG no bloquea con 403 sino con
        **HTTP 202** + página "bots use duckduckgo", y ese 202 pasaba el
        ``raise_for_status()`` como si fuera una respuesta buena. El parser
        sacaba cero resultados y el HUD lo enseñaba como un "fallo de IP". La
        causa era el ritmo: con ráfaga de peticiones DDG marca la IP al
        instante (5 GET seguidos con 1,5 s de gap -> los 5 con challenge).

        Arreglos:
        1) Se detecta el 202 y el texto del challenge explícitamente.
        2) Las peticiones se espacias (``_DDG_MIN_INTERVAL``) y se serializan
           con un lock, para que dos turnos a la vez no se pisen.
        3) Se usa el método que DDG soporta de verdad: POST con token ``vqd``,
           probando antes ``lite`` y luego el HTML clásico, con GET como
           último recurso.
        4) Al tocar el límite ya no se desactiva DDG 30 min de golpe: la espera
           crece por golpes (30 s, 60 s, 120 s...) y se reinicia en cuanto
           vuelve a haber resultados. Antes bastaba una sola petición que
           rozara el límite para matar la búsqueda web media hora.
        """
        global _DDG_BLOCKED_UNTIL, _DDG_BLOCK_WARNED, _DDG_BLOCK_STRIKES
        global _DDG_LAST_REQ_TS
        q = (query or "").strip()[:_WEB_MAX_QUERY_LEN]
        if not q:
            return ""
        ahora = time.monotonic()
        if ahora < _DDG_BLOCKED_UNTIL:
            logger.debug(
                "DuckDuckGo en espera de %.0f s: se omite '%s'.",
                _DDG_BLOCKED_UNTIL - ahora,
                q[:60],
            )
            return ""
        results: list = []
        blocked = False
        async with _DDG_LOCK:
            # Separación real entre peticiones a DDG (global al módulo, así que
            # también entre instancias del LiveAssistant).
            gap = _DDG_MIN_INTERVAL - (time.monotonic() - _DDG_LAST_REQ_TS)
            if _DDG_LAST_REQ_TS and gap > 0:
                await asyncio.sleep(gap)
            try:
                async with httpx.AsyncClient(
                    timeout=_DDG_TIMEOUT,
                    follow_redirects=True,
                    headers=self._ddg_headers(),
                    # Proxy opcional por si DuckDuckGo exige otra región.
                    proxy=(self._settings.web_proxy or None),
                ) as client:
                    vqd = await self._ddg_token(client)
                    intentos = []
                    # El bloqueo de DDG es POR ENDPOINT, no por IP: medido, con
                    # ``html.duckduckgo.com`` sirviendo 10 resultados mientras
                    # ``lite.duckduckgo.com`` y ``duckduckgo.com/html/`` seguían
                    # respondiendo 202. Por eso un 202 NO corta la cadena: se
                    # prueba el siguiente endpoint igual. Antes el código
                    # asumía "bloqueo por IP, afecta a los dos" y se quedaba sin
                    # resultados teniendo uno disponible.
                    intentos.append((_DDG_HTML_URL, {"q": q, "kl": "es-es"}, "GET", _DdgParser))
                    if vqd:
                        intentos.append((_DDG_HTML_URL, {"q": q, "vqd": vqd, "kl": "es-es"}, "POST", _DdgParser))
                        intentos.append((_DDG_LITE_URL, {"q": q, "vqd": vqd}, "POST", _DdgLiteParser))
                    intentos.append((_DDG_LITE_URL, {"q": q}, "GET", _DdgLiteParser))
                    for url, params, method, parser_cls in intentos:
                        _DDG_LAST_REQ_TS = time.monotonic()
                        body, chala = await self._fetch_ddg(client, url, params, method)
                        if chala:
                            # Solo un endpoint está limitado: se anota y se
                            # sigue con el siguiente.
                            blocked = True
                            logger.debug(
                                "DuckDuckGo limitó %s (%s %s); se prueba otro.",
                                url, method, q[:40],
                            )
                            continue
                        if not body:
                            continue
                        parser = parser_cls()
                        parser.feed(body)
                        if isinstance(parser, _DdgLiteParser):
                            parser.close()
                        else:
                            parser._finalize()
                        if parser.results:
                            results = parser.results
                            break
            except Exception as exc:  # noqa: BLE001 - la orden sigue sin nota web
                logger.warning("DuckDuckGo: error inesperado: %s", exc)
                return ""
        # Todos los endpoints agotados: ahora sí, espera creciente por golpes.
        if blocked and not results:
            _DDG_BLOCK_STRIKES = min(
                _DDG_BLOCK_STRIKES + 1, len(_DDG_BLOCK_BACKOFF)
            )
            espera = _DDG_BLOCK_BACKOFF[_DDG_BLOCK_STRIKES - 1]
            _DDG_BLOCKED_UNTIL = time.monotonic() + espera
            logger.warning(
                "DuckDuckGo limitado por IP: espera %.0f s (golpe %d).",
                espera,
                _DDG_BLOCK_STRIKES,
            )
            if not _DDG_BLOCK_WARNED:
                _DDG_BLOCK_WARNED = True
                self._provider_warned.add("duckduckgo")
                # No prometer una recuperación que no llega: si ya van varios
                # golpes, esperar no basta porque la IP está vetada, no saturada.
                if _DDG_BLOCK_STRIKES >= 3:
                    self._safe_call(
                        self.on_meta,
                        f"(Web) DuckDuckGo tiene esta IP vetada: {espera:.0f} s "
                        "de espera ya no van a bastar, el bloqueo es por IP y no "
                        "por cuota. Para que la búsqueda funcione de verdad hace "
                        "falta una clave de serper.dev (2.500/mes gratis, "
                        "SERPER_API_KEY) o un proxy (MINDVOICE_WEB_PROXY).",
                    )
                else:
                    self._safe_call(
                        self.on_meta,
                        f"(Web) DuckDuckGo limitó las peticiones desde esta IP; "
                        f"reintento en {espera:.0f} s. No es una caída: se recupera "
                        f"solo. Si molesta mucho, en Ajustes puedes pasar a "
                        f"serper.dev (2.500/mes gratis) y no dependes de la IP.",
                    )
        elif results:
            # Volvió a funcionar: se olvida el castigo anterior.
            _DDG_BLOCK_STRIKES = 0
            _DDG_BLOCKED_UNTIL = 0.0
            _DDG_BLOCK_WARNED = False
            self._provider_warned.discard("duckduckgo")
        lines = []
        for r in results[:_WEB_PROVIDER_MAX_RESULTS]:
            if not r.title and not r.snippet:
                continue
            if r.snippet:
                lines.append(f"{r.title} — {r.url}: {r.snippet}")
            else:
                lines.append(f"{r.title} — {r.url}")
        text = "\n".join(lines).strip()
        if not text:
            logger.info("DuckDuckGo sin resultados para '%s'.", q[:60])
        else:
            logger.info(
                "Búsqueda web por 'duckduckgo' lista (%s): %s…",
                q[:60],
                text[:120].replace("\n", " "),
            )
        return text

    @staticmethod
    def _ddg_headers() -> dict:
        """Cabeceras de navegador completo. Sin ``Referer``/``Origin`` DDG
        clasifica la petición como bot con mucha más facilidad (medido: el mismo
        GET con y sin estas cabeceras cambia de 10 resultados a challenge)."""
        return {
            "User-Agent": _DDG_UA,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://duckduckgo.com/",
            "Origin": "https://duckduckgo.com",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Upgrade-Insecure-Requests": "1",
        }

    @staticmethod
    def _ddg_is_blocked(status: int, body: str) -> bool:
        """True si la respuesta es el challenge de DDG.

        El bloqueo real viene por **HTTP 202** (no 403), así que hay que mirar
        el estado explícitamente: ``raise_for_status()`` deja pasar el 202 y la
        página "bots use duckduckgo" se colaba como si fuera un resultado
        vacío. También se mira el texto por si el formato cambia.
        """
        if status == _DDG_BLOCK_STATUS:
            return True
        low = (body or "").lower()
        return any(tok in low for tok in _DDG_BLOCK_TOKENS)

    async def _ddg_token(self, client) -> str:
        """Token ``vqd`` de la portada de DDG.

        El método que DDG soporta de verdad es POST con ``q`` + ``vqd``; el GET
        pelado es el que más veces devuelve el challenge. Pide la portada una
        vez y reutiliza el token.
        """
        global _DDG_VQD
        if _DDG_VQD:
            return _DDG_VQD
        try:
            r = await client.get("https://duckduckgo.com/", params={"q": "x"})
            m = re.search(r'vqd=["\']?([\d-]+)', r.text) or re.search(
                r"vqd=([\d-]+)&", r.text
            )
            if m:
                _DDG_VQD = m.group(1)
        except Exception as exc:  # noqa: BLE001 - sin token se sigue con GET
            logger.debug("DuckDuckGo: no se pudo obtener el token vqd: %s", exc)
        return _DDG_VQD

    async def _fetch_ddg(
        self,
        client,
        url: str,
        params: dict,
        method: str = "GET",
    ) -> tuple:
        """Petición a DuckDuckGo (nunca lanza).

        Devuelve ``(html, bloqueado)``. El cliente ``client`` lo recibe quien
        llama para poder compartir la conexión entre los varios intentos de una
        misma búsqueda.
        """
        try:
            if method == "POST":
                response = await client.post(url, data=params)
            else:
                response = await client.get(url, params=params)
            if response.status_code == _DDG_BLOCK_STATUS:
                return "", True
            response.raise_for_status()
            body = response.text
            return body, self._ddg_is_blocked(response.status_code, body)
        except httpx.HTTPStatusError as exc:
            return "", exc.response.status_code == _DDG_BLOCK_STATUS
        except Exception as exc:  # noqa: BLE001 - la orden sigue sin nota web
            logger.warning(
                "Búsqueda por 'duckduckgo' falló (%s): %s",
                str(params.get("q") or "")[:60],
                exc,
            )
            return "", False

    async def _web_search(self, query: str) -> str:
        """Busca en internet y devuelve notas de texto para inyectar al turno.

        Nunca lanza y nunca se queda colgada. Solo hay dos motores posibles y se
        usa EXACTAMENTE el elegido en Ajustes, sin cadena de preferencia, sin
        "auto" y sin cambio silencioso a otro (era el fallo que hacía que la IA
        anunciara un motor distinto del usado):

        - ``"duckduckgo"`` (por defecto) → DuckDuckGo, gratis y sin clave.
        - ``"serper"`` → serper.dev (resultados de Google), necesita
          ``SERPER_API_KEY``; sin clave no se busca en ningún otro sitio.

        Si el intento falla devuelve ``""``: la orden continúa sin nota web y el
        turno le pide al modelo responder con su conocimiento.
        """
        now = time.time()
        key = (query or "").strip()
        # Caché: la misma consulta reciente se responde desde el último resultado
        # sin gastar cuota de nuevo (los datos no cambian en ``_WEB_CACHE_TTL``).
        if (
            key
            and key == self._web_cache_query
            and self._web_cache_note
            and now - self._web_cache_ts <= _WEB_CACHE_TTL
        ):
            logger.info(
                "Búsqueda web '%s' servida de la caché (%d s).",
                key[:60],
                int(now - self._web_cache_ts),
            )
            return self._web_cache_note
        # Throttle: dos búsquedas DISTINTAS no pueden estrenarse a la vez. La
        # segunda dentro de ``_WEB_MIN_INTERVAL`` se reserva (aviso) en lugar de
        # agotar la cuota por un impulso de órdenes seguidas.
        if now - self._last_web_ts < _WEB_MIN_INTERVAL and self._last_web_ts > 0:
            logger.info(
                "Búsqueda web '%s' diferida (throttle %.1f s).",
                key[:60],
                _WEB_MIN_INTERVAL - (now - self._last_web_ts),
            )
            self._safe_call(
                self.on_meta,
                "(Búsqueda web diferida un momento: acaba de hacerse otra "
                "para no agotar la cuota de proveedor.)",
            )
            return ""
        # Presupuesto global de la búsqueda: aunque se encadene proveedor
        # bloqueado + modelo + respaldo, el turno NO puede quedarse esperando.
            # usuario lo perceiveía como "se guinda" (el HUD en "procesando"
        # durante 20 s y luego la sesión reciclada por el watchdog de 30 s).
        try:
            note = await asyncio.wait_for(
                self._web_search_impl(query), timeout=_WEB_TOTAL_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Búsqueda web '%s' cancelada al pasar de %.0f s: el turno "
                "continúa sin nota.",
                key[:60],
                _WEB_TOTAL_TIMEOUT,
            )
            self._safe_call(
                self.on_meta,
                "(La búsqueda web tardó demasiado y se canceló; respondo con lo "
                "que sé.)",
            )
            return ""
        if note:
            self._web_cache_query = key or None
            self._web_cache_note = note
            self._web_cache_ts = time.time()
            self._last_web_ts = time.time()
        return note

    async def _web_search_impl(self, query: str) -> str:
        """Cadena real de proveedores (sin caché ni throttle).

        Extraída de ``_web_search`` para que la entrada aplique primero la
        caché y el mínimo intervalo entre búsquedas, y aquí solo se decida el
        proveedor. Devuelve las notas de texto o ``""``.
        """
        # Integración de clima: wttr.in (gratis, sin clave) responde rápido y
        # con dato estructurado; solo se intenta si la consulta habla del
        # tiempo. Si falla, se sigue con la búsqueda web genérica.
        if _WEATHER_RE.search(query or ""):
            weather = await self._weather_search(query)
            if weather:
                return weather
        # Un ÚNICO motor: el elegido en Ajustes, sin cadena de preferencia ni
        # "auto". Es lo que garantiza que lo que usa la búsqueda, lo que dice la
        # nota y lo que oye el modelo sean siempre lo mismo.
        provider = self.current_engine()
        if provider == "duckduckgo":
            return await self._duckduckgo_search(query)
        # serper.dev: necesita clave. Si falta, NO se cae a otro motor en
        # silencio (eso rompe la garantía); se dice y ya está.
        if self._provider_geo_blocked(provider):
            # Ya se avisó al detectarlo (una vez); no se repite cada consulta.
            logger.info("Motor '%s' vetado por país: no se reintenta.", provider)
            return ""
        if not self._provider_key(self._settings, provider):
            self._safe_call(
                self.on_meta,
                f"(Web) Has elegido {WEB_ENGINES[provider]['label']} pero no "
                f"hay clave: define {engine_key_env(provider)} y reinicia. "
                "Para buscar sin clave, elige DuckDuckGo en Ajustes.",
            )
            return ""
        return await self._search_via_provider(provider, query)

    async def _weather_search(self, query: str) -> str:
        """Clima ahora mismo vía wttr.in (gratis, sin clave).

        Devuelve la línea de condiciones (localidad, temperatura, humedad y
        viento) o ``""`` si falla (red, 403, sin respuesta): entonces la
        búsqueda web genérica toma el relevo.

        Dos detalles que importaban:

        - Se pide el MÉTRICO explícito (``?m``). Sin eso wttr.in decide la
          unidad por la IP y devolvía Fahrenheit ("+77°F") al caer en un
          geoloc de EE. UU., que es justo lo que no se le pide a un usuario en
          español. Además se pide ``%l`` para traer la localidad REAL resuelta:
          sin ciudad en la orden ("dame el clima") hay que decir de dónde es el
          dato, o suena a que es el sitio del usuario cuando no lo es.
        """
        if not _WEATHER_RE.search(query or ""):
            return ""
        m = _LOCATION_RE.search(query or "")
        loc = (m.group(1) if m else "").strip()
        place = quote(loc, safe="") if loc else ""
        # La query va en la URL a mano: ``?m`` es un flag sin valor y httpx lo
        # serializaría como ``m=`` o no lo enviaría.
        url = (
            f"https://wttr.in/{place}"
            f"?format=%l|%C|%t|%h|%w&lang=es&m"
        )
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                r = await client.get(url)
            if r.status_code != 200 or not (r.text or "").strip():
                return ""
            parts = [p.strip() for p in (r.text or "").split("|")]
            if len(parts) < 4:
                return ""
            resolved, cond, temp, hum = parts[0], parts[1], parts[2], parts[3]
            wind = parts[4] if len(parts) > 4 else ""
            where = loc or (resolved if resolved and resolved != "-" else "tu zona")
            bits = [
                bit
                for bit in (cond, temp, f"humedad {hum}", f"viento {wind}")
                if bit and bit != "-"
            ]
            return f"clima en {where}: " + ", ".join(bits)
        except Exception as exc:  # noqa: BLE001 - la web genérica toma el relevo
            logger.debug("Clima wttr.in no disponible: %s", exc)
            return ""

    # ------------------------------------------------------------------
    # Integraciones locales: temporizador, volumen, portapapeles
    # ------------------------------------------------------------------
    def _get_system_volume(self) -> int:
        """Volumen maestro actual (0-100 %) vía winmm."""
        try:
            v = ctypes.c_uint(0)
            ctypes.windll.winmm.waveOutGetVolume(0, ctypes.byref(v))
            left = v.value & 0xFFFF
            right = (v.value >> 16) & 0xFFFF
            return int(((left + right) / 2) * 100 / 0xFFFF)
        except Exception:  # noqa: BLE001 - sin audio, 100 a secas
            return 100

    def _set_system_volume(self, level: int) -> int:
        """Pone el volumen maestro y devuelve el nivel aplicado (0-100 %)."""
        level = max(0, min(100, int(level)))
        v = (level * 0xFFFF) // 100
        try:
            ctypes.windll.winmm.waveOutSetVolume(0, v | (v << 16))
        except Exception:  # noqa: BLE001 - winmm no disponible
            logger.warning("No se pudo ajustar el volumen del sistema")
            return self._get_system_volume()
        return level

    def _read_clipboard_text(self) -> str:
        """Devuelve el texto del portapapeles (Unicode) o ``""`` si vacío."""
        if not ctypes.windll.user32.OpenClipboard(None):
            return ""
        try:
            h = ctypes.windll.user32.GetClipboardData(13)  # CF_UNICODETEXT
            if not h:
                return ""
            ptr = ctypes.windll.kernel32.GlobalLock(h)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                ctypes.windll.kernel32.GlobalUnlock(h)
        finally:
            ctypes.windll.user32.CloseClipboard()

    def _write_clipboard_text(self, text: str) -> None:
        """Copia ``text`` al portapapeles (Unicode); el sistema toma el bloque."""
        data = (text or "").encode("utf-16-le") + b"\x00\x00"
        buf = ctypes.windll.kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not buf or not (ptr := ctypes.windll.kernel32.GlobalLock(buf)):
            raise OSError("GlobalAlloc/GlobalLock falló")
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            ctypes.windll.kernel32.GlobalUnlock(buf)
        if not ctypes.windll.user32.OpenClipboard(None):
            ctypes.windll.kernel32.GlobalFree(buf)
            raise OSError("OpenClipboard falló")
        try:
            ctypes.windll.user32.EmptyClipboard()
            if not ctypes.windll.user32.SetClipboardData(13, buf):
                raise OSError("SetClipboardData falló")
        finally:
            ctypes.windll.user32.CloseClipboard()

    def _tone(self, freq: float = 880.0, seconds: float = 0.35, rate: int = 24000) -> bytes:
        """PCM 16-bit de un pitido con fade in/out (aviso de temporizador)."""
        n = int(rate * seconds)
        fade = max(1, int(rate * 0.06))
        out = bytearray()
        for i in range(n):
            env = min(1.0, i / fade, (n - i) / fade)
            out += struct.pack("<h", int(14000 * env * math.sin(2 * math.pi * freq * i / rate)))
        return bytes(out)

    async def _timer_worker(self, entry: dict) -> None:
        """Espera el tope, suena, avisa en voz y se auto-retira de la lista.

        Usa ``end_epoch`` (hora objetivo en época) para calcular el tiempo
        restante: así, si la app se reinició y el temporizador se restauró desde
        ``timers.json``, la cuenta sigue siendo correcta desde el reinicio.
        """
        end_epoch = entry.get("end_epoch")
        if isinstance(end_epoch, (int, float)):
            remaining = max(0.0, end_epoch - time.time())
        else:
            remaining = max(0.0, float(entry.get("seconds") or 0.0))
        try:
            await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            return
        entry["done"] = True
        player = self._player
        if player is not None:
            try:
                player.write(self._tone())
                player.write(self._tone(freq=660.0, seconds=0.35))
            except Exception:  # noqa: BLE001 - sin beep, solo el aviso
                pass
        self._safe_call(self.on_meta, f"(Temporizador cumplido: {entry['label']})")
        await self._send_announcement(
            f"(El usuario pidió {entry['label']} y SUENA el temporizador ahora "
            "mismo. Avísale en voz, de forma natural y breve, que se cumplió e "
            "interésate por si quiere repetirlo o necesita otra cosa.)"
        )
        # Auto-retirada: solo quedan en la lista los pendientes (y en disco).
        if entry in self._timers:
            self._timers.remove(entry)
            self._save_timers()
            self._publish_timers()

    def _cancel_timers(self) -> str:
        """Cancela todos los temporizadores/alarmas pendientes (nota de confirmación)."""
        pend = [d for d in self._timers if not d.get("done")]
        if not pend:
            return "(No hay temporizadores ni alarmas activos que cancelar.)"
        self._timers = [d for d in self._timers if d.get("done")]
        self._save_timers()
        self._publish_timers()
        noun = "temporizador" if len(pend) == 1 else "temporizadores"
        return f"(Cancelados {len(pend)} {noun} pendientes.)"

    def _publish_timers(self) -> None:
        """Publica un resumen de los temporizadores pendientes (I9) al overlay.

        Convierte la lista actual en algo legible de un vistazo, p. ej.
        ``"⏱ 2 (03:18 · 12:47)"``, y lo entrega con ``on_timers`` (nunca
        lanza). Se llama tras crear/cumplir/cancelar temporizadores y al
        arrancar el motor con timers restaurados.
        """
        pending = [d for d in self._timers if not d.get("done")]
        try:
            if not pending:
                summary = ""
            else:
                tags = []
                for entry in pending:
                    end = str(entry.get("end") or "")[:5]
                    label = str(entry.get("label") or "timer").strip()
                    tags.append(f"{label} @{end}" if end else label)
                summary = f"⏱ {len(pending)}: " + " · ".join(tags)
            self._safe_call(self.on_timers, summary)
        except Exception as exc:  # noqa: BLE001 - nunca romper el turno
            logger.debug("No se pudo publicar el estado de timers: %s", exc)

    def _save_timers(self) -> None:
        """Persiste solo los temporizadores pendientes (best-effort)."""
        pending = [d for d in self._timers if not d.get("done")]
        try:
            with open(_TIMERS_FILE, "w", encoding="utf-8") as fh:
                json.dump(pending, fh, ensure_ascii=False, indent=2)
        except (OSError, TypeError) as exc:
            logger.warning("No se pudo guardar los temporizadores: %s", exc)

    def _load_timers_file(self) -> list:
        """Lee los temporizadores pendientes persistidos, descartando los cumplidos."""
        try:
            with open(_TIMERS_FILE, encoding="utf-8") as fh:
                stored = json.load(fh)
        except (OSError, ValueError) as exc:
            logger.debug("Temporizadores persistidos no disponibles: %s", exc)
            stored = []
        now = time.time()
        pending = []
        for entry in stored if isinstance(stored, list) else []:
            if not isinstance(entry, dict):
                continue
            epoch = entry.get("end_epoch")
            label = (entry.get("label") or "temporizador").strip()
            if entry.get("done") or not isinstance(epoch, (int, float)):
                continue
            remaining = float(epoch) - now
            if remaining <= 0:
                continue
            end = entry.get("end")
            if not isinstance(end, str) or not end:
                end = datetime.fromtimestamp(float(epoch)).strftime("%H:%M:%S")
            pending.append(
                {
                    "seconds": int(max(1, remaining)),
                    "label": label,
                    "end": end,
                    "end_epoch": float(epoch),
                    "done": False,
                }
            )
        return pending[:_TIMERS_MAX]

    def _load_timers(self) -> None:
        """Restaura los temporizadores pendientes tras un reinicio (en el bucle)."""
        restored = self._load_timers_file()
        self._timers = restored
        for entry in restored:
            asyncio.get_running_loop().create_task(self._timer_worker(entry))
        if restored:
            logger.info("Temporizadores restaurados tras el reinicio: %d", len(restored))
        self._publish_timers()

    async def _send_announcement(self, text: str) -> None:
        """Envía un turno de voz al modelo sin pasar por la orden del usuario."""
        session = self._session
        if session is None:
            return
        self._last_data_ts = asyncio.get_running_loop().time()
        try:
            await session.send_client_content(
                turns=[types.Content(role="user", parts=[types.Part(text=text)])],
                turn_complete=True,
            )
        except Exception as exc:  # noqa: BLE001 - sesión en cierre: sin aviso
            logger.debug("No se pudo enviar el aviso en voz: %s", exc)
            self._awaiting_turn = False
            self._responding = False
            self._outdone_ts = None
            self._set_state(
                AssistantState.LISTENING
                if self._voice_active()
                else AssistantState.IDLE
            )
            return
        self._awaiting_turn = True
        self._last_data_ts = asyncio.get_running_loop().time()
        self._set_state(AssistantState.PROCESSING)

    def _local_action(self, text: str) -> Optional[str]:
        """Ejecuta una integración local si la orden coincide con una.

        Devuelve la nota de confirmación para el turno (el modelo la lee y la
        integra en voz) o ``None`` si ninguna integración aplica: la orden
        sigue su flujo normal (visión, web, etc.).
        """
        t = (text or "").strip()
        low = t.lower()
        if not t:
            return None

        # --- Memoria (Fase 2) ------------------------------------------------
        # Va primero porque habla de la propia conversación, pero devuelve None
        # en cuanto la orden es de otra integración, así que "olvida el
        # temporizador" sigue cancelando el temporizador.
        nota_memoria = self._accion_memoria(t)
        if nota_memoria is not None:
            return nota_memoria

        # --- Lista de temporizadores activos --------------------------------
        if _TIMER_LIST_RE.search(t):
            pend = [d for d in self._timers if not d["done"]]
            if not pend:
                return "(No hay temporizadores ni alarmas activos ahora.)"
            items = ", ".join(f"{d['label']} ({d['end']})" for d in pend)
            return f"(Temporizadores activos: {items}.)"

        # --- Temporizador / alarma ------------------------------------------
        if _TIMER_CANCEL_RE.search(t):
            return self._cancel_timers()
        seconds = None
        label = ""
        m_at = _TIMER_AT_RE.search(t)
        if m_at:
            hh, mm = int(m_at.group(1)), int(m_at.group(2))
            if 0 <= hh < 24 and 0 <= mm < 60:
                now = datetime.now()
                target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                # ``ceil`` y no truncar: con ``int`` la alarma de las 18:30
                # sonaba a las 18:29:59 (los milisegundos de ``now`` se
                # comían el segundo entero).
                seconds = math.ceil((target - now).total_seconds())
                label = f"alarma a las {hh:02d}:{mm:02d}"
        if seconds is None:
            m = _TIMER_AT_BARE_RE.search(t)
            if m:
                hh, mm = int(m.group(1)), int(m.group(2))
                if 0 <= hh < 24 and 0 <= mm < 60:
                    now = datetime.now()
                    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if target <= now:
                        target += timedelta(days=1)
                    # Mismo redondeo al alza que en la alarma anterior: sin esto la
                    # alarma sonaba un segundo antes de la hora pedida.
                    seconds = math.ceil((target - now).total_seconds())
                    label = f"alarma a las {hh:02d}:{mm:02d}"
        if seconds is None:
            m = _TIMER_RE.search(t)
            if m:
                n = int(m.group(1))
                unit = m.group(2).lower()
                if unit.startswith("h"):
                    mult, unit_txt = 3600, "hora"
                elif unit.startswith("m"):
                    mult, unit_txt = 60, "minuto"
                else:
                    mult, unit_txt = 1, "segundo"
                seconds = n * mult
                label = f"{n} {unit_txt}{'s' if n != 1 else ''}"
        if seconds is None:
            m_in = _TIMER_IN_RE.search(t)
            if m_in:
                n = int(m_in.group(1))
                unit = m_in.group(2).lower()
                if unit.startswith("h"):
                    mult, unit_txt = 3600, "hora"
                elif unit.startswith("m"):
                    mult, unit_txt = 60, "minuto"
                else:
                    mult, unit_txt = 1, "segundo"
                seconds = n * mult
                label = f"{n} {unit_txt}{'s' if n != 1 else ''}"
        if seconds is None:
            if _TIMER_HALF_RE.search(t):
                seconds, label = 1800, "30 minutos"
            elif _TIMER_HOURHALF_RE.search(t):
                seconds, label = 5400, "90 minutos"
            elif _TIMER_QUARTER_RE.search(t):
                seconds, label = 900, "15 minutos"
            elif _TIMER_COUPLE_RE.search(t):
                seconds, label = 7200, "2 horas"
            elif _TIMER_ONE_UNIT_RE.search(t):
                unit = _TIMER_ONE_UNIT_RE.search(t).group(1)
                if unit.startswith("h"):
                    seconds, label = 3600, "1 hora"
                else:
                    seconds, label = 60, "1 minuto"
        if seconds is not None and seconds > 0:
            self._timers = [d for d in self._timers if not d.get("done")][-_TIMERS_MAX:]
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return None  # fuera del bucle: no se programa
            # Dedupe (E10): ya hay una cuenta idéntica pendiente → se avisa en
            # lugar de programar dos pitidos a la vez con el mismo sonido.
            dupe = next(
                (d for d in self._timers if not d.get("done") and d.get("label") == label),
                None,
            )
            if dupe and dupe.get("end"):
                return (
                    f"(Ya había un temporizador de {label} pendiente "
                    f"(suena a las {dupe['end']}): no lo duplico.)"
                )
            end = (datetime.now() + timedelta(seconds=seconds)).strftime("%H:%M:%S")
            entry = {
                "seconds": seconds,
                "label": label,
                "end": end,
                "end_epoch": time.time() + seconds,
                "done": False,
            }
            self._timers.append(entry)
            self._save_timers()
            self._publish_timers()
            asyncio.get_running_loop().create_task(self._timer_worker(entry))
            return f"(Temporizador de {label} activado: suena a las {end}.)"

        # --- Volumen del sistema --------------------------------------------
        vol_involved = "volumen" in low or _VOL_MUTE_RE.search(low) or _VOL_PCT_RE.search(low)
        if vol_involved:
            level = None
            m_pct = _VOL_PCT_RE.search(t)
            if m_pct:
                level = self._set_system_volume(int(m_pct.group(1)))
            elif _VOL_MUTE_RE.search(low) and "volumen" in low:
                level = self._set_system_volume(0)
            elif _VOL_UP_RE.search(low):
                level = self._set_system_volume(self._get_system_volume() + 15)
            elif _VOL_DOWN_RE.search(low):
                level = self._set_system_volume(self._get_system_volume() - 15)
            else:
                level = self._set_system_volume(100)
            return f"(Volumen del sistema ajustado al {level}%.)"

        # --- Portapapeles ---------------------------------------------------
        m_copy = _CLIP_COPY_RE.search(t)
        if m_copy:
            payload = m_copy.group(1).strip(" \"'")
            if (
                0 < len(payload) <= 200
                and payload.split()[0].lower() not in _CLIP_MEH_FIRST
                and not payload.startswith("de seguridad")
            ):
                try:
                    self._write_clipboard_text(payload)
                    return f"(Copiado al portapapeles: '{payload}')"
                except Exception as exc:  # noqa: BLE001 - portapapeles ocupado
                    return f"(No se pudo copiar al portapapeles: {exc})"
        if _CLIP_READ_RE.search(low):
            clip = (self._read_clipboard_text() or "").strip()
            if clip:
                snippet = clip if len(clip) <= 600 else clip[:600] + "…"
                return f"(Portapapeles actual: \"{snippet}\")"
            return "(El portapapeles está vacío o no tiene texto.)"

        return None

    def _schedule_voice_web(self, session, text: str) -> None:
        """Lanza (sin bloquear) la búsqueda web pedida por VOZ.

        La transcripción de la voz llega con el turno de audio ya cerrado, así
        que aquí se arranca la búsqueda lo antes posible con el disparador
        explícito o el detector inteligente (ver ``_launch_voice_web``). Si ya
        hay otra búsqueda de voz en vuelo, la nueva cancela a la anterior.
        """
        if self._web_search_task is not None:
            if not self._web_search_task.done():
                self._web_search_task.cancel()
            self._web_search_task = None
        self._web_preempting = False
        self._web_task_seq += 1
        seq = self._web_task_seq

        explicit = self._web_query(text)
        if explicit is not None:
            self._pending_web_query = explicit
            task = asyncio.create_task(
                self._launch_voice_web(session, explicit, seq)
            )
        elif (
            self._settings.web_smart_detect
            and self._looks_searchable(text)
        ):
            async def _classify_and_search() -> None:
                query = await self._classify_web_query(text)
                if not query or self._pending_web_query is not None:
                    return
                self._pending_web_query = query
                await self._launch_voice_web(session, query, seq)
            task = asyncio.create_task(_classify_and_search())
        else:
            return
        self._web_search_task = task
        task.add_done_callback(self._on_web_task_done)

    def _on_web_task_done(self, task: asyncio.Task) -> None:
        """Libera la referencia a la tarea de búsqueda al terminar (o cancelar).

        También consume la excepción de la tarea (si la hubo) para que asyncio no
        avise de "excepción nunca recuperada" cuando la sesión muere a mitad.
        """
        if self._web_search_task is task:
            self._web_search_task = None
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def _launch_voice_web(self, session, query: str, seq: int) -> None:
        """Resuelve una búsqueda web pedida por voz en UNA sola respuesta.

        Los pasos:

        1. Interrumpe la respuesta "sin internet" que el modelo está a punto
           de pronunciar (con ``_INTERRUPT_TEXT``, igual que el botón Cancelar).
        2. Busca en internet (con la consulta ya conocida o recién clasificada).
        3. Si hay nota, la reenvía como TURNO NUEVO con los datos: el modelo
           responde HABLANDO los resultados. Si la búsqueda quedó vacía, se
           reenvía la pregunta para que el modelo responda igualmente (nunca
           se deja al usuario sin respuesta tras interrumpir).

        Si llega otra orden/búsqueda mientras tanto (``_pending_web_query`` ya
        no es la nuestra), la nota se descarta y gana lo más reciente.
        """
        def _discard_if_current() -> None:
            if seq == self._web_task_seq:
                self._clear_turn_state()

        try:
            # 1) Pre-interrupción: corta la respuesta "vacía" antes de que salga.
            #    SOLO si el modelo ya está hablando. Mandar este texto mientras
            #    el modelo todavía no ha emitido nada deja la sesión muerta
            #    (medido: sin transcripción ni respuesta, sin error ni
            #    go_away, durante más de un minuto), que es justo cuando se
            #    dispara: el usuario acaba de soltar el botón y el modelo está
            #    pensando. Si aún no ha hablado, se le deja decir su frase
            #    (corta) y la nota llega después como turno nuevo.
            if self._responding:
                self._web_preempting = True
                try:
                    await session.send_realtime_input(text=_INTERRUPT_TEXT)
                except Exception as exc:  # noqa: BLE001 - sesión en cierre
                    logger.debug("Pre-interrupción de búsqueda web no enviada: %s", exc)
                    _discard_if_current()
                    return
                self._responding = False
                if self._player is not None:
                    self._player.flush()
                self._out_acc.clear()
                self._in_acc.clear()
            # La búsqueda es parte del turno: el watchdog no debe reciclar la
            # sesión mientras trabaja (y así, si el usuario habla mientras tanto,
            # su voz manda y esta búsqueda se descarta).
            # La interrupción y la búsqueda son parte del turno: el watchdog no
            # debe reciclar la sesión mientras la web trabaja.
            self._awaiting_turn = True
            self._last_data_ts = asyncio.get_running_loop().time()
            self._set_state(AssistantState.PROCESSING)
            self._safe_call(self.on_meta, f"(Búsqueda web de '{query[:60]}'…)")

            # 2) La búsqueda; nunca rompe la sesión.
            try:
                web_note = await asyncio.wait_for(
                    self._web_search(query), timeout=_WEB_SEARCH_TIMEOUT
                )
            except Exception as exc:  # noqa: BLE001 - se trata como "sin nota"
                logger.warning("Búsqueda web por voz interrumpida: %s", exc)
                web_note = ""
            web_note = (web_note or "").strip()
            if web_note and len(web_note) > self._settings.web_note_max_chars:
                web_note = web_note[: self._settings.web_note_max_chars] + "…"
            # Los resultados también se publican en el chat (etiqueta Web), para
            # que lo que "solo se dice" también se vea escrito.
            if web_note:
                self._safe_call(self.on_web, web_note)

            # 3) Si otra búsqueda tomó el turno mientras ésta corría, se descarta.
            #    (El barge-in real cancela la tarea; esta comprobación cubre el
            #    caso en que una nueva búsqueda ya la superó.)
            if seq != self._web_task_seq:
                logger.info(
                    "Búsqueda web descartada: otra búsqueda tomó su turno "
                    "(seq %d frente a %d).",
                    seq,
                    self._web_task_seq,
                )
                _discard_if_current()
                return
            # Si el usuario volvió a hablar (micrófono activo), esperar a que
            # suelte el botón para no partir su turno de voz a medias; si se
            # queda hablando, la búsqueda se descarta (el usuario ya está en
            # otro asunto).
            waited = 0.0
            while self._voice_active() and waited < 10.0:
                await asyncio.sleep(0.2)
                waited += 0.2
            if self._voice_active() or seq != self._web_task_seq:
                logger.info(
                    "Búsqueda web descartada: el usuario volvió a hablar (%s).",
                    query[:60],
                )
                _discard_if_current()
                return
            self._pending_web_query = None
            if not web_note:
                self._safe_call(
                    self.on_meta,
                    "(Búsqueda web sin resultados; se reinterpreta la pregunta.)",
                )
            # La entrega: si el turno que pidió la nota se recicló (la sesión
            # se cierra al acabar el turno de voz), va a la sesión vigente;
            # si no hay sesión viva, se descarta en silencio.
            target = (
                self._session
                if self._session is not None and self._session is not session
                else session
            )
            if target is None:
                logger.info(
                    "Búsqueda web terminada sin sesión viva; nota descartada (%s).",
                    query[:60],
                )
                _discard_if_current()
                return
            if target is not session:
                logger.info(
                    "El turno se recicló con la búsqueda web en vuelo: "
                    "la nota se entrega a la sesión vigente."
                )
            await target.send_client_content(
                turns=[types.Content(
                    role="user",
                    parts=[types.Part(
                        text=(
                            self._web_note_for(query, web_note)
                            if web_note
                            else self._web_fallback_text(query)
                        )
                    )],
                )],
                turn_complete=True,
            )
            # El turno NUEVO (con la nota) arranca aquí: base fresca del watchdog.
            self._awaiting_turn = True
            self._last_data_ts = asyncio.get_running_loop().time()
            self._set_state(AssistantState.PROCESSING)
        except asyncio.CancelledError:
            _discard_if_current()
            raise
        except Exception as exc:  # noqa: BLE001 - la sesión pudo cerrarse a mitad
            logger.debug("Búsqueda web por voz terminada por: %s", exc)
            _discard_if_current()
        finally:
            # Solo la tarea MÁS RECIENTE puede desactivar la pre-interrupción:
            # si vino otra búsqueda, ésta conserva su estado (ver seq).
            if seq == self._web_task_seq:
                self._web_preempting = False

    @staticmethod
    def _rms_level(chunk: bytes) -> Optional[float]:
        """RMS (sobre PCM 16 bits LE) de un fragmento, o ``None`` si inválido."""
        if not chunk:
            return None
        sample_bytes = len(chunk) - (len(chunk) % 2)
        if sample_bytes == 0:
            return None
        samples = struct.unpack("<%dh" % (sample_bytes // 2), chunk[:sample_bytes])
        acc = 0.0
        for sample in samples:
            acc += float(sample * sample)
        return (acc / len(samples)) ** 0.5 if samples else None

    @staticmethod
    def _drain_chunks(chunk_queue: "asyncio.Queue[bytes]") -> None:
        while not chunk_queue.empty():
            try:
                chunk_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _describe_screen(self, screen: ScreenCapture) -> str:
        """Describe la pantalla actual (texto) en una mini-sesión Live aparte.

        La sesión principal de voz NO puede recibir imágenes: probado en vivo
        que un ``send_client_content`` con imagen inline deja de responder a
        los turnos de audio realtime posteriores. Por eso la captura se manda a
        una conexión Live efímera y de aquí sale solo el texto de la
        descripción, que se usa en órdenes de texto.

        Se usa ``capture()`` (SIEMPRE fresca, ignora detección de cambios):
        una descripción a petición es "esto que hay AHORA", y usar
        ``capture_if_changed`` anulaba la nota cuando la pantalla estaba
        estática (menú quieto, escritorio), dejando al modelo sin contexto.
        """
        started = time.monotonic()
        try:
            async with self._describe_lock:
                jpeg: Optional[bytes] = None
                for attempt in range(2):
                    try:
                        jpeg = await asyncio.to_thread(screen.capture)
                        if jpeg:
                            break
                    except Exception as exc:  # noqa: BLE001 - mss puede fallar
                        logger.warning(
                            "Captura para describir la pantalla falló "
                            "(intento %d): %s",
                            attempt + 1,
                            exc,
                        )
                if not jpeg:
                    logger.warning("No se pudo capturar la pantalla (2 intentos).")
                    return ""
                if _DEBUG_ENABLED:
                    try:
                        with open(_DEBUG_VISTA, "wb") as fh:
                            fh.write(jpeg)
                    except OSError:
                        pass
                async def _run_vision(config) -> str:
                    chunks: list[str] = []
                    async with self._client.aio.live.connect(
                        model=self._settings.model, config=config
                    ) as vision:
                        await vision.send_client_content(
                            turns=[types.Content(
                                role="user",
                                parts=[
                                    types.Part(
                                        inline_data=types.Blob(
                                            data=jpeg, mime_type="image/jpeg"
                                        )
                                    ),
                                    types.Part(
                                        text="Describe con PRECISIÓN lo que hay en "
                                             "esta pantalla ahora mismo. Da primero "
                                             "la app o el juego y la ventana activa; "
                                             "después transcribe literalmente el "
                                             "texto legible que veas (títulos, "
                                             "botones, mensajes de error, "
                                             "contenido) y por último el estado "
                                             "relevante (qué está en marcha, qué "
                                             "pide atención). Sé factual y "
                                             "concreto: no digas 'una pantalla "
                                             "con texto', di qué texto es. Si algo "
                                             "no se lee bien, dilo en vez de "
                                             "inventarlo. Responde en español, en "
                                             "3-6 frases cortas, sin saludos ni "
                                             "comentarios sobre la imagen."
                                    ),
                                ],
                            )],
                            turn_complete=True,
                        )
                        async for message in vision.receive():
                            server_content = getattr(message, "server_content", None)
                            if server_content is None:
                                continue
                            transcription = getattr(
                                server_content, "output_transcription", None
                            )
                            if transcription is not None and transcription.text:
                                chunks.append(transcription.text)
                            turn = getattr(server_content, "model_turn", None)
                            if turn is not None:
                                for part in turn.parts or []:
                                    if (
                                        part.text
                                        and not part.thought
                                    ):
                                        chunks.append(part.text)
                            if getattr(server_content, "turn_complete", False):
                                break
                    return " ".join(chunks).strip()

                description = ""
                try:
                    description = await _run_vision(
                        types.LiveConnectConfig(
                            response_modalities=["AUDIO"],
                            output_audio_transcription=types.AudioTranscriptionConfig(
                                language_codes=self._transcript_langs()
                            ),
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Mini-sesión de descripción falló (%.2f s): %s",
                        time.monotonic() - started,
                        exc,
                    )
                if _DEBUG_ENABLED:
                    try:
                        with open(_DEBUG_NOTA, "w", encoding="utf-8") as fh:
                            fh.write(description)
                    except OSError:
                        pass
                if description:
                    self._desc_cache = description
                    self._desc_cache_ts = asyncio.get_running_loop().time()
                logger.info(
                    "Descripción de pantalla lista (%.2f s): %s",
                    time.monotonic() - started,
                    description[:160] if description else "(vacía)",
                )
                return description
        except asyncio.CancelledError:
            # La espera de la descripción se canceló: el turno seguirá sin
            # contexto visual.
            logger.warning(
                "Descripción de pantalla cancelada tras %.2f s (tope del turno).",
                time.monotonic() - started,
            )
            raise

    def prefetch_description(self) -> None:
        """Pide en segundo plano tener lista la próxima descripción de pantalla.

        La llamar el HUD cuando el usuario ENFOCA la barra de órdenes, no cuando
        pulsa Enter. Es la clave de la latencia: la descripción se conseguía
        abriendo una mini-sesión Live aparte (2,8 s de mediana, y se cancelaba en
        el 46 % de las órdenes por el tope de 4 s del turno), con lo que el
        modelo acababa respondiendo sin contexto visual y se inventaba lo que
        había en pantalla. Con el aviso al enfocar, el tiempo de escritura del
        usuario cubre ese coste y, al enviar la orden, la nota suele estar ya
        lista y fresca.

        Nunca lanza y nunca bloquea: si no hay motor, sesión o pantalla, o si ya
        hay una descrição reciente, no hace nada.
        """
        if not self._settings.screen_enabled or self.quit_event.is_set():
            return
        now = asyncio.get_running_loop().time()
        if self._desc_cache and now - self._desc_cache_ts <= _DESC_CACHE_TTL:
            return
        if self._desc_prefetch_task is not None and not (
            self._desc_prefetch_task.done()
        ):
            return
        screen = self._screen_ref
        if screen is None:
            return

        async def _warm() -> None:
            try:
                await self._describe_screen(screen)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - el precalentado es best-effort
                logger.debug("Precalentado de pantalla omitido: %s", exc)

        try:
            self._desc_prefetch_task = asyncio.ensure_future(_warm())
        except RuntimeError:  # sin bucle activo: se calienta en el próximo turno
            self._desc_prefetch_task = None

    async def _voice_screen_note(self, session, desc_fut) -> None:
        """Inyecta el contexto visual en el turno de voz si está disponible.

        Primero aprovecha la caché reciente (precalentada al enfocar la barra,
        así que lo normal es que sea instantáneo) y solo si no hay, espera a la
        descripción en vuelo con un tope corto. La visión nunca bloquea la voz:
        si no llega a tiempo, el audio se cierra igual y el turno va sin
        contexto visual, que es como estaba antes.
        """
        now = asyncio.get_running_loop().time()
        if (
            self._desc_cache is not None
            and now - self._desc_cache_ts <= _DESC_CACHE_TTL
        ):
            await self._send_screen_note(session, self._desc_cache)
            return
        if desc_fut is None:
            return
        try:
            note = await asyncio.wait_for(
                asyncio.shield(desc_fut), timeout=_SCREEN_NOTE_TIMEOUT
            )
        except Exception:  # noqa: BLE001 - tope agotado o mini-sesión rota
            logger.debug("Nota de pantalla no lista al cerrar el turno de voz.")
            return
        await self._send_screen_note(session, note or "")

    async def _send_screen_note(self, session, description: str) -> None:
        """Inyecta la descripción de pantalla como NOTA de texto del turno.

        Va con ``turn_complete=False`` para que el audio realtime que sigue
        (o que ya llegó) cierre el mismo turno: probado en vivo que así el
        modelo responde a la voz usando el contexto de la pantalla. Nunca se
        envía una imagen por aquí (esa vía dejaba la sesión muda).
        """
        text = (description or "").strip()
        if not text:
            return
        if len(text) > _SCREEN_NOTE_MAX_CHARS:
            text = text[:_SCREEN_NOTE_MAX_CHARS]
        try:
            await session.send_client_content(
                turns=[
                    types.Content(
                        role="user",
                        parts=[types.Part(text=f"(Contexto visual: {text})")],
                    )
                ],
                turn_complete=False,
            )
            logger.info("Nota de pantalla inyectada en el turno: %s", text[:160])
        except Exception as exc:  # noqa: BLE001
            # A mitad de un turno de voz el servidor puede rechazarlo; NO se
            # propaga: reciclar la sesión cortaría la voz del usuario.
            logger.debug("No se pudo inyectar la nota de pantalla: %s", exc)

    async def _sleep_responsive(self, seconds: float) -> None:
        """Duerme por intervalos cortos para responder a desactivaciones."""
        end = asyncio.get_running_loop().time() + seconds
        while self.quit_event.is_set() is False:
            remaining = end - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(
                    asyncio.sleep(remaining), timeout=min(1.0, remaining)
                )
                return
            except asyncio.TimeoutError:
                continue

    async def _watchdog_loop(
        self,
        session,
        player: Optional[AudioPlayer],
    ) -> None:
        """Recicla la sesión si un turno queda estancado sin datos.

        Cada segundo comprueba si hay un turno en curso (el modelo hablando o
        esperando respuesta a una orden/voz) y el servidor no envía NADA desde
        hace ``response_stall_timeout`` segundos. En ese caso corta la respuesta
        congelada, vacía el búfer de voz y fuerza la reconexión automática del
        ``run()``. Sin este watchdog, un turno congelado dejaba al asistente
        mudo para siempre o esperando la ingrata reconexión por error 1008.
        """
        loop = asyncio.get_running_loop()
        stall = getattr(
            self._settings, "response_stall_timeout", _RESPONSE_STALL_TIMEOUT
        )
        while not self.quit_event.is_set():
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            if self.quit_event.is_set():
                return
            if not (self._responding or self._awaiting_turn):
                continue
            # Finalización por gracia: el modelo terminó de emitir contenido
            # (audio/texto/transcripción) pero el servidor no envió
            # ``turn_complete`` (turno recortado por su tope de audio o sesión a
            # punto de caerse). Cerrar el turno a los ``_OUTDONE_GRACE_S`` de
            # silencio entrega el texto al cliente ya y evita la espera de 30 s
            # del watchdog general (que, además, descartaba el texto acumulado).
            #
            # ESTA COMPROBACIÓN VA ANTES que la de "micrófono abierto" a propósito:
            # el modelo YA terminó de hablar, así que da igual que el micrófono
            # siga abierto esperando. Si se invertía el orden, con el micro
            # abierto (modo toggle) esta rama quedaba saltada para siempre, el
            # turno no se cerraba nunca y el micrófono --con el segmento ya
            # cerrado-- se quedaba bloqueado hasta el tope de 300 s.
            if self._responding and self._outdone_ts is not None:
                if loop.time() - self._outdone_ts > _OUTDONE_GRACE_S:
                    logger.info(
                        "Respuesta acabada sin turn_complete (%.0f s de "
                        "silencio): cerrando turno y entregando el texto.",
                        loop.time() - self._outdone_ts,
                    )
                    await self._end_turn(session)
                    continue
            # Mientras el micrófono REALMENTE está enviando audio, el servidor
            # escucha y legítimamente no devuelve datos: no es un estancamiento.
            # Ojo: "micrófono abierto" NO es lo mismo que "micrófono enviando".
            # Con el segmento ya cerrado el bucle de voz descarta los chunks, no
            # sale tráfico y el turno SÍ puede estar muerto: por eso el corte
            # exige ``_voice_sending``.
            if self._voice_active():
                # Tope absoluto: una sesión de voz interminable (PTT pegado o
                # micrófono abierto) que no cierra ni un turno se recicla aquí.
                # Sin esto, ``_last_data_ts`` se refrescaba para siempre y la
                # sesión nunca se reciclaba: el micrófono seguía subiendo audio
                # (440 MB medidos) y el asistente ya no respondía nunca.
                #
                # VAD manual: hablar es una actividad de duración indefinida por
                # diseño, así que la base del tope se refresca mientras sale
                # audio de verdad. El tope sigue protegiendo a quien habla y solo
                # queda para el micrófono ABIERTO Y MUDO, que es el atasco real
                # (PTT pegado, sin voz).
                if (
                    self._voice_sending
                    and self._settings.voice_manual_vad
                    # Solo mientras sale audio de verdad. Antes se refrescaba la
                    # base en TODO turno con VAD manual, con lo que el tope
                    # absoluto no podía dispararse jamás en el único caso que
                    # protege: micro abierto y callado. Medido el 30/09: 4 min
                    # de micro abierto y 0 turnos -> el servidor cortó con
                    # 1011 "Resource has been exhausted" y el turno ya no se
                    # transcribía.
                    and self._voice_last_audio_ts is not None
                    and loop.time() - self._voice_last_audio_ts
                    < _VOICE_FLOW_GRACE_S
                ):
                    if self._voice_opened_ts is not None:
                        self._voice_opened_ts = loop.time()
                elif (
                    self._voice_opened_ts is not None
                    and loop.time() - self._voice_opened_ts > _VOICE_MAX_OPEN_S
                ):
                    logger.warning(
                        "Voz abierta %.0f s sin cerrar ningún turno: se recicla "
                        "la sesión (PTT u micrófono atascados).",
                        loop.time() - self._voice_opened_ts,
                    )
                    self._set_state(AssistantState.ERROR)
                    self._awaiting_turn = False
                    self._responding = False
                    raise _SessionStalled(
                        "sesión de voz abierta sin cerrar ningún turno "
                        f"({loop.time() - self._voice_opened_ts:.0f} s)"
                    )
                if self._voice_sending:
                    # Se refresca la base de tiempo para que, al soltar el botón,
                    # los 10 s empiecen a contar DESDE el final del habla (si no,
                    # un turno de voz largo dispararía un falso estancamiento
                    # inmediato al soltar).
                    self._last_data_ts = loop.time()
                    continue
            last = self._last_data_ts
            if last is None:
                continue
            idle = loop.time() - last
            # Turno de voz sin NADA de vuelta (ni transcripción, ni audio, ni
            # error): el servidor se lo ha quedado. No se espera al tope general
            # de 30 s: se recycle ya y el audio se reenvía en la sesión nueva
            # (el primer turno de una sesión recién abierta es el que mejor se
            # procesa). Ver ``_replay_voice_turn``.
            if (
                self._awaiting_turn
                and not self._responding
                and idle > _VOICE_DEAD_S
                and self._voice_replay
                and self._voice_replay_tries < 1
            ):
                logger.warning(
                    "Turno de voz sin respuesta tras %.0f s; se reabre la "
                    "sesión y se reenvía la frase (%.1f s de audio).",
                    idle,
                    len(self._voice_replay)
                    / max(1.0, self._voice_replay_rate * 2),
                )
                self._replay_pending = True
                self._set_state(AssistantState.ERROR)
                self._responding = False
                self._awaiting_turn = False
                raise _SessionStalled(
                    f"turno de voz sin respuesta ({idle:.0f} s sin datos)"
                )
            if idle <= stall:
                continue
            logger.warning(
                "Respuesta estancada (%.0f s sin datos). Reiniciando el turno…",
                idle,
            )
            self._set_state(AssistantState.ERROR)
            # Intento blando de detener al modelo; si la sesión ya está rota,
            # el cierre se produce igualmente al lanzar la excepción.
            try:
                await session.send_realtime_input(text=_INTERRUPT_TEXT)
            except Exception:  # noqa: BLE001 - sesión a punto de cerrarse
                pass
            if player is not None:
                player.flush()
            now_mono = time.monotonic()
            if now_mono - self._reset_window_start > _RESET_WINDOW_S:
                self._reset_window_start = now_mono
                self._reset_count = 0
            self._reset_count += 1
            if self._reset_count >= _HARD_RESET_ESCALATE:
                # Hard reset del pipeline de salida: varios turnos congelados
                # seguidos sugieren que el reproductor se atascó, no solo la red.
                logger.warning(
                    "Varios turnos estancados seguidos: reiniciando el "
                    "pipeline de audio de salida…"
                )
                self._reset_count = 0
                if player is not None:
                    try:
                        await asyncio.to_thread(player.restart)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("No se pudo reiniciar el reproductor: %s", exc)
            self._responding = False
            self._awaiting_turn = False
            # Aborta la sesión; run() la reabre con retroceso exponencial.
            raise _SessionStalled(
                f"turno estancado {idle:.0f} s sin datos del servidor"
            )

    @staticmethod
    def _safe_call(
        callback: Optional[Callable], *args: object
    ) -> None:
        """Invoca un callback de UI a prueba de fallos.

        Los callbacks (``on_text``, ``on_voice_level``…) corren DENTRO del bucle
        asyncio: si el overlay lanza una excepción aquí (p. ej. un problema en
        ``_uiput``), esa excepción derribaba el ``_receive_loop`` y mataba la
        sesión Live (silencio + reconexión). Este helper captura el error y lo
        registra sin propagarlo.
        """
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:  # noqa: BLE001 - la UI no debe tumbar la sesión
            logger.warning("Callback de UI falló (ignorado)", exc_info=True)

    async def _receive_loop(
        self,
        session,
        player: Optional[AudioPlayer],
    ) -> None:
        """Consume las respuestas del modelo: texto a consola y PCM al altavoz.

        Con los modelos ``*-live-preview`` (solo audio) el texto llega por la
        transcripción de salida (``output_transcription``): se acumula por
        turno y se entrega completo a ``on_text``. Tu voz transcrita
        (``input_transcription``) se entrega a ``on_user_text``.

        Se recorre con ``_live_messages`` (y no con ``session.receive()`` a
        pelo) porque el generador del SDK solo cubre UN turno: se cierra en
        cada ``turn_complete`` y, sin relanzarlo, el socket dejaba de leerse
        y el asistente solo respondía a la primera llamada.
        """
        async for message in _live_messages(session):
            if self.quit_event.is_set():
                return
            # Todo dato recibido refresca el watchdog y confirma que el modelo
            # se ha hecho cargo de la última orden/voz.
            self._last_data_ts = asyncio.get_running_loop().time()

            # Uso de tokens: el servidor envía el acumulado de la sesión.
            usage = getattr(message, "usage_metadata", None)
            if usage is not None:
                if usage.prompt_token_count is not None:
                    self._token_prompt = max(
                        self._token_prompt, usage.prompt_token_count
                    )
                if usage.response_token_count is not None:
                    self._token_response = max(
                        self._token_response, usage.response_token_count
                    )
                if (
                    usage.prompt_token_count is not None
                    or usage.response_token_count is not None
                ):
                    self._safe_call(self.on_tokens, self._token_prompt, self._token_response)

            # Reanudación de sesión (opt-in): guardamos el handle más reciente que
            # publica el servidor. Se usa solo si config.session_resumption
            # está activo; con v1alpha no se emiten handles fiables.
            resume = None
            if self._settings.session_resumption:
                resume = getattr(message, "session_resumption_update", None)
            if resume is not None and getattr(resume, "resumable", False):
                if resume.new_handle:
                    self._session_handle = resume.new_handle

            if message.server_content is not None:
                content = message.server_content

                if content.interrupted:
                    # El usuario habló por encima del modelo: vaciamos el búfer.
                    self._responding = False
                    self._awaiting_turn = False
                    if player is not None:
                        player.flush()
                    self._out_acc.clear()
                    self._in_acc.clear()
                    self._turn_text = ""  # el usuario habló por encima: turno nuevo
                    self._outdone_ts = None
                    if self._web_preempting:
                        # La interrupción la lanzó NUESTRA propia búsqueda web
                        # (para cortar la respuesta "sin internet"): se
                        # conserva la consulta pendiente y la búsqueda inyectará
                        # la nota como turno nuevo. Además NO se salta a
                        # "Listo": el turno sigue en curso (procesando).
                        logger.debug(
                            "Interrupción propia de búsqueda web; consulta "
                            "conservada (se sigue procesando)."
                        )
                        self._web_preempting = False
                    else:
                        # Barge-in real del usuario: la nota web pendiente (si
                        # la había) queda obsoleta; se descarta con su tarea
                        # para no inyectarla en un turno que no la pidió.
                        self._pending_web_query = None
                        if (
                            self._web_search_task is not None
                            and not self._web_search_task.done()
                        ):
                            self._web_search_task.cancel()
                            self._web_search_task = None
                        self._set_state(
                            AssistantState.LISTENING
                            if self._voice_active()
                            else AssistantState.IDLE
                        )
                        self._safe_call(self.on_interrupted)

                data = message.data
                if data:
                    # OJO: antes era ``self._hotkeys.muted`` a pelo. Un
                    # AttributeError aquí dentro DEL BUCLE DE RECEPCIÓN no se
                    # recovera: tumba la sesión entera y el asistente queda
                    # mudo hasta que el watchdog la recicle. Con ``getattr`` un
                    # ``_hotkeys`` ausente degrada a "no silenciado" en vez de
                    # matar el turno.
                    if player is not None and not getattr(
                        self._hotkeys, "muted", False
                    ):
                        player.write(data)
                    self._responding = True
                    self._awaiting_turn = False
                    self._outdone_ts = asyncio.get_running_loop().time()
                    self._mark_response_activity()
                    self._set_state(AssistantState.SPEAKING)

                text = message.text
                if text and content.model_turn is not None:
                    # Solo el texto visible: el pensamiento del modelo no se
                    # muestra en la consola/GUI (ruido de razonamiento).
                    self._responding = True
                    self._awaiting_turn = False
                    self._outdone_ts = asyncio.get_running_loop().time()
                    self._mark_response_activity()
                    spoken = "".join(
                        p.text
                        for p in content.model_turn.parts
                        if p.text and not p.thought
                    )
                    if spoken.strip():
                        self._set_state(AssistantState.SPEAKING)
                        self._safe_call(self.on_text, spoken)
                elif text:
                    self._responding = True
                    self._awaiting_turn = False
                    self._outdone_ts = asyncio.get_running_loop().time()
                    self._mark_response_activity()
                    self._set_state(AssistantState.SPEAKING)
                    self._safe_call(self.on_text, text)

                # Transcripción de lo que el modelo está diciendo (solo audio).
                out_transcript = content.output_transcription
                if out_transcript is not None and out_transcript.text:
                    self._responding = True
                    self._awaiting_turn = False
                    self._outdone_ts = asyncio.get_running_loop().time()
                    self._mark_response_activity()
                    self._set_state(AssistantState.SPEAKING)
                    self._out_acc.append(out_transcript.text)
                    # Acumulador del turno: sobrevive al flush de pantalla y
                    # sirve para detectar cortes al llegar ``turn_complete``.
                    self._turn_text += out_transcript.text
                    if out_transcript.finished:
                        # Orden de visualización: PRIMERO lo que dijo el usuario
                        # (su turno), DESPUÉS la respuesta del modelo. Sin este
                        # flush previo, la respuesta llegaba al chat ANTES que
                        # la transcripción del usuario (mensajes al revés).
                        self._flush_input(session)
                        self._flush_output()

                # Transcripción de lo que el usuario dijo por el micrófono.
                in_transcript = content.input_transcription
                if in_transcript is not None and in_transcript.text:
                    self._set_state(AssistantState.LISTENING)
                    self._in_acc.append(in_transcript.text)
                    if in_transcript.finished:
                        self._flush_input(session)

                if content.turn_complete:
                    await self._end_turn(
                        session,
                        reason=getattr(
                            content, "turn_complete_reason", None
                        ),
                    )
            elif message.go_away is not None:
                logger.info("El servidor solicitó la desconexión (go_away).")
                return

    def _flush_output(self) -> None:
        text = "".join(self._out_acc).strip()
        self._out_acc.clear()
        if text:
            logger.info("RESPUESTA DEL MODELO -> %s", text)
            self._safe_call(self.on_text, text)
            self._remember("assistant", text)

    def _flush_input(self, session) -> None:
        text = "".join(self._in_acc).strip()
        self._in_acc.clear()
        if text:
            logger.info("VOZ DEL USUARIO -> %s", text)
            self._safe_call(self.on_user_text, text)
            self._remember("user", text)
            # La pregunta del turno, para el registro de la Fase 2.
            self._pregunta_turno = text
            # Fase 2: una rectificación deja el turno anterior como ``corrected``.
            self._marcar_correccion(text)
            # Nuevo turno hablado: rondas de autocompletado disponibles de nuevo.
            self._cont_remaining = _CONTINUE_MAX_ROUNDS
            # Integración local POR VOZ (temporizador, volumen, portapapeles):
            # se ejecuta aquí mismo; si aplica, la respuesta del modelo coincide
            # con la acción y no hace falta búsqueda web.
            local_note = self._local_action(text)
            if local_note:
                self._safe_call(self.on_meta, local_note)
                # Con una integración local no hay búsqueda que hacer ni datos que
                # contradecir: la orden ya se resolvió sin modelo.
            else:
                # Aviso de contradicción (Fase 2), pero por voz NO se interrumpe
                # la respuesta para inyectarlo: el barge-in con `_INTERRUPT_TEXT`
                # deja muda la sesión (ver la nota de `cancel`), que es un precio
                # altísimo por una frase. Se marca en disputa, se le dice al
                # usuario en el HUD, y la sección `[En disputa]` del system
                # prompt hace que pregunte en cuanto haya una conexión.
                try:
                    aviso_disputa = self._detectar_disputa(text)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("No se pudo revisar la memoria del turno: %s", exc)
                    aviso_disputa = ""
                if aviso_disputa:
                    self._safe_call(
                        self.on_meta,
                        "(Memoria) Eso contradice algo que tenías guardado; "
                        "te lo pregunto en cuanto pueda hablar.",
                    )
                if self._settings.web_search_enabled:
                    self._schedule_voice_web(session, text)

    async def _end_turn(self, session, reason=None) -> None:
        """Cierra el turno de respuesta en curso y decide si continuar.

        Punto único de finalización (lo invocan ``turn_complete`` y el watchdog
        cuando la transcripción terminó y no llegó ``turn_complete``). Entrega al
        cliente el texto del modelo (y mi voz antes que la respuesta: ver la nota
        en ``out_transcript``), limpia el estado, avisa a ``on_turn_complete`` y,
        si la respuesta parecía cortada por el tope del servidor, pide su
        continuación exactamente donde se quedó. La guarda ``_closing_turn``
        evita cerrar dos veces el mismo turno (receive loop + watchdog a la vez).

        ``reason`` es el ``TurnCompleteReason`` que envió el servidor (o None si
        el cierre lo decidió el watchdog porque no llegó ``turn_complete``). Si
        el motivo es un rechazo/bloqueo de seguridad, NO se pide continuación y
        se avisa al usuario de por qué terminó la respuesta.
        """
        if self._closing_turn:
            # Reentrada mientras se cierra (race watchdog/turn_complete): solo
            # se entrega cualquier texto adicional que pudiera haber llegado.
            self._flush_output()
            return
        self._closing_turn = True
        # Un turno cerrado cuenta como "la voz funciona": rearma el tope
        # absoluto del watchdog para una sesión de voz muy larga. Se rearma con
        # la hora actual (y no a None) para que la red de seguridad siga
        # contando: si el micrófono queda abierto y ya no cierra ningún turno,
        # ``_VOICE_MAX_OPEN_S`` sigue reciclando la sesión.
        self._voice_opened_ts = (
            asyncio.get_running_loop().time() if self._voice_active() else None
        )
        try:
            # ¿Cierre por rechazo/bloqueo (seguridad) en vez de corte natural?
            reason_val = getattr(reason, "value", reason)
            rejected = str(reason_val or "").upper() in _TURN_REJECT_REASONS
            self._responding = False
            self._awaiting_turn = False
            # El turno ya está atendido: el audio guardado por si había que
            # reenviarlo deja de ser válido (si no, un estancamiento posterior
            # reenviaría una frase vieja). El contador de intentos va con él: es
            # de ESTE turno y, sin rearme, el watchdog quedaba sin reenvío de voz
            # para el resto de la sesión en cuanto se usaba una vez.
            self._voice_replay = None
            self._voice_replay_tries = 0
            # Usuario antes que modelo (ver nota en out_transcript).
            self._flush_input(session)
            self._flush_output()
            # ¿La respuesta larga se cortó por el tope del servidor?
            # (finito: sólo lo detectan respuestas MUY largas que acaban mal —sin
            # puntuación final o cortadas a la mitad— y hay un límite de rondas
            # por turno de usuario). Se usa ``_turn_text`` (acumulador del turno)
            # y NO ``_out_acc``, que se vacía al mostrar el texto en el chat.
            pending = (self._turn_text or "").strip()
            should_continue = (
                pending
                and not rejected
                and self._cont_remaining > 0
                and self._pending_web_query is None
                and (
                    self._web_search_task is None
                    or self._web_search_task.done()
                )
                and self._is_truncated(pending)
            )
            self._set_state(
                AssistantState.LISTENING
                if self._voice_active()
                else AssistantState.IDLE
            )
            self._safe_call(self.on_turn_complete)
            # Registro del turno (Fase 2): ya se sabe cómo acabó, así que es
            # el momento de anotarlo para la reflexión. Un turno rechazado se
            # marca como callejón sin salida (no es culpa de la respuesta) y se
            # limpia la pregunta para no arrastrarla al turno siguiente.
            pregunta_turno = (self._pregunta_turno or "").strip()
            if pregunta_turno:
                self.registrar_giro(
                    pregunta_turno,
                    "dead_end" if rejected else "useful",
                )
            self._pregunta_turno = ""
            self._ultima_pregunta = pregunta_turno
            # Los contadores de refuerzo del grafo se aplazaron durante el turno
            # (escribir a disco en cada recuperación costaba un cuarto de
            # frame). Aquí, al terminar el turno, se vuelcan.
            indice = self._memoria_indice
            if indice is not None:
                try:
                    indice._vaciar_pendientes()
                except Exception as exc:  # noqa: BLE001 - es contadores
                    logger.debug("No se volcaron los contadores del grafo: %s", exc)
            self._outdone_ts = None
            if rejected:
                self._safe_call(
                    self.on_meta,
                    "(MindVoice) El modelo detuvo la respuesta por considerar "
                    "el contenido no apto o rechazarlo: el turno quedó cerrado.",
                )
            turn_begin = self._turn_begin
            turn_first_audio = self._turn_first_audio
            if turn_begin is not None:
                total = time.monotonic() - turn_begin
                if turn_first_audio is not None:
                    logger.info(
                        "Métrica: turno en %.2f s, primera respuesta a %.2f s",
                        total,
                        turn_first_audio - turn_begin,
                    )
                else:
                    logger.info("Métrica: turno en %.2f s, sin contenido", total)
                self._safe_call(
                    self.on_meta,
                    f"(Respondida en {total:.1f} s).",
                )
            if should_continue:
                self._cont_remaining -= 1
                self._turn_text = ""
                await self._request_continuation(session)
            else:
                self._turn_text = ""
                self._turn_begin = None
                self._turn_first_audio = None
            # La búsqueda web pedida por voz ya corre por su cuenta (ver
            # ``_launch_voice_web``): aquí ya no hay nada que inyectar.
        finally:
            self._closing_turn = False
            # Si el usuario cambió la voz/idioma: la nueva sesión se abre con la
            # configuración fresca justo al cerrar este turno (memoria intacta).
            self._maybe_reconnect()

    def _transcript_langs(self) -> list:
        """Códigos BCP-47 para la transcripción de audio del modelo."""
        code = (getattr(self._settings, "transcript_lang", None) or "es-ES").strip()
        return [code]

    def _stamp_turn_begin(self) -> None:
        """Marca el inicio de un turno de usuario (para las métricas de E4)."""
        self._turn_begin = time.monotonic()
        self._turn_first_audio = None
        self._outdone_ts = None

    def _mark_response_activity(self) -> None:
        """Registra la PRIMERA respuesta del modelo dentro del turno (E4).

        Se invoca en cada contenido/audio/texto que llega mientras se responde;
        la guarda interna hace que el log de latencia solo se emita una vez.
        """
        if self._turn_begin is not None and self._turn_first_audio is None:
            self._turn_first_audio = time.monotonic()
            logger.info(
                "Métrica: primera respuesta del modelo a %.2f s del turno",
                self._turn_first_audio - self._turn_begin,
            )

    @staticmethod
    def _is_truncated(text: str) -> bool:
        """Heurística CONSERVADORA de respuesta cortada por el servidor.

        Solo marca respuestas Muy largas (>= ``_CONTINUE_MIN_CHARS``) que no
        terminan con puntuación de cierre (``.`` ``!`` ``?`` ``…``) o que
        terminan con coma/punto y coma/dos puntos/guion (corte duro a mitad
        de frase). Las respuestas normales del modelo cierran con punto o
        signo de interrogación y nunca disparan esto.
        """
        t = (text or "").strip()
        if len(t) < _CONTINUE_MIN_CHARS:
            return False
        if not t:
            return False
        last = t[-1]
        if last in ".!?…\"”')":
            return False
        if last in ",;:–—-":
            return True
        # Muy larga y sin signo de cierre: casi con seguridad recortada.
        return True

    async def _request_continuation(
        self,
        session,
        anchor: Optional[str] = None,
    ) -> bool:
        """Pide al modelo que continúe la respuesta que parecía cortada.

        Con ``anchor`` (cola de una respuesta interrumpida por una caída de
        sesión) se incluye el último tramo hablado para enganchar EXACTO donde
        quedó el audio, sin repetir lo ya dicho.
        """
        self._last_data_ts = asyncio.get_running_loop().time()
        self._stamp_turn_begin()
        if anchor:
            anchor_txt = (anchor or "").strip()
            continuation = (
                "(El usuario pidió una respuesta completa y la sesión se "
                "interrumpió a mitad de habla. Continúa EXACTAMENTE desde el "
                f"último tramo que dijiste —> '{anchor_txt[-160:]}' —, termina "
                "el punto que desarrollabas y cierra con un punto. No repitas "
                "lo ya dicho.)"
            )
        else:
            continuation = _CONTINUE_TEXT
        self._safe_call(
            self.on_meta, "(La respuesta parecía cortada: la hago continuar…)"
        )
        try:
            await session.send_client_content(
                turns=[
                    types.Content(
                        role="user",
                        parts=[types.Part(text=continuation)],
                    )
                ],
                turn_complete=True,
            )
        except Exception as exc:  # noqa: BLE001 - el turno ya pudo cerrar
            logger.debug("No se pudo autocompletar la respuesta: %s", exc)
            self._awaiting_turn = False
            self._responding = False
            self._outdone_ts = None
            self._turn_text = ""
            self._turn_begin = None
            self._turn_first_audio = None
            self._set_state(
                AssistantState.LISTENING
                if self._voice_active()
                else AssistantState.IDLE
            )
            return False
        self._awaiting_turn = True
        self._last_data_ts = asyncio.get_running_loop().time()
        self._set_state(AssistantState.PROCESSING)
        return True

    def _maybe_math_note(self, text: str) -> Optional[str]:
        """Resuelve una cuenta escrita en español y devuelve la nota, o None.

        Solo actúa sobre frases cortas que, recortado el verbo de pregunta
        ("cuánto es", "calcula", …), quedan como una expresión puramente
        aritmética (dígitos y operadores). Si no es claramente una cuenta,
        devuelve ``None`` y la orden sale sin nota.
        """
        t = (text or "").strip()
        if not t or len(t) > 90:
            return None
        preview = _MATH_PREFIX_RE.sub("", t).strip(" ¿?")
        if not preview or not re.search(r"\d", preview):
            return None
        expr = preview.lower().replace(",", ".")
        for word, repl in _MATH_SUBST:
            expr = expr.replace(word, repl)
        expr = expr.replace("×", "*").replace("÷", "/").replace("·", "*")
        expr = re.sub(r"(?i)(\d)\s*x\s*(\d)", r"\1*\2", expr)
        expr = expr.replace("^", "**")
        # "X% de Y" → (X/100)*Y ; "X%" a secas → X/100
        expr = re.sub(
            r"(?i)(\d+(?:\.\d+)?)\s*%\s*(?:de|del)\s*(\d+(?:\.\d+)?)",
            r"(\1/100)*\2",
            expr,
        )
        expr = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", expr)
        expr = re.sub(r"\s+", "", expr)
        if not re.fullmatch(r"[0-9+\-*/%().]+", expr):
            return None
        result = self._safe_math(expr)
        if result is None:
            return None
        return (
            f"(Cálculo exacto resuelto por la app: {preview} = {result}. "
            f"Es un dato confirmado: úsalo si el usuario preguntó por esta "
            f"cuenta y NO vuelvas a calcularlo.)"
        )

    def _safe_math(self, expr: str) -> Optional[str]:
        """Evalúa la expresión aritmética con un AST validado de SOLO números.

        Se ejecuta ``eval`` estrictamente después de comprobar que el árbol
        contiene únicamente constantes numéricas y operadores aritméticos
        (sin nombres, atributos ni llamadas), así que es inseguro de romper.
        """
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError:
            return None
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant):
                if not isinstance(node.value, (int, float)):
                    return None
            elif not isinstance(
                node,
                (
                    ast.Expression,
                    ast.BinOp,
                    ast.UnaryOp,
                    ast.Constant,
                    ast.Add,
                    ast.Sub,
                    ast.Mult,
                    ast.Div,
                    ast.Mod,
                    ast.Pow,
                    ast.USub,
                    ast.UAdd,
                ),
            ):
                return None
        try:
            value = float(eval(compile(tree, "<calc>", "eval")))
            if value != value or abs(value) == float("inf"):
                return None
        except Exception:  # noqa: BLE001 - expresión no evaluable
            return None
        return format(value, ".10g")