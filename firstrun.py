"""Asistente de primera ejecución: pide la clave de la API de Gemini.

Sin esto, instalar MindVoice obligaba a abrir una terminal y definir
``GEMINI_API_KEY`` en las variables de entorno del sistema *antes* de arrancar
nada. Para quien no es desarrollador eso es un muro: no ven la variable, no
saben que existe y el mensaje de error ("Falta GEMINI_API_KEY") no dice qué
hacer a continuación.

Aquí se sustituye ese muro por un diálogo: se pega la clave, se valida y se
guarda cifrada (ver ``credenciales.py``). A partir de ahí la app funciona con
doble clic.

El diálogo usa PyQt6 y no ``tkinter`` a propósito: el runtime embebido de la
instalación no incluye ``tkinter`` (no viene en el paquete oficial de Python
embebido), mientras que PyQt6 ya es dependencia obligatoria por el overlay.

Robustez: si PyQt6 no está disponible, si el usuario cierra la ventana o si no
hay consola donde preguntar, se degrada a una pregunta por terminal en vez de
fallar. Ninguna de esas rutas debe tumbar la app: si al final no hay clave,
queda que lo diga el punto de entrada, que sí sabe reaccionar (overlay/main).
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

import credenciales

logger = logging.getLogger(__name__)

URL_CONSOLA = "https://aistudio.google.com/apikey"

# Longitud mínima que se acepta sin quejarse. Deliberadamente conservadora: se
# avisa de las claves sospechosamente cortas, pero NO se exige un formato
# concreto (ni prefijo "AIza" ni longitud exacta). Los formatos de credencial
# cambian según el producto y el canal, y un diálogo que rechaza la clave
# válida porque su formato cambió deja al usuario sin salida. Si la clave está
# mal, el propio API responde; para eso no hace falta adivinar aquí.
LONGITUD_MINIMA = 24


def _clave_disponible() -> bool:
    return bool(credenciales.get_api_key())


def _validar(clave: str) -> Optional[str]:
    """Devuelve un mensaje de error, o ``None`` si la clave parece utilizable.

    Solo bloquea lo que es claramente inservible. Cualquier otra duda se deja
    pasar: un falso positivo en un asistente de instalación es mucho peor que
    dejar pasar una clave rara que luego rechace el servidor.
    """
    clave = clave.strip()
    if not clave:
        return "Pega tu clave de la API de Gemini para continuar."
    if len(clave) < LONGITUD_MINIMA:
        return (
            f"Ojo: eso solo tiene {len(clave)} caracteres y parece demasiado "
            "corto para una clave de API. Revisa que la hayas copiado entera."
        )
    return None


def _preguntar_por_consola() -> str:
    """Pregunta por terminal. Último recurso, pero mejor que un error seco."""
    print("=" * 66)
    print("  MindVoice necesita una clave de la API de Gemini (es gratis)")
    print(f"  Consíguela en: {URL_CONSOLA}")
    print("=" * 66)
    try:
        return input("Pega aquí la clave (Enter para salir): ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def _preguntar_con_qt() -> Optional[str]:
    """Diálogo de PyQt6. Devuelve la clave o ``None`` si se cancela/no hay Qt."""
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import (
            QApplication,
            QDialog,
            QDialogButtonBox,
            QLabel,
            QLineEdit,
            QVBoxLayout,
        )
    except ImportError:
        logger.info("PyQt6 no disponible: se pregunta por consola.")
        return _preguntar_por_consola()

    owns_app = QApplication.instance() is None
    app = QApplication.instance() or QApplication(sys.argv[:1])

    dialog = QDialog()
    dialog.setWindowTitle("MindVoice · primera configuración")
    dialog.setMinimumWidth(520)

    layout = QVBoxLayout(dialog)

    cabecera = QLabel(
        "<b>Bienvenido a MindVoice.</b><p>"
        "Para funcionar necesita una clave de la API de Google Gemini "
        "(tiene plan gratuito).<br>"
        f"Pégala en <a href='{URL_CONSOLA}'>{URL_CONSOLA}</a> "
        "(entra con tu cuenta de Google y pulsa «Create API key»)."
    )
    cabecera.setWordWrap(True)
    cabecera.setOpenExternalLinks(True)
    cabecera.setTextFormat(Qt.TextFormat.RichText)
    layout.addWidget(cabecera)

    entrada = QLineEdit()
    entrada.setEchoMode(QLineEdit.EchoMode.Password)
    entrada.setPlaceholderText("Pega aquí tu clave de la API")
    layout.addWidget(entrada)

    aviso = QLabel("")
    aviso.setWordWrap(True)
    aviso.setStyleSheet("color: #b3261e;")
    layout.addWidget(aviso)

    nota = QLabel(
        f"Se guarda cifrada en tu equipo, en <code>{credenciales.store_location()}</code>, "
        "y solo dentro de tu cuenta de Windows. No se envía a ningún sitio."
    )
    nota.setWordWrap(True)
    nota.setStyleSheet("color: #666; font-size: 11px;")
    layout.addWidget(nota)

    botones = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    botones.button(QDialogButtonBox.StandardButton.Ok).setText("Guardar y continuar")
    botones.button(QDialogButtonBox.StandardButton.Cancel).setText("Ahora no")

    def _aceptar() -> None:
        problema = _validar(entrada.text())
        if problema:
            aviso.setText(problema)
            return
        dialog.accept()

    botones.accepted.connect(_aceptar)
    botones.rejected.connect(dialog.reject)
    entrada.returnPressed.connect(_aceptar)
    layout.addWidget(botones)

    entrada.setFocus()
    resultado = dialog.exec()

    clave = entrada.text().strip() if resultado else ""
    if owns_app:
        # Se cierra la QApplication para no dejar un bucle de eventos vivo en
        # este proceso, que va a lanzar el overlay como hijo.
        app.quit()
    return clave or None


def asegurar_clave(forzar: bool = False) -> str:
    """Devuelve una clave de Gemini, pidiendo una si no hay ninguna.

    Con ``forzar=True`` vuelve a preguntar aunque ya exista una guardada (para
    cambiarla desde los ajustes). Devuelve ``""`` si el usuario cancela.
    """
    if not forzar and _clave_disponible():
        return credenciales.get_api_key()

    clave = _preguntar_con_qt() or ""
    if not clave:
        return credenciales.get_api_key() if not forzar else ""

    if not credenciales.save_api_key(clave):
        logger.warning("No se pudo guardar la clave; se usará solo en memoria.")
    return clave
