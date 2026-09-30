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
# Cuántos nodos se mira para extraer una lección.
LECCION_NODOS = 12
# Tope de comparaciones del barrido de contradicciones al arrancar. Con la
# memoria real del usuario hay miles de etiquetas, así que sin este tope el
# barrido completo no cabe en el tiempo de arranque. Ver
# ``detectar_contradicciones``.
PARES_MAX = 4000
# Cuántos recuerdos se comparan con lo que el usuario acaba de decir para ver si
# lo contradice. Ocho porque es lo que cabe en una respuesta corta: más que esto
# y la nota de "tienes un dato en disputa" se vuelve un monólogo.
CHOCQUE_CANDIDATOS = 8

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
# Solo tiene sentido marcadas como contradictorias dos frases que affirmen un
# valor con la MISMA fórmula. "se llama Nube" contra "se llama Bigotes" sí;
# "se llama Nube" contra "vive en Bilbao" no son dos versiones del mismo dato.
# Se compila una vez porque esto está en el bucle de comparación.
_RE_MISMA_FORMULA = re.compile(r"\b(llama|llamado|nombre)\b")


def _firma(entrada: MemoryEntry) -> tuple[str, set[str], bool]:
    """Lo comparable de un recuerdo, calculado UNA vez.

    Existe por rendimiento y no por gusto. El barrido de contradicciones
    compara cada recuerdo con decenas de otros, y antes de esto cada pareja
    repetía cuatro normalizaciones y dos tokenizados (dos en la comprobación de
    similitud y otros dos dentro de la de contradicción). Con 4000 parejas eso
    son 16 000 tokenizados de los que solo hacían falta 400: unos 100 ms de
    arranque tirados a la basura. Aquí se calcula una firma por recuerdo y se
    reutiliza para todas sus parejas.
    """
    texto = normalizar(entrada.text)
    return texto, _temas(entrada.text), bool(_RE_MISMA_FORMULA.search(texto))


def _contradiccion(a: MemoryEntry, b: MemoryEntry) -> bool:
    """¿Dos recuerdos del mismo tema se contradicen?

    El patrón que se busca es "el mismo marco, distinto valor": dos frases que
    comparten casi todo y se diferencian en una o dos palabras. "Se llama Nube"
    frente a "se llama Bigotes". Cuanto más se parecen, más probable es que
    estén disputando el mismo dato y no hablando de cosas distintas.
    """
    return _contradiccion_entre(_firma(a), _firma(b))


def _contradiccion_entre(fa, fb) -> bool:
    """Lo mismo que ``_contradiccion``, pero sobre firmas ya calculadas."""
    ta, ca, afirma_a = fa
    tb, cb, afirma_b = fb
    # Una dentro de la otra es redundancia, no contradicción.
    if ta in tb or tb in ta:
        return False
    # Solo tiene sentido si ambas afirman un valor con la misma fórmula.
    if not (afirma_a and afirma_b):
        return False
    distintos = ca ^ cb
    # Una contradicción razonable se diferencia en una o dos palabras. Si se
    # diferencian en diez, son dos recuerdos distintos y no un choque.
    if not (1 <= len(distintos) <= 3):
        return False
    return True


def detectar_contradicciones(grafo: GraphBackend, solo: str = "") -> list[dict]:
    """Marca como ``contested`` los pares que se contradicen y avisa.

    No elige ganador: solo levanta la bandera y deja constancia, porque decidir
    cuál de los dos dice la verdad es asunto del usuario.

    **El coste es lo que obliga al diseño.** La versión anterior comparaba todos
    los pares de todos los nodos: con la memoria real del usuario (1418 nodos,
    4 MB) eso son un millón de comparaciones, y en un turno de voz eso no cabe.
    Ahora solo se comparan recuerdos que **comparten al menos una etiqueta**, que
    es justo la relación que el grafo ya tiene guardada y indexada:

    - con ``solo`` (el id de un recuerdo recién guardado) se compara solo con sus
      vecinos, que son unos pocos: es la vía del turno, y cuesta microsegundos;
    - sin ``solo`` se agrupa por etiqueta y se compara dentro de cada grupo,
      acotado por ``PARES_MAX``: es la vía del arranque, y se puede saltar sin
      perder nada porque los grupos que no se alcancen no se contradicen (no
      comparten tema).
    """
    if not isinstance(grafo, GraphBackend):
        return []
    with grafo._lock:
        vecinos = dict(grafo._adj)
        idx = dict(grafo._idx)
        nodos_por_id = {n["id"]: n for n in grafo._grafo["nodes"]}

    def _recuerdos(ids):
        salida = []
        for ident in ids:
            nodo = nodos_por_id.get(ident)
            if nodo is None or nodo.get("kind") not in ("brief", "permanent"):
                continue
            if nodo.get("contested"):
                # Ya está marcado: volver a compararlo solo gastaría CPU.
                continue
            salida.append(nodo)
        return salida

    pares: list[dict] = []
    vistos: set[tuple[str, str]] = set()
    gastados = 0
    marcados: list[str] = []

    def _comparar(lote, presupuesto: int = 0) -> int:
        """Compara un lote. Devuelve cuántas parejas ha hecho de verdad.

        ``presupuesto`` es un tope DURO de parejas, comprobado en cada
        iteración, no un aviso para después. La primera versión lo comprobaba al
        terminar cada grupo, y eso no acotaba nada: la etiqueta más poblada de la
        memoria real tiene más de 300 recuerdos, o sea 45 000 parejas, y el
        "tope" se activaba después de haberlas hecho todas. Medido: 2,8 s de
        arranque. Con el tope dentro del bucle son 4000 parejas y ~0,2 s.
        """
        nonlocal gastados
        # Una firma por recuerdo, no una por pareja: el mismo recuerdo se compara
        # con decenas de otros y su firma no cambia. Ver ``_firma``.
        firma = {n["id"]: _firma(grafo._a_entry(n)) for n in lote}
        for i, na in enumerate(lote):
            fa = firma[na["id"]]
            for nb in lote[i + 1:]:
                if presupuesto and gastados >= presupuesto:
                    return gastados
                gastados += 1
                clave = (na["id"], nb["id"])
                if clave in vistos:
                    continue
                vistos.add(clave)
                fb = firma[nb["id"]]
                if _similitud(fa[1], fb[1]) < UMBRAL_TEMA:
                    continue
                if not _contradiccion_entre(fa, fb):
                    continue
                # La marca se acumula y se escribe UNA vez al final del barrido:
                # por marca, el coste es reescribir el grafo entero.
                marcados.append(na["id"])
                marcados.append(nb["id"])
                pares.append(
                    {
                        "a": na["id"],
                        "b": nb["id"],
                        "a_texto": grafo._a_entry(na).text,
                        "b_texto": grafo._a_entry(nb).text,
                    }
                )
        return gastados

    if solo and solo in idx:
        # Un salto: los vecinos del nodo son etiquetas, y los recuerdos que
        # comparten tema cuelgan de esas etiquetas. Sin el segundo salto el
        # lote sería solo el propio nodo y no habría nada que comparar.
        alcance = {solo}
        for vecino, _ in vecinos.get(solo, []):
            alcance.add(vecino)
            for segundo, _ in vecinos.get(vecino, []):
                alcance.add(segundo)
        _comparar(_recuerdos(alcance), presupuesto=CHOCQUE_CANDIDATOS * CHOCQUE_CANDIDATOS)
        grafo.marcar_disputas(marcados)
        return pares

    etiquetas = [
        n for n in nodos_por_id.values() if n.get("kind") == "tag"
    ]
    # De más a menos pobladas: si el presupuesto se acaba, se ha mirado lo más
    # denso, que es donde de verdad se concentran las contradicciones.
    etiquetas.sort(key=lambda n: -len(vecinos.get(n["id"], [])))
    for etiqueta in etiquetas:
        grupo = _recuerdos([v for v, _ in vecinos.get(etiqueta["id"], [])])
        if len(grupo) < 2:
            continue
        if _comparar(grupo, presupuesto=PARES_MAX) >= PARES_MAX:
            logger.info(
                "Memoria: barrido de contradicciones cortado en %d comparaciones "
                "(grupos por etiqueta, de más a menos poblados).",
                gastados,
            )
            break
    if marcados:
        grafo.marcar_disputas(marcados)
    if pares:
        logger.info("Memoria: %d contradiccion(es) marcadas para revision.", len(pares))
    return pares


def detectar_chocque(grafo: GraphBackend, texto: str) -> str:
    """¿Lo que el usuario dice ahora contradice lo que ya había?

    Es la vía del turno, y a propósito distinta de ``detectar_contradicciones``:
    aquí el recuerdo nuevo todavía no está en el grafo (``_remember`` corre
    DESPUÉS de enviar el turno), así que se busca a los candidatos por parecido
    y se comparan con ellos. Sale un texto para el prompt, o ``""``.

    Coste: una búsqueda sobre el grafo (unos pocos candidatos) y una comparación
    por candidato. No depende del tamaño del grafo, así que puede ir en el
    camino crítico.
    """
    nuevo = (texto or "").strip()
    if not isinstance(grafo, GraphBackend) or not nuevo:
        return ""
    try:
        candidatos = grafo.buscar(nuevo, limite=CHOCQUE_CANDIDATOS)
    except Exception as exc:  # noqa: BLE001 - la memoria no tumba el turno
        logger.debug("No se pudo buscar para detectar un choque: %s", exc)
        return ""
    if not candidatos:
        return ""
    entrada = MemoryEntry(id="nuevo", text=nuevo, role="user")
    # Firma del texto nuevo UNA vez: se compara con todos los candidatos, y aquí
    # esto va en el camino crítico del turno (ver ``_firma``).
    f_nuevo = _firma(entrada)
    chocan = []
    for otro in candidatos:
        if otro.text.strip() == nuevo:
            continue  # el mismo recuerdo, no es un choque
        f_otro = _firma(otro)
        if _similitud(f_nuevo[1], f_otro[1]) < UMBRAL_TEMA:
            continue
        if not _contradiccion_entre(f_nuevo, f_otro):
            continue
        grafo.marcar_disputa(otro.id, True)
        chocan.append(otro)
    if not chocan:
        return ""
    # Solo se cita el primero: en la nota al modelo, más de un recuerdo en
    # disputa es ruido, y el resto ya quedó marcado para la auditoría.
    otro = chocan[0]
    logger.info(
        "Memoria: lo dicho ahora contradice un recuerdo anterior (nuevo: «%s»).",
        recortar(nuevo, 80),
    )
    return (
        "(AVISO DE MEMORIA: lo que el usuario acaba de decir contradice un "
        f"recuerdo guardado. Antes tenías «{recortar(otro.text, 160)}» y ahora "
        f"has dicho «{recortar(nuevo, 160)}». NO elijas una versión ni las des "
        "por ciertas: si el turno necesita ese dato, pregunta al usuario cuál "
        "vale en una frase breve y sigue a partir de su respuesta.)"
    )





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


# --------------------------------------------------------------- correcciones
# Marcas con las que el usuario rectifica. Sin esto, el resultado "corrected" de
# la reflexión nunca se producía: el motor solo sabía registrar "útil" o "callejón
# sin salida", y una corrección no es un fallo de la respuesta sino una señal
# distinta y más valiosa.
_REGLAS_CORRECCION = (
    r"\bno,?\s+(?:eso\s+)?(?:no|es|era)\b",
    r"\beso\s+no\s+(?:es|era)\b",
    r"\bte\s+equivocas\b",
    r"\b(?:eso\s+)?(?:está|esta)\s+(?:mal|equivocado)\b",
    r"\bincorrect[oa]\b",
    r"\ben realidad\b",
    r"\b(?:corrijo|corrección|corregido)\b",
    r"\bno\s+(?:era|es|son)\s+.{0,40},?\s*(?:es|son)\s+",
    r"\bsuponía\b",
)


def es_correccion(texto: str) -> bool:
    """¿El usuario está rectificando algo que se acaba de decir?

    Deliberadamente conservadora: se exige una marca explícita de rectificación.
    Un detector demasiado listo marcaría como corrección media conversación, y
    las lecciones aprendidas serían basura, que es peor que no tener lecciones.
    """
    t = (texto or "").strip()
    if not t or len(t) > 240:
        return False
    return any(re.search(patron, t, flags=re.IGNORECASE) for patron in _REGLAS_CORRECCION)


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