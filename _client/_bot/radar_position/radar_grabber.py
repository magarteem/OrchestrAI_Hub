"""Захват региона радара с экрана через MSS.

Поддерживает два режима:
  - Полноэкранный CS2: координаты радара абсолютные (left=5, top=5)
  - Оконный CS2:       координаты авто-вычисляются через win32gui
                       (позиция окна + смещение контента + offset радара)
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

import mss
import numpy as np

from .config import RadarConfig, RadarRegion

_LOG = logging.getLogger(__name__)

# Смещения оконного режима CS2 (заголовок + рамки)
# left_border, top_border (title bar), right_border, bottom_border
_WINDOW_BORDER = (8, 30, 8, 8)

_CS2_WINDOW_TITLE = "Counter-Strike 2"


def _get_cs2_window_offset() -> Optional[Tuple[int, int]]:
    """Возвращает (left, top) угол области контента окна CS2.

    Учитывает рамку окна и заголовок.
    Возвращает None если окно не найдено (CS2 не запущен).
    """
    try:
        import win32gui
        hwnd = win32gui.FindWindow(None, _CS2_WINDOW_TITLE)
        if not hwnd:
            return None
        rect = win32gui.GetWindowRect(hwnd)
        # rect = (left, top, right, bottom) — включая рамку
        content_left = rect[0] + _WINDOW_BORDER[0]
        content_top  = rect[1] + _WINDOW_BORDER[1]
        return content_left, content_top
    except Exception as exc:
        _LOG.debug("win32gui error: %s", exc)
        return None


class RadarGrabber:
    """Захватывает кроп радара с экрана.

    Автоматически определяет режим:
    - Полноэкранный → использует region.left/top напрямую
    - Оконный       → добавляет смещение окна CS2

    Поддерживает контекстный менеджер:
        with RadarGrabber(cfg) as grabber:
            frame = grabber.grab()
    """

    def __init__(self, cfg: RadarConfig) -> None:
        self._cfg    = cfg
        self._region = cfg.region
        self._sct: Optional[mss.mss] = None
        self._abs_region: Optional[dict] = None  # кешированный абсолютный регион

    # ------------------------------------------------------------------
    def open(self) -> None:
        if self._sct is None:
            self._sct = mss.mss()
        self._resolve_region()

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None

    def __enter__(self) -> "RadarGrabber":
        self.open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    def _resolve_region(self) -> dict:
        """Вычисляет абсолютный регион захвата с учётом позиции окна CS2."""
        offset = _get_cs2_window_offset()

        if offset is None:
            # Окно не найдено — используем координаты как есть (полноэкранный режим)
            _LOG.debug("Окно CS2 не найдено — используем абсолютные координаты")
            self._abs_region = self._region.to_mss_dict()
        else:
            win_left, win_top = offset
            self._abs_region = {
                "left":   win_left + self._region.left,
                "top":    win_top  + self._region.top,
                "width":  self._region.width,
                "height": self._region.height,
            }
            _LOG.info(
                "Окно CS2 найдено: content offset=(%d, %d) → "
                "радар на экране: left=%d, top=%d",
                win_left, win_top,
                self._abs_region["left"],
                self._abs_region["top"],
            )

        return self._abs_region

    def refresh_window_position(self) -> None:
        """Пересчитать позицию окна (вызвать если CS2 был перемещён)."""
        self._resolve_region()

    # ------------------------------------------------------------------
    def grab(self) -> Optional[np.ndarray]:
        """Захватывает кадр радара.

        Returns:
            np.ndarray shape (H, W, 4) BGRA или None при ошибке.
        """
        if self._sct is None:
            self.open()
        if self._abs_region is None:
            self._resolve_region()
        try:
            raw = self._sct.grab(self._abs_region)
            return np.array(raw)
        except Exception:
            return None

    def grab_bgr(self) -> Optional[np.ndarray]:
        """Возвращает кадр в BGR (без альфа-канала) — для OpenCV/YOLO."""
        frame = self.grab()
        if frame is None:
            return None
        return frame[:, :, :3]

    @property
    def region(self) -> RadarRegion:
        return self._region

    @property
    def abs_region(self) -> Optional[dict]:
        """Абсолютный регион захвата на экране (с учётом позиции окна)."""
        return self._abs_region
