"""Sistema de diseño del HUD: tokens y animaciones.

Se importa bajo demanda para no arrastrar PyQt6 cuando solo se usan los
tokens, que es el caso de cualquier herramienta que quiera saber el color de algo
sin levantar la interfaz.
"""

from __future__ import annotations

from . import tokens

__all__ = ["tokens"]