"""Primitivas comunes de la memoria de MindVoice.

Define el contrato que cualquier backend cumple y cómo se elige cuál usar.

Regla de oro del módulo: la memoria es una optimización de contexto, nunca una
fuente de la que dependa el flujo de respuestas. Si un backend falla, el sistema
cae al plano y deja rastro en el log; nunca se pierde una conversación.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Valores de procedencia. ``EXTRACTED`` es un hecho que el usuario dijo de forma
# literal; ``INFERRED`` es una conclusión que el sistema dedujo; ``contested``
# marca los que el usuario corrigió y aún no se han resuelto.
EXTRACTED = "EXTRACTED"
INFERRED = "INFERRED"
CONTESTED = "contested"

# Marcas de procedencia que se añaden al final de la línea en la nota del prompt.
# La Fase 2 lo pide explícitamente ("explica de dónde sacas un recuerdo"): sin
# esto el modelo recite un dato sin poder decir de dónde lo sacó, y el usuario
# no puede ni auditarlo. Son cortas a propósito: cuestan tokens en cada turno y
# el bloque entero se mide contra el presupuesto.
MARCA_DICHO = "· tú lo dijiste"
MARCA_INFERIDO = "· deducido"

# Encabezado de la sección de memoria en disputa. El modelo recibe aquí la orden
# de PREGUNTAR en vez de asumir, que es lo que convierte una contradicción
# detectada en un comportamiento visible y no en un silencio.
DISPUTA_HEADING = "[En disputa: hay recuerdos que se contradicen]"

# Variables de entorno que fuerzan un backend sin tocar código.
ENV_BACKEND = "MINDVOICE_MEMORY"
ENV_GRAPH_DIR = "MINDVOICE_MEMORY_GRAPH"

# Tope de caracteres de una nota de memoria inyectada en el prompt.
ENTRY_MAX_CHARS = 400
# Presupuesto de caracteres del bloque completo inyectado al system prompt.
BLOCK_BUDGET = 6000

_PALABRAS_VACIAS = frozenset(
    """a al algo algun alguna algunas alguno algunos ante antes como con contra cual
    cuando de del desde donde dos el ella ellas ellos en entre era erais eran eres
    esa esas ese eso esos esta estaba estan estas este esto estos fue fui ha habia
    han hasta hay la las le les lo los mas me mi mis mucho muy nada ni no nos
    nosotros o os otra otras otro otros para pero poco por porque que quien se
    sea si sin sobre solo son su sus te tiene tienen todo todos tu tus un una uno
    unos y ya yo""".split()
)


def normalizar(texto: str) -> str:
    """Minúsculas sin acentos: así se comparan y buscan los textos."""
    plano = unicodedata.normalize("NFKD", texto or "").lower()
    return "".join(c for c in plano if not unicodedata.combining(c))


# Caché de tokenización. Se vacía de golpe al llenarse (en vez de avanzar) para
# que nunca cresca sin límite en una sesión larga.
_CACHE_PALABRAS: dict[tuple[str, int], set[str]] = {}
_CACHE_PALABRAS_MAX = 4000


def palabras(texto: str, min_len: int = 4) -> set[str]:
    """Palabras significativas de un texto, ya normalizadas."""
    clave = (texto or "", min_len)
    # La búsqueda por grafo llama a esto una vez por nodo y en cada
    # recuperación: sin caché, con 400 recuerdos son 80.000 normalizaciones por
    # pregunta (la mitad del coste de recuperar). El texto de un recuerdo no
    # cambia una vez guardado, así que cachearlo es seguro.
    hit = _CACHE_PALABRAS.get(clave)
    if hit is not None:
        return hit
    crudos = re.findall(r"[\w]+", normalizar(texto), flags=re.UNICODE)
    resultado = {p for p in crudos if len(p) >= min_len and p not in _PALABRAS_VACIAS}
    if len(_CACHE_PALABRAS) >= _CACHE_PALABRAS_MAX:
        _CACHE_PALABRAS.clear()
    _CACHE_PALABRAS[clave] = resultado
    return resultado


def ahora_iso() -> str:
    """Marca temporal en ISO-8601 UTC, como espera el esquema del grafo."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def antiguedad_dias(iso: str) -> float:
    """Días desde una marca ISO. Infinito si la marca no se entiende."""
    if not iso:
        return float("inf")
    try:
        marca = datetime.fromisoformat(iso)
    except ValueError:
        return float("inf")
    if marca.tzinfo is None:
        marca = marca.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - marca).total_seconds() / 86400.0)


def recortar(texto: str, limite: int = ENTRY_MAX_CHARS) -> str:
    """Recorta con elipsis, igual que hacía el bloque plano."""
    texto = (texto or "").strip()
    return texto if len(texto) <= limite else texto[:limite] + "…"


# Cuántas etiquetas automáticas se sacan de un recuerdo, y de qué longitud.
ETIQUETAS_MAX = 5
_ETIQUETA_MIN_LEN = 5

# Verbos y auxiliares muy comunes: sin esta lista "tengo" o "llama" salían
# como etiquetas y no sirven para encontrar nada. No es una lista cerrada, solo
# lo que más se repite en una conversación normal.
_VERBOS_COMUNES = frozenset(
    """puede puedo tengo tiene tienen hacen hacer hace hice decir dijo creo
    cree queda quedan quiere querer saber pregunta preguntar gusta gustar
    parece existir existe hay entiende empieza terminar trabajo trabajar
    mirar ayuda ayudar pasar quedar ver viendo usa usar deben deber ser
    estar siendo tener teniendo poder pueden puedes esto este aqui alli
    entonces ahora siempre nunca tambien ademas entonces2""".split()
)


def autoetiquetas(texto: str, cuantas: int = ETIQUETAS_MAX) -> list[str]:
    """Saca los términos que un recuerdo da por supuestos.

    Sin esto el grafo sería una bolsa de nodos sin relaciones: no habría nada
    que recorrer, y la recuperación por tema (preguntar por "el perro" y
    encontrar "Nube") no existiría.

    Entran dos clases de términos:

    * palabras largas y sin repetir, que en Castellano son sobre todo las que
      nombran cosas ("perro", "espresso", "proyecto");
    * nombres propios y palabras con cifras, aunque sean cortas ("Nube", "Ana",
      "PyQt6"), porque son justo los más difíciles de recuperar por otra vía.
    """
    crudo = texto or ""
    cuenta: dict[str, int] = {}

    for palabra in re.findall(r"[\w]+", normalizar(crudo), flags=re.UNICODE):
        if len(palabra) < _ETIQUETA_MIN_LEN or palabra in _PALABRAS_VACIAS:
            continue
        if palabra in _VERBOS_COMUNES or palabra.isdigit():
            continue
        cuenta[palabra] = cuenta.get(palabra, 0) + 1

    # Nombres propios: en mayúscula. Al principio de la frase solo se acepta si
    # no es un verbo o una palabra vacía, porque ahí la mayúscula no significa
    # nada ("Tengo" no es un nombre). En medio de la frase, la mayúscula sí es
    # una pista fiable.
    for i, palabra in enumerate(re.findall(r"[^\s.,;:!?()\[\]]+", crudo)):
        if not palabra[:1].isupper() or palabra.isupper():
            continue
        clave = normalizar(palabra)
        if len(clave) < 3 or clave in _PALABRAS_VACIAS:
            continue
        if i == 0 and clave in _VERBOS_COMUNES:
            continue
        cuenta.setdefault(clave, cuenta.get(clave, 0) + 2)

    # A empates, se queda la más corta: "perro" antes que "perros".
    orden = sorted(cuenta.items(), key=lambda kv: (-kv[1], len(kv[0])))
    return [p for p, _ in orden[:cuantas]]


def similitud(a: str, b: str) -> float:
    """Cuánto se parecen dos textos, de 0 a 1 (Jaccard sobre palabras)."""
    pa, pb = palabras(a), palabras(b)
    if not pa or not pb:
        return 0.0
    return len(pa & pb) / len(pa | pb)


@dataclass
class MemoryEntry:
    """Un recuerdo. El mismo shape en plano y en grafo."""

    id: str
    text: str
    role: str = "user"  # "user" | "assistant" | "system"
    kind: str = "brief"  # "brief" | "permanent"
    importance: float = 0.5  # 0..1, la/sbinopsis
    confidence: str = EXTRACTED
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=ahora_iso)
    hits: int = 0  # veces que la recuperación la eligió (realimentación)
    contested: bool = False

    @property
    def norm_label(self) -> str:
        """Clave de comparación, como el ``norm_label`` del grafo de Graphify."""
        return re.sub(r"\s+", " ", normalizar(self.text)).strip()

    def a_dict(self) -> dict:
        datos = asdict(self)
        datos.pop("norm_label", None)
        return datos


class MemoryBackend(ABC):
    """Contrato de la memoria.

    ``retrieve`` es el método que las fases siguientes van a optimizar: recibe la
    pregunta del turno y devuelve lo que merece la pena mandar al modelo, ya
    recortado al presupuesto.
    """

    nombre = "abstracto"

    @abstractmethod
    def remember(self, text: str, role: str = "user", **campos) -> MemoryEntry | None:
        """Guarda un recuerdo y lo devuelve (o ``None`` si no se guardó)."""

    @abstractmethod
    def retrieve(
        self, query: str = "", limite: int = 8, presupuesto: int = BLOCK_BUDGET
    ) -> list[MemoryEntry]:
        """Devuelve los recuerdos más útiles para ``query``, ya recortados."""

    @abstractmethod
    def forget(self, entry_id: str) -> bool:
        """Olvida un recuerdo concreto. ``True`` si algo se borró."""

    @abstractmethod
    def export(self) -> str:
        """Volca la memoria completa como JSON legible (para el usuario)."""

    @abstractmethod
    def reset(self) -> bool:
        """Borra la memoria efímera conservando lo persistente, si lo hay."""

    def block(self, query: str = "", presupuesto: int = BLOCK_BUDGET) -> str:
        """Texto final para el system prompt.

        Es la misma firma en los dos backends para que el cambio sea invisible
        desde fuera.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Auditoría (Fase 2). No son abstractas a propósito: son la vía de las
    # preguntas que la app tiene que poder contestar ("¿qué recuerdas de X?",
    # "¿de dónde sabes eso?", "olvida lo de X"), y un backend que no sepa
    # devolver una lista vacía no puede impedir que se le pregunte. Sin esto,
    # con la memoria en plano esas órdenes no tendrían respuesta y el motor
    # rompería con AttributeError en mitad de la voz.
    # ------------------------------------------------------------------
    def buscar(self, texto: str, limite: int = 8) -> list[MemoryEntry]:
        """Recuerdos que más se parecen a ``texto``, de más a menos parecido."""
        return []

    def forget_matching(self, texto: str, limite: int = 8) -> list[str]:
        """Olvida lo que hable de ``texto``. Devuelve los textos borrados.

        Es el "olvida" fino: el de toda la vida borra todo de golpe, y con la
        memoria en grafo hace falta poder quitar un tema sin perder el resto.
        """
        return []

    def en_disputa(self) -> list[MemoryEntry]:
        """Recuerdos que el usuario contradijo y que siguen sin resolverse."""
        return []


def backend_forzado() -> str | None:
    """Backend pedido por entorno, si lo hay."""
    valor = (os.environ.get(ENV_BACKEND) or "").strip().lower()
    if valor in ("flat", "plano", "graph", "grafo"):
        return "flat" if valor in ("flat", "plano") else "graph"
    if valor:
        logger.warning("%s=%r no es un backend válido; se ignora.", ENV_BACKEND, valor)
    return None