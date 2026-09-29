"""Captura de pantalla, adquisición de voz y reproducción de audio."""

from .screen import ScreenCapture
from .microphone import MicrophoneCapture, list_input_devices
from .playback import AudioPlayer, list_output_devices

__all__ = [
    "ScreenCapture",
    "MicrophoneCapture",
    "AudioPlayer",
    "list_input_devices",
    "list_output_devices",
]