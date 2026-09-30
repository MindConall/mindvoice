"""Un clon limpio debe comportarse como el original: sin degradaciones ocultas.

Estas pruebas no tocan red ni disco. Reproducen el arranque de una instalación
nueva (sin ``.env``, sin ``user_prefs.json``, sin ``web_model_cache.json``) y
comprueban lo que el README promete: que el modelo de búsqueda es el
documentado, que la lista de reserva está ordenada por calidad y no por
disponibilidad de una clave concreta, que el motor de búsqueda se elige según
la clave que hay, y que el arranque dice con qué configuración se arranca.
"""

import importlib
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

import config  # noqa: E402
import live_assistant as la  # noqa: E402
import prefs as prefs_mod  # noqa: E402

# Variables de entorno queouncedidual en una máquina de desarrollo y que un
# clon limpio NO tendría.
_SIN_CLAVES = (
    "GEMINI_API_KEY",
    "MINDVOICE_API_KEY",
    "SERPER_API_KEY",
    "MINDVOICE_WEB_SEARCH_MODEL",
)


def _entorno_limpio(**extra):
    """Entorno sin ninguna clave, con lo que se pase en ``extra``."""
    limpio = {k: v for k, v in os.environ.items() if k not in _SIN_CLAVES}
    limpio.update(extra)
    return mock.patch.dict(os.environ, limpio, clear=True)


class TestClonLimpio(unittest.TestCase):
    maxDiff = None

    # -- modelo de búsqueda ------------------------------------------------
    def test_pin_de_busqueda_documentado(self) -> None:
        """El pin por defecto es el que dice el README y ``.env.example``."""
        self.assertEqual("gemini-3.6-flash", config.WEB_SEARCH_MODEL)
        # Y es exactamente el que aparece documentado fuera del código.
        readme = (RAIZ / "README.md").read_text(encoding="utf-8")
        self.assertIn(config.WEB_SEARCH_MODEL, readme)
        self.assertIn(
            config.WEB_SEARCH_MODEL,
            (RAIZ / ".env.example").read_text(encoding="utf-8"),
        )

    def test_lista_de_reserva_ordenada_por_calidad(self) -> None:
        """El pin va PRIMERO; los ``-lite`` (baratos) van al final."""
        lista = list(la._WEB_MODEL_FALLBACKS)
        self.assertEqual(config.WEB_SEARCH_MODEL, lista[0])
        baratos = [m for m in lista if m.endswith("-lite")]
        self.assertTrue(baratos, "debe quedar alguna red de seguridad barata")
        for barato in baratos:
            self.assertGreater(
                lista.index(barato),
                lista.index(config.WEB_SEARCH_MODEL),
                f"el modelo barato «{barato}» va por delante del pin",
            )
        # El pin no se repite por error al construir la tupla.
        self.assertEqual(len(lista), len(set(lista)))

    def test_los_baratos_no_definen_el_default_de_prefs(self) -> None:
        """``DEFAULTS`` no puede clavar un motor: ``None`` = decide el defecto."""
        self.assertIsNone(prefs_mod.DEFAULTS["web_search_provider"])

    def test_el_lite_no_es_el_primero_candidato(self) -> None:
        """Regresión del bug: el -lite se colaba primero por los 429 de una key."""
        self.assertNotEqual("gemini-3.1-flash-lite", la._WEB_MODEL_FALLBACKS[0])

    # -- motor de búsqueda -------------------------------------------------
    def test_motor_por_defecto_sin_clave_es_duckduckgo(self) -> None:
        with _entorno_limpio():
            self.assertEqual("duckduckgo", config.default_web_engine())
            self.assertEqual("duckduckgo", config.normalize_web_engine(None))
            self.assertEqual("duckduckgo", config.normalize_web_engine(""))
            self.assertEqual("duckduckgo", config.normalize_web_engine("tavily"))

    def test_motor_por_defecto_con_clave_es_serper(self) -> None:
        with _entorno_limpio(SERPER_API_KEY="clave-de-prueba"):
            self.assertEqual("serper", config.default_web_engine())
            self.assertEqual("serper", config.normalize_web_engine(None))
            # Un valor desconocido con clave también cae en serper, no en DDG.
            self.assertEqual("serper", config.normalize_web_engine("serpapi"))

    def test_settings_toma_el_motor_por_defecto_real(self) -> None:
        with _entorno_limpio(SERPER_API_KEY="clave-de-prueba"):
            self.assertEqual("serper", config.Settings().web_search_provider)
        with _entorno_limpio():
            self.assertEqual("duckduckgo", config.Settings().web_search_provider)

    def test_prefs_sin_motor_no_lo_fijan_en_ddg(self) -> None:
        """``user_prefs.json`` sin motor elegido no debe clavar DuckDuckGo."""
        with _entorno_limpio(SERPER_API_KEY="clave-de-prueba"):
            ajustes = config.Settings()
            prefs_mod.apply_prefs(ajustes, {"web_search_provider": None})
            self.assertEqual("serper", ajustes.web_search_provider)
        with _entorno_limpio():
            ajustes = config.Settings()
            # OJO: un dict VACÍO haría `prefs or load_prefs()` y leería el
            # user_prefs.json real de esta máquina. Un prefs real sin motor
            # elegido siempre trae otras claves.
            prefs_mod.apply_prefs(ajustes, {"web_search_enabled": True})
            self.assertEqual("duckduckgo", ajustes.web_search_provider)

    def test_motor_eligido_gana_al_defecto(self) -> None:
        """Si el usuario eligió uno, la clave no lo cambia por la espalda."""
        with _entorno_limpio(SERPER_API_KEY="clave-de-prueba"):
            ajustes = config.Settings()
            prefs_mod.apply_prefs(ajustes, {"web_search_provider": "duckduckgo"})
            self.assertEqual("duckduckgo", ajustes.web_search_provider)

    def test_solo_existen_los_motores_documentados(self) -> None:
        """El README no puede prometer motores que el registro no tiene."""
        self.assertEqual({"duckduckgo", "serper"}, set(config.WEB_ENGINES))
        readme = (RAIZ / "README.md").read_text(encoding="utf-8")
        for fantasma in ("searxng", "wikipedia", "brave", "tavily", "serpapi"):
            self.assertNotIn(
                fantasma,
                readme.lower(),
                f"el README sigue prometiendo el motor retirado «{fantasma}»",
            )

    # -- diagnóstico de arranque -------------------------------------------
    def test_el_arranque_declara_su_configuracion(self) -> None:
        """El bloque (Arranque) dice modelo, API, clave y motor."""
        with _entorno_limpio():
            asis = la.LiveAssistant.__new__(la.LiveAssistant)
            asis._settings = config.Settings()
            avisos = []
            asis.on_meta = avisos.append
            asis._safe_call = lambda fn, texto: fn(texto)
            asis._startup_diagnostics()
        union = "\n".join(avisos)
        self.assertIn("(Arranque) Voz: gemini-3.1-flash-live-preview", union)
        self.assertIn("API v1alpha", union)
        self.assertIn("Clave de API: AUSENTE", union)
        self.assertIn("motor: DuckDuckGo", union)
        self.assertIn("Modelo de búsqueda: gemini-3.6-flash", union)

    def test_el_arranque_avisa_si_el_motor_es_ddg(self) -> None:
        """Sin clave hay que avisar de que DuckDuckGo suele dar peores resultados."""
        with _entorno_limpio():
            asis = la.LiveAssistant.__new__(la.LiveAssistant)
            asis._settings = config.Settings()
            avisos = []
            asis.on_meta = avisos.append
            asis._safe_call = lambda fn, texto: fn(texto)
            asis._startup_diagnostics()
        self.assertIn("SERPER_API_KEY", "\n".join(avisos))

    def test_el_arranque_no_imprime_la_clave(self) -> None:
        """Ni en la prueba ni en producción: la clave no se imprime nunca."""
        secreto = "AIza-no-me-publiques-esta-clave"
        with _entorno_limpio(GEMINI_API_KEY=secreto):
            asis = la.LiveAssistant.__new__(la.LiveAssistant)
            asis._settings = config.Settings()
            asis._settings.api_key = secreto
            avisos = []
            asis.on_meta = avisos.append
            asis._safe_call = lambda fn, texto: fn(texto)
            asis._startup_diagnostics()
        union = "\n".join(avisos)
        self.assertNotIn(secreto, union)
        self.assertIn("Clave de API: presente", union)

    # -- persistencia entre reinicios ---------------------------------------
    @staticmethod
    def _app_dir() -> str:
        """Carpeta del código, en minúsculas para comparar sin sorpresas."""
        return str(importlib.import_module("rutas").app_dir()).lower()

    def test_el_estado_mutable_no_vive_junto_al_codigo(self) -> None:
        """Nada de lo que la app escribe puede estar en la carpeta de la app.

        Instalada en ``C:\\Program Files`` esa carpeta no se puede escribir: los
        ``open(..., "w")`` darían PermissionError y, como todo va envuelto en
        ``try/except``, el fallo se perdería en silencio. Lo que se guarda tiene
        que ir al directorio de datos del usuario.
        """
        app_dir = self._app_dir()
        persistentes = [prefs_mod.PREFS_FILE, la._WEB_MODEL_CACHE_FILE]
        dentro_de_app = [
            str(ruta) for ruta in persistentes
            if str(ruta).lower().startswith(app_dir)
        ]
        self.assertEqual(
            [],
            dentro_de_app,
            "estado mutable escrito junto al código: no persistirá en un install",
        )

    def test_la_cache_web_sobrevive_a_un_data_dir_nuevo(self) -> None:
        """Guardar y releer la caché crea el directorio si aún no existe."""
        import tempfile

        with tempfile.TemporaryDirectory() as temporal:
            destino = Path(temporal) / "MindVoice" / "web_model_cache.json"
            with mock.patch.object(la, "_WEB_MODEL_CACHE_FILE", destino), \
                 mock.patch.object(
                     la, "ensure_data_dir",
                     lambda: destino.parent.mkdir(parents=True, exist_ok=True) or destino.parent,
                 ):
                la._save_cached_web_model("gemini-3.6-flash")
                self.assertTrue(destino.exists(), "no se pudo guardar la caché")
                self.assertEqual("gemini-3.6-flash", la._load_cached_web_model())

    def test_un_json_corrupto_no_impide_arrancar(self) -> None:
        """Preferencias inservibles: se avisa y se sigue con los valores por defecto."""
        import tempfile

        with tempfile.TemporaryDirectory() as temporal:
            roto = Path(temporal) / "user_prefs.json"
            roto.write_text("{esto no es json,,,[", encoding="utf-8")
            with mock.patch.object(prefs_mod, "PREFS_FILE", roto):
                prefs = prefs_mod.load_prefs()
        self.assertEqual(prefs_mod.DEFAULTS["voice"], prefs["voice"])
        self.assertIn("web_search_provider", prefs)

    # -- higiene del clon ---------------------------------------------------
    def test_no_hay_rutas_absolutas_a_una_maquina(self) -> None:
        """Nada de rutas de la máquina del autor en el código que se reparte."""
        fugas = re.compile(r"[A-Za-z]:\\Users\\|/home/[a-z]+/|/Users/[a-z]+/")
        fiscalizados = [
            p for p in RAIZ.glob("*.py")
            if not p.name.startswith("test_")
        ] + [RAIZ / "README.md", RAIZ / ".env.example"]
        autos = []
        for archivo in fiscalizados:
            texto = archivo.read_text(encoding="utf-8", errors="replace")
            for linea in texto.splitlines():
                # Los .env.example son documentación: ahí sí se pueden citar
                # rutas de ejemplo, pero no deben traer la del autor.
                if archivo.name == ".env.example" and linea.lstrip().startswith("#"):
                    continue
                if "graphify" in linea.lower():
                    continue
                if fugas.search(linea):
                    autos.append(f"{archivo.name}: {linea.strip()[:100]}")
        self.assertEqual([], autos, "rutas absolutas filtradas al clon")


if __name__ == "__main__":
    unittest.main()