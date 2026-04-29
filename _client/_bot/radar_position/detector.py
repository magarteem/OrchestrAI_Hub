"""YOLOv12 детектор маркеров на радаре CS2.

Классы модели:
    0 = player_me  (бордовый/жёлтый значок игрока)
    1 = enemy      (красный ромб, красный "?")

Старые модели с 1 классом (player_dot) поддерживаются через обратную совместимость:
    detect() → (cx, cy) или None — работает для обоих форматов моделей.

Новый API:
    detect_all() → list[Detection] — все найденные маркеры с классами.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

_LOG = logging.getLogger(__name__)

# ID классов
CLASS_PLAYER_ME  = 0   # бордовый/жёлтый — моя метка
CLASS_ENEMY      = 1   # красный ромб / красный "?" — враг
CLASS_PLAYER_DOT = 0   # алиас для старых моделей с 1 классом


@dataclass(frozen=True)
class Detection:
    """Один обнаруженный маркер на радаре."""
    class_id:   int
    class_name: str
    cx:         float   # центр bbox, пиксели
    cy:         float
    conf:       float


_CLASS_NAMES: dict[int, str] = {
    CLASS_PLAYER_ME: "player_me",
    CLASS_ENEMY:     "enemy",
}


class RadarDotDetector:
    """Детектор маркеров радара через YOLOv12.

    Args:
        weights_path: путь к .pt файлу весов
        confidence:   порог уверенности (0–1)
        iou:          порог IoU для NMS
        device:       "cuda" / "cpu" / None (авто)
    """

    def __init__(
        self,
        weights_path: str | Path,
        confidence:   float = 0.50,
        iou:          float = 0.45,
        device:       Optional[str] = None,
    ) -> None:
        self._weights = Path(weights_path)
        self._conf    = confidence
        self._iou     = iou
        self._device  = device
        self._model   = None
        self._nc: int = 2   # будет обновлено после загрузки модели

    # ------------------------------------------------------------------
    def load(self) -> None:
        """Загружает модель (один раз при старте)."""
        try:
            from ultralytics import YOLO
            import torch
        except ImportError as exc:
            raise ImportError(
                "Установи ultralytics: pip install ultralytics"
            ) from exc

        if not self._weights.is_file():
            raise FileNotFoundError(
                f"Веса модели не найдены: {self._weights}\n"
                "Сначала обучи модель: python -m dataset_tools.trainer --map <карта>"
            )

        if self._device is None:
            import torch
            self._device = "cuda" if torch.cuda.is_available() else "cpu"

        _LOG.info("Загрузка модели: %s → %s", self._weights.name, self._device)
        self._model = YOLO(str(self._weights))
        self._model.to(self._device)

        # Читаем кол-во классов из модели
        try:
            self._nc = self._model.model.nc  # type: ignore[attr-defined]
        except Exception:
            self._nc = 2

        _LOG.info("Модель готова (nc=%d)", self._nc)

        # Прогрев
        dummy = np.zeros((295, 295, 3), dtype=np.uint8)
        self._predict(dummy)

    def is_loaded(self) -> bool:
        return self._model is not None

    # ------------------------------------------------------------------
    def detect_all(self, frame_bgr: np.ndarray) -> list[Detection]:
        """Возвращает все обнаруженные маркеры на радаре.

        Args:
            frame_bgr: BGR кадр радара

        Returns:
            Список Detection (class_id, class_name, cx, cy, conf).
            Отсортирован по уверенности (выше сначала).
        """
        if self._model is None:
            raise RuntimeError("Сначала вызови load()")

        results = self._predict(frame_bgr)
        detections: list[Detection] = []

        for result in results:
            boxes = result.boxes
            for i in range(len(boxes)):
                cls_id = int(boxes.cls[i])
                conf   = float(boxes.conf[i])
                xyxy   = boxes[i].xyxy.cpu().numpy()[0]
                cx = float((xyxy[0] + xyxy[2]) / 2.0)
                cy = float((xyxy[1] + xyxy[3]) / 2.0)
                name = _CLASS_NAMES.get(cls_id, f"class_{cls_id}")
                detections.append(Detection(cls_id, name, cx, cy, conf))

        detections.sort(key=lambda d: d.conf, reverse=True)
        return detections

    def detect(self, frame_bgr: np.ndarray) -> Optional[Tuple[float, float]]:
        """Находит позицию игрока (class 0 = player_me / player_dot).

        Обратная совместимость с MemoryPositionReader.
        Возвращает (cx, cy) с наибольшей уверенностью или None.
        """
        detections = self.detect_all(frame_bgr)
        for det in detections:
            if det.class_id == CLASS_PLAYER_ME:
                return det.cx, det.cy
        return None

    def detect_enemies(self, frame_bgr: np.ndarray) -> list[Tuple[float, float]]:
        """Возвращает список (cx, cy) всех обнаруженных врагов (class 1)."""
        return [
            (d.cx, d.cy)
            for d in self.detect_all(frame_bgr)
            if d.class_id == CLASS_ENEMY
        ]

    def _predict(self, frame: np.ndarray):
        return self._model.predict(
            source=frame,
            verbose=False,
            conf=self._conf,
            iou=self._iou,
        )
