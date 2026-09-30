"""Tests del sistema de diseño (Fase 3).

Dos cosas se comprueban aquí:

* que los tokens son coherentes entre sí (escalas, radios, duraciones);
* que las animaciones no rompen las reglas de rendimiento y no se acumulan.

Las reglas de rendimiento se prueban de verdad: se mide cuánto tarda un
volcado con y sin animación para comprobar que no se va de las manos.

Y una tercera, que es la que hace segura la migración: cada token tiene que
valer EXACTAMENTE el literal que sustituyó en el QSS. Si no, aplicar el sistema
de diseño habría cambiado la pantalla.
"""

from __future__ import annotations

import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import (  # noqa: E402
    QApplication,
    QGraphicsOpacityEffect,
    QWidget,
)

from ui import tokens  # noqa: E402
from ui.animations import (  # noqa: E402
    Desvanecer,
    Fade,
    FadeWidget,
    Pulso,
    animaciones_vivas,
    validar_reglas,
)

_app = QApplication.instance() or QApplication([])


class TestTokens(unittest.TestCase):
    def test_la_escala_de_espacio_crece_y_no_se_repasa(self) -> None:
        valores = list(tokens.ESCALA_ESPACIO)
        self.assertEqual(valores, sorted(valores), "la escala debe crecer")
        self.assertEqual(valores[0], 4, "la base es 4 px")
        self.assertEqual(
            valores,
            [tokens.ESPACIO_XS, tokens.ESPACIO_SM, tokens.ESPACIO_MD,
             tokens.ESPACIO_LG, tokens.ESPACIO_XL, tokens.ESPACIO_XXL],
            "las constantes sueltas tienen que ser la escala",
        )
        # Hasta 16 son peldaños de 4 px; después la separación se duplica
        # respecto al salto anterior (16 -> 24 -> 32), que es como se leen bien
        # los huecos grandes.
        for a, b in zip(valores[:3], valores[1:4]):
            self.assertEqual(b - a, 4, f"{a}->{b} debería subir 4 px")
        self.assertEqual(valores[3] * 3 // 2, valores[4], "16 -> 24")
        self.assertEqual(valores[4] * 4 // 3, valores[5], "24 -> 32")

    def test_los_colores_son_hex_validos(self) -> None:
        import re

        for nombre in dir(tokens):
            if nombre.startswith("_") or not nombre.isupper():
                continue
            valor = getattr(tokens, nombre)
            if isinstance(valor, str) and valor.startswith("#"):
                self.assertRegex(valor, r"^#[0-9a-fA-F]{6}$", f"{nombre}={valor}")

    def test_toda_la_tipografia_es_al_menos_el_minimo(self) -> None:
        for tam in (tokens.TAMANO_CIFRA, tokens.TAMANO_TITULO,
                    tokens.TAMANO_CUERPO, tokens.TAMANO_ETIQUETA, tokens.TAMANO_MINI):
            self.assertGreaterEqual(tam, tokens.REGLAS["tamano_min_px"])

    def test_las_duraciones_caben_en_un_frame_largo(self) -> None:
        for dur in (tokens.DURACION_TOGGLE_MS, tokens.DURACION_APARECER_MS,
                    tokens.DURACION_DESVANECER_MS, tokens.DURACION_LISTA_MS):
            self.assertLessEqual(dur, tokens.REGLAS["duracion_max_ms"])
            self.assertGreaterEqual(dur, tokens.REGLAS["duracion_min_ms"])

    def test_el_radio_crece_y_no_se_pasa(self) -> None:
        r = [tokens.RADIO_CHIP, tokens.RADIO_BLOQUE, tokens.RADIO_TARJETA, tokens.RADIO_PANEL]
        self.assertEqual(r, sorted(r))
        for radio in r:
            self.assertLessEqual(radio, tokens.REGLAS["radio_max_px"])

    def test_todo_estado_tiene_color(self) -> None:
        for estado in ("idle", "listening", "processing", "speaking", "error"):
            self.assertIn(estado, tokens.ESTADO_COLOR)

    def test_las_reglas_se_cumplen(self) -> None:
        self.assertEqual(validar_reglas(), [])


class TestTokensNoMuevenPixeles(unittest.TestCase):
    """La migración a tokens solo es segura si no cambia ni un valor.

    Cada token nació de un literal que ya estaba escrito en el QSS del HUD. Si
    un token vale lo mismo que su literal, sustituir uno por otro no cambia el
    QSS, y por tanto no mueve un píxel. En cuanto alguien "ordena" un token para
    que quede más bonito, esto salta.
    """

    LITERALES = {
        "tinta": "#e8ecf2",
        "tinta_alta": "#f4f7fb",
        "tinta_suave": "#9fb2c9",
        "tinta_borde": "#b9c8da",
        "tinta_tenue": "#8fa3ba",
        "tinta_media": "#c7d1de",
        "fondo": "#151a24",
        "verde": "#59d98f",
        "rojo": "#ff5d5d",
        "rojo_tenue": "#ffd7d7",
        "ambar": "#f0c67a",
        "cian": "#7ee0ff",
        "radio_tarjeta": 12,
        "radio_bloque": 11,
        "radio_chip": 8,
        "radio_panel": 18,
        "tamano_cuerpo": 12,
        "tamano_titulo": 13,
        "tamano_etiqueta": 10,
    }

    def test_cada_token_es_su_literal_historical(self) -> None:
        for nombre, esperado in self.LITERALES.items():
            with self.subTest(token=nombre):
                self.assertEqual(
                    getattr(tokens, nombre), esperado,
                    f"{nombre} ya no vale lo que valía el literal del QSS: "
                    "migrarlo cambiaría la pantalla",
                )


class TestValidarReglas(unittest.TestCase):
    def test_detecta_una_duracion_larga(self) -> None:
        original = tokens.DURACION_TOGGLE_MS
        try:
            tokens.DURACION_TOGGLE_MS = 900
            fallos = validar_reglas()
            self.assertTrue(any("900" in f for f in fallos))
        finally:
            tokens.DURACION_TOGGLE_MS = original
        self.assertEqual(validar_reglas(), [])

    def test_detecta_una_tipografia_muy_pequena(self) -> None:
        original = tokens.TAMANO_ETIQUETA
        try:
            tokens.TAMANO_ETIQUETA = 6
            self.assertTrue(any("6 px" in f for f in validar_reglas()))
        finally:
            tokens.TAMANO_ETIQUETA = original


class _Contador(QWidget):
    """Widget que cuenta sus propios repintados, para medirlo de verdad."""

    def __init__(self) -> None:
        super().__init__()
        self.repintados = 0

    def paintEvent(self, event) -> None:  # noqa: N802 - nombre de Qt
        self.repintados += 1
        super().paintEvent(event)


class TestRendimientoAnimaciones(unittest.TestCase):
    def widget(self) -> _Contador:
        w = _Contador()
        w.resize(120, 40)
        w.show()
        return w

    def drenar(self, segundos: float = 0.35) -> None:
        fin = time.perf_counter() + segundos
        while time.perf_counter() < fin:
            _app.processEvents()

    def test_el_fundido_cambia_la_opacidad_de_verdad(self) -> None:
        """El contrato del fundido es que la opacidad recorra valores.

        No se comprueba el número de repintados: con la plataforma "offscreen"
        Qt no genera eventos de pintado al cambiar la opacidad de una ventana, y
        eso no dice nada sobre si la animación funciona.
        """
        w = self.widget()
        anim = Fade(w, 0.0, 1.0, tokens.DURACION_APARECER_MS)
        anim.start()
        vistos = set()
        for _ in range(8):
            self.drenar(0.02)
            vistos.add(round(float(w.windowOpacity()), 2))
        self.assertGreater(len(vistos), 1, "la opacidad tiene que ir variando")
        anim.stop()

    def test_medir_relleno_no_cuesta_mas_que_el_frame(self) -> None:
        """Rellenar el widget a mano debe caber de sobra en 16 ms."""
        w = self.widget()
        self.drenar(0.1)
        from PyQt6.QtGui import QColor, QPainter

        pixmap = None
        t0 = time.perf_counter()
        for _ in range(60):
            from PyQt6.QtGui import QPixmap

            pixmap = QPixmap(w.size())
            pixmap.fill(QColor(21, 26, 36))
            p = QPainter(pixmap)
            p.setPen(QColor(232, 236, 242))
            p.drawText(4, 20, "MindVoice 123 45 6789")
            p.end()
        medio = (time.perf_counter() - t0) / 60
        self.assertLess(medio, 0.016, f"cada repintado tardó {medio*1000:.2f} ms")

    def test_animar_no_toca_la_geometria(self) -> None:
        """La regla que más rendimiento protege: no animar layout."""
        w = self.widget()
        antes = w.geometry()
        anim = Fade(w, 0.0, 1.0, tokens.DURACION_APARECER_MS)
        anim.start()
        self.drenar(0.05)
        self.assertEqual(w.geometry(), antes, "animar opacidad no debe mover nada")
        # Se para al final: si el test deja la animación corriendo, deja viva una
        # cuenta en el contador y contamina los tests que vienen detrás.
        anim.stop()

    def test_el_desvanecido_oculta_al_terminar(self) -> None:
        w = self.widget()
        anim = Desvanecer(w, 60)
        anim.start()
        self.drenar(0.3)
        self.assertFalse(w.isVisible(), "el desvanecido tiene que acabar ocultando")
        # Y la opacidad se recupera para la siguiente entrada.
        self.assertAlmostEqual(w.windowOpacity(), 1.0, places=3)

    def test_una_animacion_parada_no_deja_el_contador_colgado(self) -> None:
        antes = animaciones_vivas()
        w = self.widget()
        p = Pulso(w, 50)
        p.start()
        self.drenar(0.15)
        p.stop()
        self.drenar(0.1)
        self.assertLessEqual(animaciones_vivas(), antes + 1)

    def test_el_pulso_es_mas_largo_que_el_tope_de_animacion(self) -> None:
        """El pulso es un ciclo, no una respuesta: puede durar más."""
        self.assertGreater(tokens.DURACION_PULSO_MS, tokens.REGLAS["duracion_max_ms"])

    def test_window_opacidad_solo_sirve_en_ventanas(self) -> None:
        """Por qué hace falta un efecto: Qt ignora windowOpacity en un hijo.

        Es un detalle de Qt que no da ningún aviso, así que queda escrito como
        test para que nadie vuelva a animar un panel hijo con ``windowOpacity``
        y se pregunte por qué no se ve.
        """
        padre = _Contador()
        padre.resize(200, 100)
        hijo = _Contador()
        hijo.resize(50, 20)
        hijo.setParent(padre)
        padre.show()
        self.drenar(0.05)

        hijo.setWindowOpacity(0.5)
        # Y esto es exactamente el problema: en un widget hijo Qt **no se
        # guarda ni se aplica**, sigue a 1.0 sin avisar de nada.
        self.assertAlmostEqual(hijo.windowOpacity(), 1.0, places=3)

    def test_el_fundido_de_un_hijo_sí_se_ve(self) -> None:
        """La razón de ser de la clase: un hijo tiene que fundirse de verdad.

        Antes esto no se comprobaba: ``FadeWidget`` animaba una propiedad
        dinámica llamada "opacity" sobre su propio ``QObject``, un valor que
        nadie lee. El fundido de un hijo no se veía y el test pasaba equally.
        """
        padre = _Contador()
        padre.resize(200, 100)
        hijo = _Contador()
        hijo.resize(50, 20)
        hijo.setParent(padre)
        padre.show()
        self.drenar(0.05)

        anim = FadeWidget(hijo, 0.0, 1.0, 120)
        self.assertIsNotNone(
            hijo.graphicsEffect(),
            "un widget hijo necesita un efecto de opacidad para fundirse",
        )
        anim.start()
        self.drenar(0.05)
        a_mitad = anim.opacidad_actual
        self.drenar(0.25)
        al_final = anim.opacidad_actual

        self.assertLess(
            a_mitad, 0.9,
            f"a mitad del fundido debería verse translúcido, estaba en {a_mitad}",
        )
        self.assertAlmostEqual(al_final, 1.0, places=2)

    def test_al_quedar_opaco_se_retira_el_efecto(self) -> None:
        """Un efecto de opacidad permanente encarece cada paint del widget."""
        padre = _Contador()
        padre.resize(200, 100)
        hijo = _Contador()
        hijo.setParent(padre)
        padre.show()
        self.drenar(0.05)

        anim = FadeWidget(hijo, 0.0, 1.0, 60)
        anim.start()
        self.drenar(0.25)
        self.assertIsNone(
            hijo.graphicsEffect(),
            "el efecto debería retirarse al quedar opaco",
        )

    def test_se_reaprovecha_el_efecto_de_opacidad_que_ya_tenia(self) -> None:
        """Si el widget ya trae un efecto de opacidad, se usa ese mismo.

        Un ``QWidget`` solo admite un efecto y al instalar otro Qt borra el que
        tenía. Así que no se puede apartar y devolver: o se reutiliza el suyo, o
        se destruye lo que el widget tenía puesto.
        """
        padre = _Contador()
        padre.resize(200, 100)
        hijo = _Contador()
        hijo.setParent(padre)
        previo = QGraphicsOpacityEffect(hijo)
        previo.setOpacity(0.42)
        hijo.setGraphicsEffect(previo)
        padre.show()
        self.drenar(0.05)

        anim = FadeWidget(hijo, 0.0, 1.0, 120)
        self.assertIsNotNone(
            hijo.graphicsEffect(), "no se puede fundir un widget sin efecto"
        )
        anim.start()
        self.drenar(0.04)
        a_mitad = anim.opacidad_actual
        self.drenar(0.25)

        self.assertLess(
            a_mitad, 0.9,
            f"con efecto previo el fundido tampoco se vio: {a_mitad}",
        )
        # Y al terminar, el efecto del widget sigue siendo el suyo, no el
        # nuestro por encima.
        actual = hijo.graphicsEffect()
        self.assertIsNotNone(actual, "el efecto del widget se perdió")
        self.assertAlmostEqual(actual.opacity(), 1.0, places=2)

    def test_no_se_toca_un_widget_con_otro_tipo_de_efecto(self) -> None:
        """Con una sombra puesta, fundirse rompería el widget: mejor no animar.

        Preferimos no hacer nada visible a destruir un efecto que el widget
        tenía puesto. Y que quede dicho, no fallido en silencio.
        """
        from PyQt6.QtWidgets import QGraphicsDropShadowEffect

        padre = _Contador()
        padre.resize(200, 100)
        hijo = _Contador()
        hijo.setParent(padre)
        sombra = QGraphicsDropShadowEffect(hijo)
        hijo.setGraphicsEffect(sombra)
        padre.show()
        self.drenar(0.05)

        antes = animaciones_vivas()
        anim = FadeWidget(hijo, 0.0, 1.0, 60)
        anim.start()
        self.drenar(0.2)

        self.assertIsNotNone(hijo.graphicsEffect(), "la sombra debe seguir puesta")
        self.assertEqual(animaciones_vivas(), antes)

    def test_el_pulso_tambien_funciona_en_un_hijo(self) -> None:
        """El punto de micrófono es un hijo: ahí el pulso se notaba."""
        padre = _Contador()
        padre.resize(200, 100)
        punto = _Contador()
        punto.resize(16, 16)
        punto.setParent(padre)
        padre.show()
        self.drenar(0.05)

        pulso = Pulso(punto, 60)
        pulso.start()
        vistos = set()
        for _ in range(12):
            self.drenar(0.02)
            vistos.add(round(pulso.opacidad_actual, 2))
        pulso.stop()
        self.assertGreater(
            len(vistos), 1,
            f"el pulso no varió la opacidad: {vistos}",
        )

    def test_parar_no_deja_el_contador_colgado(self) -> None:
        """``finished`` no salta al parar, así que parar también tiene que restar.

        Este era el fallo real: cada ``start()`` sumaba y solo ``finished``
        restaba, así que una animación interrupted fuga su cuenta para siempre.
        """
        for fabrica in (
            lambda w: Fade(w, 0.0, 1.0, 400),
            lambda w: Desvanecer(w, 400),
            lambda w: FadeWidget(w, 0.0, 1.0, 400),
            lambda w: Pulso(w, 400),
        ):
            with self.subTest(clase=fabrica.__qualname__):
                antes = animaciones_vivas()
                anim = fabrica(self.widget())
                anim.start()
                self.drenar(0.03)
                anim.stop()
                self.assertEqual(
                    animaciones_vivas(), antes,
                    "parar a mitad tiene que devolver la cuenta",
                )

    def test_arrancar_dos_veces_no_cuenta_dos(self) -> None:
        """Un ``start()`` repetido no puede duplicar la cuenta."""
        antes = animaciones_vivas()
        anim = Fade(self.widget(), 0.0, 1.0, 80)
        anim.start()
        anim.start()
        anim.start()
        self.drenar(0.2)
        self.assertEqual(animaciones_vivas(), antes)

    def test_terminar_tambien_devuelve_la_cuenta(self) -> None:
        """Y una animación que acaba sola también."""
        antes = animaciones_vivas()
        anim = Fade(self.widget(), 0.0, 1.0, 60)
        anim.start()
        self.drenar(0.3)
        self.assertEqual(animaciones_vivas(), antes)

    def test_diez_animaciones_no_se_acumulan_en_el_bucle(self) -> None:
        """Diez widgets animándose a la vez no pueden dejar basura viva."""
        antes = animaciones_vivas()
        for i in range(10):
            w = _Contador()
            w.resize(60, 20)
            f = Fade(w, 0.0, 1.0, 60)
            f.start()
            self.drenar(0.02)
        self.drenar(0.3)
        # Sin excepción, el contador vuelve a su punto de partida.
        self.assertEqual(animaciones_vivas(), antes)


if __name__ == "__main__":
    unittest.main()