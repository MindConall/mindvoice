"""Backend en grafo: los recuerdos son nodos y las relaciones son aristas.

Por qué se lee y escribe ``knowledge_graph.json`` directamente en vez de llamar
a la CLI de Graphify en cada turno:

* ``graphify extract`` sobre prosa necesita un LLM configurado (en esta máquina
  falla por el paquete ``gemini`` ausente) y tarda segundos. En un turno de voz
  eso no cabe.
* ``graphify query`` funciona, pero devuelve texto con.banner y avisos de
  truncado, y no tiene salida JSON: parsearlo en el bucle es frágil.

Lo que sí es estable es el *formato* del ``graph.json``, que es un JSON plano con
``nodes`` y ``links``. Se respeta tal cual para que las herramientas de Graphify
sigan entendiendo el archivo, y se usan sus campos reales: ``norm_label`` para
buscar, ``confidence``/``confidence_score`` para la procedencia.

La recuperación es un BFS ponderado sobre el grafo: primero las semillas que
coinciden con la pregunta, después sus vecinos. Un recuerdo que se usa mucho sube
(``hits``), uno que el usuario corrige se marca ``contested`` y deja de competir.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from datetime import datetime, timedelta, timezone

from .base import (
    BLOCK_BUDGET,
    EXTRACTED,
    INFERRED,
    MemoryBackend,
    MemoryEntry,
    antiguedad_dias,
    ahora_iso,
    autoetiquetas,
    normalizar,
    palabras,
    recortar,
)

logger = logging.getLogger(__name__)

GRAPH_FILENAME = "knowledge_graph.json"
BRIEF_HEADING = "[Breve (conversación reciente)]"
PERMANENT_HEADING = "[A largo plazo (hechos persistentes resumidos)]"

# Pesos de la puntuación. La relevancia manda sobre el resto: es lo que
# determina si el recuerdo entra al prompt.
_PESO_RELEVANCIA = 1.0
_PESO_IMPORTANCIA = 0.55
_PESO_RECENCIA = 0.25
_PESO_REFUERZO = 0.20
# Vida media del refuerzo por acierto, en días. Un recuerdo muy usado deja de
# empujar solo cuando pasa tiempo sin volver a salir.
_MEDIA_REFUERZO_DIAS = 60.0
# Mínimo de recuerdos que se mandan aunque la pregunta no coincida con nada.
# Mandar cero memoria es peor que mandar de más: el modelo perdería el contexto
# del usuario a la fuerza y sin ningún aviso.
MINIMO_SEGURIDAD = 3
# Un recuerdo en disputa se hunde, pero no desaparece: puede que la razón la
# tenga el usuario.
_PENALIZACION_DISPUTA = 0.45


def _id_para(texto: str, secuencia: int) -> str:
    """Id estable y legible, al estilo de Graphify (palabras + secuencia)."""
    nucleo = re.sub(r"[^a-z0-9]+", "_", normalizar(texto)).strip("_")
    nucleo = nucleo[:48] or "recuerdo"
    return f"mem_{nucleo}_{secuencia}"


def _salvar_atomico(ruta: str, datos: dict) -> None:
    """Escribe a un temporal y renombra: un corte de luz no deja el grafo a medias."""
    temporal = ruta + ".tmp"
    with open(temporal, "w", encoding="utf-8") as fh:
        json.dump(datos, fh, ensure_ascii=False, indent=1)
    os.replace(temporal, ruta)


class GraphBackend(MemoryBackend):
    """Memoria como grafo pequeño, escribible y legible sin red."""

    nombre = "grafo"

    def __init__(self, directorio: str) -> None:
        self._dir = directorio
        self._ruta = os.path.join(directorio, GRAPH_FILENAME)
        # El overlay puede leer mientras el bucle reescribe; el candado evita
        # que alguien vea un JSON a medias.
        self._lock = threading.RLock()
        self._grafo: dict = {"nodes": [], "links": []}
        self._idx: dict[str, int] = {}  # id -> posición en nodes
        self._adj: dict[str, list[tuple[str, float]]] = {}  # id -> [(vecino, peso)]
        self._seq = 0
        self._degradado = False
        # Hay cambios en memoria que aún no están en disco (los contadores de
        # refuerzo). Se vuelcan cuando la app dice que terminó el turno.
        self._pendiente = False
        os.makedirs(directorio, exist_ok=True)
        self._cargar()

    # ------------------------------------------------------------------ E/S
    def _cargar(self) -> None:
        """Lee el grafo del disco. Un archivo roto no rompe la app."""
        try:
            with open(self._ruta, encoding="utf-8") as fh:
                bruto = json.load(fh)
            nodos = [n for n in bruto.get("nodes", []) if isinstance(n, dict) and n.get("id")]
            aristas = [
                l
                for l in bruto.get("links", [])
                if isinstance(l, dict) and l.get("source") and l.get("target")
            ]
        except FileNotFoundError:
            self._grafo = self._grafico_vacio()
            return
        except (OSError, ValueError, AttributeError) as exc:
            # No se renombra ni se borra nada: se avisa y se sigue en memoria.
            logger.warning(
                "Grafo de memoria ilegible (%s); se empieza de cero en memoria. "
                "El archivo se conserva en %s.",
                exc,
                self._ruta,
            )
            self._degradado = True
            self._grafo = self._grafico_vacio()
            return
        self._grafo = {
            "directed": False,
            "multigraph": False,
            "graph": {"name": "mindvoice-memory"},
            "nodes": nodos,
            "links": aristas,
            "built_at_commit": bruto.get("built_at_commit"),
        }
        self._reindexar()

    @staticmethod
    def _grafico_vacio() -> dict:
        return {
            "directed": False,
            "multigraph": False,
            "graph": {"name": "mindvoice-memory"},
            "nodes": [],
            "links": [],
            "built_at_commit": None,
        }

    def _reindexar(self) -> None:
        """Recalcula índices y adyacencia desde cero (el grafo es pequeño)."""
        with self._lock:
            self._idx = {}
            self._adj = {}
            for i, nodo in enumerate(self._grafo["nodes"]):
                self._idx[nodo["id"]] = i
                self._adj.setdefault(nodo["id"], [])
            for arista in self._grafo["links"]:
                a, b = arista["source"], arista["target"]
                if a not in self._idx or b not in self._idx:
                    continue
                peso = float(arista.get("weight") or 1.0)
                self._adj[a].append((b, peso))
                self._adj[b].append((a, peso))
            self._seq = sum(1 for n in self._grafo["nodes"] if str(n.get("id", "")).startswith("mem_"))

    def _persistir(self, urgente: bool = True) -> None:
        """Escribe el grafo a disco.

        ``urgente=False`` aplaza la escritura: sirve para los cambios que solo
        ajustan contadores (el refuerzo por acierto), que no son contenido y no
        justifican reescribir todo el JSON.
        """
        if not urgente:
            self._pendiente = True
            return
        try:
            _salvar_atomico(self._ruta, self._grafo)
            self._pendiente = False
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("No se pudo guardar el grafo de memoria: %s", exc)
            self._degradado = True

    def _vaciar_pendientes(self) -> bool:
        """Vuelca al disco lo que se aplazó. Devuelve si había algo pendiente.

        Lo llama la app al cerrar el turno, que es el momento natural: los
        contadores de refuerzo llegan a su sitio sin pagar el coste de disco en
        cada recuperación.
        """
        if not self._pendiente:
            return False
        self._persistir(urgente=True)
        return True

    # ------------------------------------------------------ conversión schema
    @staticmethod
    def _nodo(entry: MemoryEntry) -> dict:
        """Vuelca una entrada al shape de nodo que usa Graphify."""
        return {
            "id": entry.id,
            "label": entry.text[:80],
            "norm_label": entry.norm_label,
            "file_type": "document",
            "_origin": "semantic" if entry.confidence == INFERRED else "ast",
            "source_file": f"memory/{entry.kind}.json",
            "source_location": f"L{entry.hits}",
            # Campos propios de la memoria, que Graphify ignora sin problema.
            "description": entry.text,
            "role": entry.role,
            "kind": entry.kind,
            "importance": entry.importance,
            "confidence": entry.confidence,
            "confidence_score": 1.0 if entry.confidence == EXTRACTED else 0.6,
            "tags": entry.tags,
            "created_at": entry.created_at,
            "hits": entry.hits,
            "contested": entry.contested,
            "last_hit_at": entry.created_at,
        }

    @staticmethod
    def _a_entry(nodo: dict) -> MemoryEntry:
        """Vuelve de nodo a entrada, tolerando nodos que no son de memoria."""
        return MemoryEntry(
            id=nodo["id"],
            text=nodo.get("description") or nodo.get("label") or "",
            role=nodo.get("role") or "user",
            kind=nodo.get("kind") or "brief",
            importance=float(nodo.get("importance", 0.5)),
            confidence=nodo.get("confidence") or EXTRACTED,
            tags=list(nodo.get("tags") or []),
            created_at=nodo.get("created_at") or ahora_iso(),
            hits=int(nodo.get("hits") or 0),
            contested=bool(nodo.get("contested")),
        )

    @staticmethod
    def _arista(a: str, b: str, relation: str, confianza: str) -> dict:
        return {
            "source": a,
            "target": b,
            "relation": relation,
            "_origin": "semantic" if confianza == INFERRED else "ast",
            "confidence": confianza,
            "confidence_score": 1.0 if confianza == EXTRACTED else 0.6,
            "context": relation,
            "source_file": "memory/memory.json",
            "source_location": None,
            "weight": 1.0,
        }

    # -------------------------------------------------------------- escritura
    def remember(self, text: str, role: str = "user", **campos) -> MemoryEntry | None:
        texto = (text or "").strip()
        if not texto:
            return None
        # Sin etiquetas a mano se sacan del propio texto: son las relaciones
        # que convierten una lista de recuerdos en algo recorrible.
        etiquetas = list(campos.get("tags") or []) or autoetiquetas(texto)
        with self._lock:
            self._seq += 1
            entrada = MemoryEntry(
                id=_id_para(texto, self._seq),
                text=texto,
                role=role,
                kind=campos.get("kind", "brief"),
                importance=float(campos.get("importance", 0.5)),
                confidence=campos.get("confidence", EXTRACTED),
                tags=etiquetas,
            )
            self._grafo["nodes"].append(self._nodo(entrada))
            self._idx[entrada.id] = len(self._grafo["nodes"]) - 1
            self._adj.setdefault(entrada.id, [])
            self._enlazar(entrada, etiquetas)
            self._persistir()
            return entrada

    def _enlazar(self, entrada: MemoryEntry, tags) -> None:
        """Crea aristas hacia nodos-tag ya existentes.

        Una etiqueta nueva se convierte en nodo, así dos recuerdos sobre el mismo
        tema quedan unidos y el BFS los puede alcanzar el uno desde el otro.
        """
        for etiqueta in tags:
            etiqueta = (etiqueta or "").strip()
            if not etiqueta:
                continue
            nodo_tag = self._nodo_de_etiqueta(etiqueta)
            confianza = (
                INFERRED if entrada.confidence == INFERRED else EXTRACTED
            )
            self._grafo["links"].append(
                self._arista(entrada.id, nodo_tag, "mentions", confianza)
            )
            self._adj.setdefault(entrada.id, []).append((nodo_tag, 1.0))
            self._adj.setdefault(nodo_tag, []).append((entrada.id, 1.0))

    def _nodo_de_etiqueta(self, etiqueta: str) -> str:
        id_etiqueta = "tag_" + re.sub(r"[^a-z0-9]+", "_", normalizar(etiqueta)).strip("_")
        if id_etiqueta not in self._idx:
            self._seq += 1
            nodo = {
                "id": id_etiqueta,
                "label": etiqueta,
                "norm_label": normalizar(etiqueta),
                "file_type": "concept",
                "_origin": "ast",
                "source_file": "memory/tags.json",
                "source_location": None,
                "description": etiqueta,
                "kind": "tag",
                "importance": 0.3,
                "confidence": EXTRACTED,
                "confidence_score": 1.0,
                "tags": [],
                "created_at": ahora_iso(),
                "hits": 0,
                "contested": False,
                "last_hit_at": ahora_iso(),
            }
            self._grafo["nodes"].append(nodo)
            self._idx[id_etiqueta] = len(self._grafo["nodes"]) - 1
            self._adj.setdefault(id_etiqueta, [])
        return id_etiqueta

    def forget(self, entry_id: str) -> bool:
        """Olvida un recuerdo y sus aristas. Las etiquetas se quedan."""
        with self._lock:
            if entry_id not in self._idx:
                return False
            nodo = self._grafo["nodes"][self._idx[entry_id]]
            if nodo.get("kind") == "tag":
                return False
            self._grafo["nodes"] = [
                n for n in self._grafo["nodes"] if n["id"] != entry_id
            ]
            self._grafo["links"] = [
                l
                for l in self._grafo["links"]
if l["source"] != entry_id and l["target"] != entry_id
        ]
        self._reindexar()
        self._persistir()
        return True

    def marcar_disputa(self, entry_id: str, disputado: bool = True) -> bool:
        """Sube o baja la bandera ``contested`` de un recuerdo."""
        with self._lock:
            if entry_id not in self._idx:
                return False
            self._grafo["nodes"][self._idx[entry_id]]["contested"] = disputado
            self._persistir()
        return True

    def archivar(self, entradas) -> None:
        """El grafo no archiva: nada se queda fuera, se marca como permanente."""
        for e in entradas or []:
            texto = (getattr(e, "text", None) or "").strip()
            if texto:
                self.remember(texto, role="system", kind="permanent", importance=0.7)

    def reset(self) -> bool:
        """Borra los recuerdos conservando las etiquetas (el hilo del tema)."""
        with self._lock:
            self._grafo["nodes"] = [
                n for n in self._grafo["nodes"] if n.get("kind") == "tag"
            ]
            self._grafo["links"] = []
            self._reindexar()
            self._persistir()
        return True

    def export(self) -> str:
        with self._lock:
            return json.dumps(self._grafo, ensure_ascii=False, indent=2)

    # -------------------------------------------------------------- lectura
    def _relevancia(self, nodo: dict, terminos: set[str]) -> float:
        """Puntúa un nodo para una pregunta.

        Es la función que decide qué entra al prompt, así que todo lo que no
        ayude a distinguir recuerdos se deja fuera del cálculo.
        """
        texto = nodo.get("description") or nodo.get("label") or ""
        if not terminos:
            # Sin pregunta (arranque de sesión): manda lo permanente y lo nuevo.
            return 0.35
        comunes = terminos & palabras(texto)
        if not comunes:
            return 0.0
        # Normalizar por la longitud del texto evita que un recuerdo largo
        # gane siempre por tener más palabras.
        peso_texto = len(comunes) / math.sqrt(max(8.0, len(palabras(texto)) * 4.0))
        etiqueta = nodo.get("norm_label") or ""
        if terminos & {etiqueta}:
            peso_texto += 0.5
        return min(1.0, peso_texto)

    def _bonificacion(self, nodo: dict) -> float:
        """Recencia, refuerzo y castigo por disputa."""
        antiguedad = antiguedad_dias(nodo.get("last_hit_at") or nodo.get("created_at") or "")
        recencia = math.exp(-antiguedad / 45.0) if antiguedad != float("inf") else 0.3
        refuerzo = min(1.0, math.log1p(int(nodo.get("hits") or 0)) / math.log(6.0))
        if nodo.get("contested"):
            refuerzo *= _PENALIZACION_DISPUTA
        return recencia, refuerzo

    def retrieve(
        self, query: str = "", limite: int = 8, presupuesto: int = BLOCK_BUDGET
    ) -> list[MemoryEntry]:
        """BFS ponderado: semillas por relevancia y luego un salto de contexto.

        Sin pregunta, se queda solo con lo que tiene senal (permanente, con
        etiquetas o muy reciente), que es el "resumen" de la sesión.
        """
        terminos = palabras(query) if query else set()
        with self._lock:
            # Se puntúa TODO el grafo. Se probó un índice invertido
            # (término -> ids) para recorrer solo los candidatos, pero con la
            # memoria real las palabras de la pregunta casi nunca aparecen en
            # los recuerdos: el índice devolvía 0 candidatos y había que
            # recorrerlo todo igual, sin ganar nada (7,3 ms frente a 6,6 ms).
            # Se quitó: 40 líneas de invalidación a cambio de nada.
            candidatos = [
                n
                for n in self._grafo["nodes"]
                if n.get("kind") in ("brief", "permanent") and not n.get("tag_only")
            ]
            if not candidatos:
                return []

            puntuaciones: dict[str, float] = {}
            for nodo in candidatos:
                rel = self._relevancia(nodo, terminos)
                if rel <= 0.0 and terminos:
                    continue
                recencia, refuerzo = self._bonificacion(nodo)
                puntuaciones[nodo["id"]] = (
                    _PESO_RELEVANCIA * rel
                    + _PESO_IMPORTANCIA * float(nodo.get("importance", 0.5))
                    + _PESO_RECENCIA * recencia
                    + _PESO_REFUERZO * refuerzo
                )

            # Un salto de contexto: lo que cuelga de una semilla pondera menos
            # pero entra, para no perder el hilo de un tema.
            for semilla, base in list(puntuaciones.items()):
                for vecino, peso in self._adj.get(semilla, []):
                    if vecino in puntuaciones:
                        continue
                    nodo_v = self._grafo["nodes"][self._idx[vecino]]
                    if nodo_v.get("kind") == "tag":
                        continue
                    puntuaciones[vecino] = base * 0.45 * peso

            ordenados = sorted(
                puntuaciones.items(), key=lambda kv: kv[1], reverse=True
            )[:limite]

            # Suelo de seguridad: si la pregunta no comparte ni una palabra con
            # lo que hay guardado, el grafo no puede quedarse mudo. Mandar la
            # memoria entera es caro, pero mandar CERO es peor: el modelo
            # perdería el contexto del usuario sin avisar. Se rellena con lo
            # más saliente hasta llegar a un mínimo.
            if len(ordenados) < min(MINIMO_SEGURIDAD, limite):
                ya = {ident for ident, _ in ordenados}
                relleno = []
                for nodo in candidatos:
                    if nodo["id"] in ya:
                        continue
                    recencia, refuerzo = self._bonificacion(nodo)
                    relleno.append(
                        (
                            nodo["id"],
                            _PESO_IMPORTANCIA * float(nodo.get("importance", 0.5))
                            + _PESO_RECENCIA * recencia
                            + _PESO_REFUERZO * refuerzo,
                        )
                    )
                relleno.sort(key=lambda kv: kv[1], reverse=True)
                for ident, _ in relleno[: MINIMO_SEGURIDAD - len(ordenados)]:
                    ordenados.append((ident, 0.0))

            elegidas = [self._grafo["nodes"][self._idx[ident]] for ident, _ in ordenados]

            # Recompensa por acierto: lo que se usa, la próxima vez pesa más.
            ahora = ahora_iso()
            for nodo in elegidas:
                nodo["hits"] = int(nodo.get("hits") or 0) + 1
                nodo["last_hit_at"] = ahora

        # Ojo: solo son contadores, así que la escritura se APLAZA. Antes esta
        # línea escribía todo el grafo a disco en cada recuperación, que con
        # 400 recuerdos eran ~36 ms: un cuarto de frame por cada pregunta.
        self._persistir(urgente=False)
        # `_seleccionar` ya devuelve entradas; convertirlas otra vez aquí
        # intentaba indexar un MemoryEntry como si fuera un nodo.
        return self._seleccionar(elegidas, query, presupuesto)

    @staticmethod
    def _seleccionar(elegidas, query: str, presupuesto: int) -> list[MemoryEntry]:
        """Recorta al presupuesto sin dejar una entrada a medias."""
        resultado: list[MemoryEntry] = []
        usado = 0
        for nodo in elegidas:
            entrada = GraphBackend._a_entry(nodo)
            texto = recortar(entrada.text)
            coste = len(texto) + 8
            if usado + coste > presupuesto and resultado:
                break
            resultado.append(entrada)
            usado += coste
        return resultado

    def block(self, query: str = "", presupuesto: int = BLOCK_BUDGET) -> str:
        """Mismo formato que el plano, pero selecting lo relevante a la pregunta."""
        entradas = self.retrieve(query, limite=8, presupuesto=presupuesto)
        if not entradas:
            return ""
        secciones: list[str] = []
        permanentes = [e for e in entradas if e.kind == "permanent"]
        breves = [e for e in entradas if e.kind != "permanent"]

        if permanentes:
            lineas = []
            for e in permanentes:
                texto = e.text if len(e.text) <= 600 else e.text[:600] + "…"
                marca = " (discutido)" if e.contested else ""
                lineas.append(f"- {texto}{marca}")
            secciones.append(PERMANENT_HEADING + "\n" + "\n".join(lineas))
        if breves:
            lineas = []
            for e in breves:
                texto = recortar(e.text)
                quien = "usuario" if e.role == "user" else "MindVoice"
                marca = " (discutido)" if e.contested else ""
                lineas.append(f"- {quien}: {texto}{marca}")
            secciones.append(BRIEF_HEADING + "\n" + "\n".join(lineas))
        return "\n\n".join(secciones)

    # ------------------------------------------------------------------ diagnóstico
    def podar(self, max_nodos: int | None = None, max_dias: int | None = None) -> int:
        """Recorta el grafo y devuelve cuántos recuerdos se fueron.

        El grafo solo crece y el fichero entero se reescribe en cada cambio, así
        que en una sesión larga el coste deja de ser despreciable (medido: 4 MB
        y 1418 nodos). Se poda por dos lados, siempre en este orden:

        1. **Antigüedad**: lo más viejo que no sea etiqueta ni permanente.
        2. **Volumen**: si aun así se pasa de ``max_nodos``, cae lo menos
           relevante, no lo nuevo.

        Las etiquetas nunca se tocan: son el hilo del tema, y son lo que hace que
        "lo de Python" siga significando algo cuando se olvidan los mensajes
        concretos.
        """
        desde = None
        if max_dias and max_dias > 0:
            desde = (
                datetime.now(timezone.utc) - timedelta(days=float(max_dias))
            ).isoformat()
        with self._lock:
            antes = len(self._idx)
            protegidos = {
                n["id"]
                for n in self._grafo["nodes"]
                if n.get("kind") in ("tag", "permanent")
            }
            candidatos = [
                n for n in self._grafo["nodes"] if n["id"] not in protegidos
            ]
            # Lo más viejo primero. El campo es ``created_at``, no ``ts``: el nodo no
            # tiene ``ts`` y ordenar por él era ordenar por None, o sea por el
            # orden de lista, que por casualidad dejaba lo nuevo. Un nodo SIN
            # fecha se trata como recientísimo ("9999"): si no, un registro
            # antiguo sin marca se podaría antes que uno nuevo.
            def _antiguedad(n: dict) -> str:
                return str(n.get("created_at") or "9999")

            candidatos.sort(key=_antiguedad)

            if desde is not None:
                mantener = {n["id"] for n in candidatos if _antiguedad(n) >= desde}
                self._borrar_nodos(
                    n["id"] for n in candidatos if n["id"] not in mantener
                )
                candidatos = [n for n in candidatos if n["id"] in mantener]

            if max_nodos and max_nodos > 0:
                vivos = len(self._grafo["nodes"])
                if vivos > max_nodos:
                    # Por aquí se cae: lo menos importante y más viejo primero.
                    candidatos.sort(
                        key=lambda n: (
                            float(n.get("importance") or 0.0),
                            _antiguedad(n),
                        )
                    )
                    self._borrar_nodos(
                        n["id"] for n in candidatos[: vivos - max_nodos]
                    )

            self._reindexar()
            self._persistir()
            return antes - len(self._idx)

    def _borrar_nodos(self, ids) -> None:
        """Quita nodos y sus aristas. El llamante ya tiene el ``_lock``."""
        ids = {i for i in ids}
        if not ids:
            return
        self._grafo["nodes"] = [
            n for n in self._grafo["nodes"] if n["id"] not in ids
        ]
        self._grafo["links"] = [
            l
            for l in self._grafo["links"]
            if l["source"] not in ids and l["target"] not in ids
        ]

    def estadisticas(self) -> dict:
        with self._lock:
            por_tipo: dict[str, int] = {}
            for n in self._grafo["nodes"]:
                por_tipo[str(n.get("kind"))] = por_tipo.get(str(n.get("kind")), 0) + 1
            return {
                "nodos": len(self._grafo["nodes"]),
                "aristas": len(self._grafo["links"]),
                "por_tipo": por_tipo,
                "disputados": sum(1 for n in self._grafo["nodes"] if n.get("contested")),
                "degradado": self._degradado,
                "ruta": self._ruta,
            }