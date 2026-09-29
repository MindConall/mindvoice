"""Pruebas de la invariante del motor de búsqueda.

Debe cumplirse siempre, para cualquier valor guardado en Ajustes:

    motor elegido  ==  motor consultado  ==  motor de la nota  ==  motor anunciado

Si esta suite pasa, la IA no puede decir que ha buscado en un sitio distinto
del que ha usado. Corre: ``python test_web_engines.py``
"""

import asyncio
import unittest
from unittest import mock

from config import (
    DEFAULT_WEB_ENGINE,
    WEB_ENGINES,
    Settings,
    normalize_web_engine,
)
from live_assistant import LiveAssistant


def make_assistant(provider, **overrides):
    """LiveAssistant con la red cortada: solo se observa a quién consulta."""
    settings = Settings(web_search_provider=provider, **overrides)
    with mock.patch.object(LiveAssistant, "__init__", lambda self, s: None):
        a = LiveAssistant.__new__(LiveAssistant)
    a._settings = settings
    a._provider_geo_blocked_set = set()
    a._provider_warned = set()
    a.on_meta = lambda *_a, **_k: None
    # Estado mínimo de la caché/throttle de búsqueda (sin esto, la caché leída
    # antes de escribirla reventaría con AttributeError).
    a._web_cache_query = ""
    a._web_cache_note = ""
    a._web_cache_ts = 0.0
    a._web_last_search_at = 0.0
    a._web_throttle_warned = False
    a._last_web_ts = 0.0
    a._web_inflight = False
    return a


class TestRegistry(unittest.TestCase):
    def test_only_ddg_and_serper_exist(self):
        self.assertEqual(set(WEB_ENGINES), {"duckduckgo", "serper"})

    def test_ddg_is_first_and_default(self):
        self.assertEqual(DEFAULT_WEB_ENGINE, "duckduckgo")
        self.assertEqual(list(WEB_ENGINES)[0], "duckduckgo")
        self.assertEqual(Settings().web_search_provider, "duckduckgo")

    def test_no_removed_provider_is_referenced(self):
        blob = repr(WEB_ENGINES).lower()
        for gone in ("tavily", "serpapi", "brave", "auto"):
            self.assertNotIn(gone, blob)

    def test_normalize_migrates_dead_values(self):
        for dead in ("auto", "google", "tavily", "serpapi", "brave", "", None, "  "):
            self.assertEqual(normalize_web_engine(dead), "duckduckgo")
        self.assertEqual(normalize_web_engine("SERPER"), "serper")
        self.assertEqual(normalize_web_engine(" duckduckgo "), "duckduckgo")

    def test_only_serper_needs_a_key(self):
        self.assertFalse(WEB_ENGINES["duckduckgo"]["key_field"])
        self.assertEqual(WEB_ENGINES["serper"]["key_env"], "SERPER_API_KEY")
        self.assertFalse(hasattr(Settings(), "tavily_api_key"))
        self.assertFalse(hasattr(Settings(), "serpapi_api_key"))
        self.assertFalse(hasattr(Settings(), "brave_api_key"))


class TestInvariant(unittest.TestCase):
    """Elegido = consultado = nota = anunciado."""

    def _run_search(self, a, query):
        """Ejecuta la búsqueda y devuelve a quién se consultó de verdad."""
        called = []

        async def fake_serper(provider, q):
            called.append("serper")
            return "titulo — https://x.test — resumen"

        async def fake_ddg(q):
            called.append("duckduckgo")
            return "titulo — https://x.test — resumen"

        with mock.patch.object(a, "_search_via_provider", fake_serper), \
             mock.patch.object(a, "_duckduckgo_search", fake_ddg):
            note = asyncio.run(a._web_search(query))
        return called, note

    def test_ddg_never_touches_serper(self):
        a = make_assistant("duckduckgo", serper_api_key="clave-falsa")
        called, note = self._run_search(a, "precio del dolar")
        self.assertEqual(called, ["duckduckgo"])

    def test_serper_is_the_only_one_called_when_selected(self):
        a = make_assistant("serper", serper_api_key="clave-falsa")
        called, _ = self._run_search(a, "precio del dolar")
        self.assertEqual(called, ["serper"])

    def test_serper_without_key_does_not_fall_back_silently(self):
        a = make_assistant("serper", serper_api_key=None)
        called, note = self._run_search(a, "precio del dolar")
        self.assertEqual(called, [], "no debe buscar en otro motor sin avisar")
        self.assertEqual(note, "")

    def test_dead_value_in_config_uses_ddg(self):
        a = make_assistant("tavily", serper_api_key="clave-falsa")
        called, _ = self._run_search(a, "precio del dolar")
        self.assertEqual(called, ["duckduckgo"])

    def test_label_and_spoken_follow_the_selection(self):
        for chosen, spoken in (("duckduckgo", "DuckDuckGo"), ("serper", "serper.dev")):
            a = make_assistant(chosen, serper_api_key="k")
            self.assertEqual(a.engine_spoken(), spoken)
            note = a._web_note_for("dolar", "algo")
            self.assertIn(spoken, note, "la nota debe nombrar el motor elegido")

    def test_note_never_names_another_engine(self):
        a = make_assistant("serper", serper_api_key="k")
        note = a._web_note_for("dolar", "algo")
        self.assertIn("serper.dev", note)
        self.assertNotIn("DuckDuckGo", note)

    def test_active_provider_reports_the_choice(self):
        a = make_assistant("duckduckgo")
        self.assertEqual(a.current_engine(), "duckduckgo")
        self.assertEqual(a._active_provider(), "duckduckgo")
        b = make_assistant("serper", serper_api_key="k")
        self.assertEqual(b._active_provider(), "serper")
        c = make_assistant("serper", serper_api_key=None)
        self.assertIsNone(c._active_provider(), "sin clave no se puede usar")

    def test_geo_blocked_engine_is_not_used(self):
        a = make_assistant("serper", serper_api_key="k")
        a._provider_geo_blocked_set.add("serper")
        called, note = self._run_search(a, "dolar")
        self.assertEqual(called, [])
        self.assertEqual(note, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
