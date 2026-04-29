from .base import BaseMouseControls
from .win32_mouse import Win32MouseControls


def get_mouse() -> BaseMouseControls:
    return Win32MouseControls()


__all__ = ["BaseMouseControls", "Win32MouseControls", "get_mouse"]
