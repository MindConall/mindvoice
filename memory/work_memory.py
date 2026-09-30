"""Memoria de trabajo: lo que se aprende entre sesiones.

Fase 2. Tres piezas:

* **Reflexión**: con las señales guardadas por ``graphify save-result``, que son
  _trazas_ comprobables ("esto sirvió", "esto fue un callejón sin salida",
  "esto estaba mal"), se genera una lección por tema. Determinista y local: sin
  red y sin LLM, como el resto del módulo.
* **Contradicciones**: cuando dos recuerdos del mismo tema se contradicen ("mi
  perro se llama Nube" / "mi perro se llama Bigotes"), no se elige un ganador en
  silencio. Se marcan ambos como ``contested`` y se avisa, para que el usuario
  decida.
* **Perfil**: las preferencias que el usuario repite (idioma, cómo quiere que le
  hablen) sePromueven aparte y Travels en la nota, porque son las que más
  cambian el tono de la respuesta.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

from .base import MemoryEntry, normalizar, palabras, recortar
from .graph_backend import GraphBackend

logger = logging.getLogger(__name__)

# Cuántos turnos de memoria de trabajo se guardan antes de resumirlos.
TRACES_MAX = 40
# Una señal pesa la mitad cada este número de días (mismo criterio que
# ``graphify reflect --half-life-days``).
MEDIA_SENAL_DIAS = 30.0
# Dos recuerdos se consideran del mismo tema si comparten esto.
UMBRAL_TEMA = 0.34
# Cuántos nodos se menacing en una lección.
LECCION_NODOS = 12

UTIL = "useful"
CALLEJON = "dead_end"
CORREGIDO = "corrected"


# --------------------------------------------------------------------- trazas
class Traza:
    """Un turno de memoria de trabajo: qué se preguntó y cómo acabó."""

    __slots__ = ("pregunta", "resultado", "nodos", "correccion", "cuando")

    def __init__(self, pregunta, resultado=UTIL, nodos=None, correccion="", cuando=0.0):
        self.pregunta = (pregunta or "").strip()
        self.resultado = resultado
        self.nodos = list(nodos or [])
        self.correccion = (correccion or "").strip()
        self.cuando = cuando or time.time()

    def a_dict(self) -> dict:
        return {
            "pregunta": self.pregunta,
            "resultado": self.resultado,
            "nodos": self.nodos,
            "correccion": self.correccion,
            "cuando": self.cuando,
        }

    @classmethod
    def desde(cls, dato: dict) -> "Traza":
        return cls(
            dato.get("pregunta", ""),
            dato.get("resultado", UTIL),
            dato.get("nodos") or [],
            dato.get("correccion", ""),
            dato.get("cuando") or time.time(),
        )

    def peso(self, ahora: float | None = None) -> float:
        """La señal pierde fuerza con el tiempo, igual que en Graphify."""
        ahora = ahora or time.time()
        dias = max(0.0, (ahora - self.cuando) / 86400.0)
        return 0.5 ** (dias / MEDIA_SENAL_DIAS)

    def vale(self) -> bool:
        """¿Esta traza se puede usar para aprender algo?"""
        return bool(self.pregunta) and self.resultado in (UTIL, CALLEJON, CORREGIDO)


class MemoriaTrabajo:
    """Registro de turnos y lo que se aprende de ellos."""

    def __init__(self, directorio: str, trazas_max: int = TRACES_MAX) -> None:
        self._dir = directorio
        self._ruta = os.path.join(directorio, "work-memory.json")
        self._traza_max = trazas_max
        self._trazas: list[Traza] = []
        self.cargar()

    def cargar(self) -> None:
        try:
            with open(self._ruta, encoding="utf-8") as fh:
                bruto = json.load(fh)
        except (OSError, ValueError):
            self._trazas = []
            return
        trazas = []
        for dato in bruto.get("trazas", []) if isinstance(bruto, dict) else []:
            if isinstance(dato, dict):
                t = Traza.desde(dato)
                if t.vale():
                    trazas.append(t)
        self._trazas = trazas[-self._traza_max:]

    def _guardar(self) -> None:
        try:
            os.makedirs(self._dir, exist_ok=True)
            temporal = self._ruta + ".tmp"
            with open(temporal, "w", encoding="utf-8") as fh:
                json.dump(
                    {"trazas": [t.a_dict() for t in self._trazas[-self._traza_max:]]},
                    fh,
                    ensure_ascii=False,
                    indent=1,
                )
            os.replace(temporal, self._ruta)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("No se pudo guardar la memoria de trabajo: %s", exc)

    def registrar(
        self, pregunta, resultado: str = UTIL, nodos=None, correccion: str = ""
    ) -> Traza | None:
        """Apunta un turno. Los resultados desconocidos se ignoran."""
        traza = Traza(pregunta, resultado, nodos, correccion)
        if not traza.vale():
            logger.debug("Traza ignorada (resultado %r).", resultado)
            return None
        self._trazas.append(traza)
        del self._trazas[: max(0, len(self._trazas) - self._traza_max)]
        self._guardar()
        return traza

    def forget(self, pregunta: str) -> int:
        """Quita las trazas que mencionen esa pregunta. Devuelve cuántas."""
        clave = normalizar(pregunta)
        antes = len(self._trazas)
        self._trazas = [t for t in self._trazas if clave not in normalizar(t.pregunta)]
        self._guardar()
        return antes - len(self._trazas)

    def trazas(self) -> list[Traza]:
        return list(self._trazas)


# ------------------------------------------------------------------ lecciones
def _temas(texto: str) -> set[str]:
    return palabras(texto)


def _similitud(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def lecciones(registro: MemoriaTrabajo) -> dict:
    """Agrupa las trazas por tema y saca una lección por grupo.

    Determinista: mismo registro, misma salida. Sin red ni LLM, para que la
    reflexión no dependa de que haya red ni de cuánto cueste.
    """
    ahora = time.time()
    grupos: dict[str, dict] = {}
    for traza in registro.trazas():
        for tema in _temas(traza.pregunta):
            g = grupos.setdefault(
                tema, {"tema": tema, "util": 0.0, "callejon": 0.0, "corregido": 0.0,
                       "correciones": [], "preguntas": 0, "nodos": set()}
            )
            peso = traza.peso(ahora)
            g["preguntas"] += 1
            g["nodos"].update(traza.nodos)
            if traza.resultado == UTIL:
                g["util"] += peso
            elif traza.resultado == CALLEJON:
                g["callejon"] += peso
            elif traza.resultado == CORREGIDO:
                g["corregido"] += peso
            if traza.correccion:
                g["correciones"].append(traza.correccion)

    salida = []
    for g in grupos.values():
        # Una lección solo se escribe si hay una señal clara: al menos dos
        # aciertos, o un acierto, o una corrección.
        if g["util"] < 1.0 and g["corregido"] < 0.5 and g["callejon"] < 1.5:
            continue
        veredicto = _veredicto(g)
        salida.append(
            {
                "tema": g["tema"],
                "util": round(g["util"], 2),
                "callejon": round(g["callejon"], 2),
                "corregido": round(g["corregido"], 2),
                "preguntas": g["preguntas"],
                "leccion": veredicto,
                "correciones": g["correciones"][:3],
            }
        )
    salida.sort(key=lambda x: -(x["util"] + x["corregido"] + x["callejon"]))
    return {"lecciones": salida[:LECCION_NODOS], "trazas": len(registro.trazas())}


def _veredicto(g: dict) -> str:
    """Traduce los números a una frase que una persona pueda leer."""
    if g["corregido"] >= 1.0:
        detalle = f"hay {int(g['corregido'])} correccion(es) del usuario"
    elif g["callejon"] >= 1.5:
        detalle = f"este tema dio {int(g['callejon'])} callejon(es) sin salida"
    else:
        detalle = f"funciona bien ({int(g['util'])} turno(s))"
    return f"«{g['tema']}»: {detalle}."


def render_lecciones(calculo: dict) -> str:
    """El texto de las lecciones que se puede añadir a la nota del prompt."""
    filas = [l for l in calculo.get("lecciones", []) if l["leccion"]]
    if not filas:
        return ""
    return "[Lecciones aprendidas]\n" + "\n".join(f"- {l['leccion']}" for l in filas)


# ---------------------------------------------------------- contradicciones
def _contradiccion(a: MemoryEntry, b: MemoryEntry) -> bool:
    """¿Dos recuerdos del mismo tema se contradicen?

    El patrón que se busca es "el mismo marco, distinto valor": dos frases que
    comparten casi todo y se diferencian en una o dos palabras. "Se llama Nube"
    frente a "se llama Bigotes". Cuanto más se parecen, más probable es que
    estén disputando el mismo dato y no hablando de cosas distintas.
    """
    ta, tb = normalizar(a.text), normalizar(b.text)
    # Una dentro de la otra es redundancia, no contradicción.
    if ta in tb or tb in ta:
        return False
    # Solo tiene sentido si ambas afirman un valor con la misma fórmula.
    if not (re.search(r"\b(llama|llamado|nombre)\b", ta) and re.search(r"\b(llama|llamado|nombre)\b", tb)):
        return False
    ca, cb = _temas(a.text), _temas(b.text)
    distintos = ca ^ cb
    # Una contradicción razonable se diferencia en una o dos palabras. Si se
    # diferencian en diez, son dos recuerdos distintos y no un choque.
    if not (1 <= len(distintos) <= 3):
        return False
    return True


def detectar_contradicciones(grafo: GraphBackend) -> list[dict]:
    """Marca como ``contested`` los pares que se contradicen y avisa.

    No elige ganador: solo levanta la bandera y deja constancia, porque decidir
    cuál de los dos Says la verdad es asunto del usuario.
    """
    if not isinstance(grafo, GraphBackend):
        return []
    encontrados = []
    nodos = [n for n in grafo._grafo["nodes"] if n.get("kind") in ("brief", "permanent")]
    for i, na in enumerate(nodos):
        ea = grafo._a_entry(na)
        for nb in nodos[i + 1:]:
            eb = grafo._a_entry(nb)
            ta, tb = _temas(ea.text), _temas(eb.text)
            if _similitud(ta, tb) < UMBRAL_TEMA:
                continue
            if _contradiccion(ea, eb):
                grafo.marcar_disputa(na["id"], True)
                grafo.marcar_disputa(nb["id"], True)
                encontrados.append(
                    {"a": na["id"], "b": nb["id"], "a_texto": ea.text, "b_texto": eb.text}
                )
    if encontrados:
        logger.info("Memoria: %d contradiccion(es) marcadas para revision.", len(encontrados))
    return encontrados


# ------------------------------------------------------------------- perfil
_REGLAS_PERFIL = (
    # (expresión, etiqueta)
    (r"\bresponde?(me)?\s+(?:en\s+)?(?:castellano|espa(ñ|n)ol|ingl(é|e)s|franc(é|e)s|portugu(é|e)s)\b", "idioma"),
    (r"\b(?:sempre|siempre)\s+(?:en|con)\s+castellano\b", "idioma"),
    (r"\b(?:brevity|breve|corto|corta|conciso|concisa|resumido)\b", "tono"),
    (r"\bno me (?:digas|respondas|quiero)\b", "tono"),
    (r"\b(?:usa|habla|hablame)\s+(?:en\s+)?(?:catal(ó|a)n|gallego|vasco)\b", "idioma"),
)


def detectar_perfil(entradas) -> list[dict]:
    """Saca las preferencias de estilo que el usuario ha pedido de verdad.

    Solo cuenta una frase que aparece con la forma de una orden ("responde en
    inglés"), no cualquier coincidencia suelta. Es a propósito conservador: un
    perfil equivocado cambia el tono de todas las respuestas.
    """
    cuenta: dict[str, int] = {}
    muestras: dict[str, str] = {}
    for e in entradas:
        texto = (getattr(e, "text", "") or "").lower()
        for patron, etiqueta in _REGLAS_PERFIL:
            if re.search(patron, texto):
                cuenta[etiqueta] = cuenta.get(etiqueta, 0) + 1
                muestras.setdefault(etiqueta, getattr(e, "text", ""))
    salida = []
    for etiqueta, veces in cuenta.items():
        # Una sola vez puede ser casualidad; dos ya son una costumbre.
        if veces < 2:
            continue
        salida.append({"tipo": etiqueta, "veces": veces, "muestra": recortar(muestras[etiqueta], 120)})
    return salida


def render_perfil(perfil) -> str:
    filas = [p for p in perfil if p.get("veces", 0) >= 2]
    if not filas:
        return ""
    partes = ", ".join(f"{p['tipo']} (x{p['veces']})" for p in filas)
    return f"[Cómo quieres que te hable]\n{partes}"


def nota_ampliada(
    bloque: str,
    registro: MemoriaTrabajo | None = None,
    grafo: GraphBackend | None = None,
    entradas=None,
) -> str:
    """Añade lecciones y perfil al bloque, sin quitarle nada.

    Lo que ya iba al prompt se conserva; lo nuevo va detrás y en secciones
    propias, para que quede claro qué es recuerdo y qué es lección.
    """
    partes = [bloque] if bloque else []
    if entradas is not None:
        perfil = detectar_perfil(entradas)
        if perfil:
            partes.append(render_perfil(perfil))
    if registro is not None:
        render = render_lecciones(lecciones(registro))
        if render:
            partes.append(render)
    return "\n\n".join(partes)