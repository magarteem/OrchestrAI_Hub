from typing import Any

from .base import BaseGrabber
from .mss_grabber import MSSGrabber


def get_grabber(name: str, **kwargs: Any) -> BaseGrabber:
    if name != "mss":
        raise ValueError(f"В демо доступен только grabber 'mss', передано: {name!r}")
    grabber = MSSGrabber()
    grabber.initialize(**kwargs)
    return grabber


__all__ = ["BaseGrabber", "MSSGrabber", "get_grabber"]
