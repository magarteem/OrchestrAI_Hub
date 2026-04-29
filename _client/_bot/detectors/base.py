from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


class BaseDetector(ABC):

    def __init__(
        self,
        class_names: List[str],
        weights_path: str,
        confidence_threshold: float = 0.5,
        iou_threshold: float = 0.45,
    ):
        self.class_names = class_names
        self.weights_path = weights_path
        self.confidence_threshold = confidence_threshold
        self.iou_threshold = iou_threshold
        self.colors = self._generate_colors()

    def _generate_colors(self) -> List[Tuple[int, int, int]]:
        import random

        return [
            (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
            for _ in self.class_names
        ]

    def set_colors(self, colors: List[Tuple[int, int, int]]) -> None:
        self.colors = colors

    def get_class_name(self, class_id: int) -> str:
        if 0 <= class_id < len(self.class_names):
            return self.class_names[class_id]
        return "unknown"

    @abstractmethod
    def detect(
        self,
        image: np.ndarray,
        verbose: bool = False,
    ) -> Dict[str, List[Dict[str, Any]]]:
        pass

    def filter_by_classes(
        self,
        detections: Dict[str, List[Dict]],
        include_classes: Tuple[str, ...],
    ) -> List[Dict[str, Any]]:
        filtered = []

        for class_name, boxes in detections.items():
            if class_name in include_classes:
                for box in boxes:
                    box_copy = box.copy()
                    box_copy["tcls"] = class_name
                    filtered.append(box_copy)

        return filtered

    def draw_boxes(
        self,
        image: np.ndarray,
        detections: Dict[str, List[Dict]],
        min_confidence: float = 0.0,
        line_thickness: int = 2,
    ) -> np.ndarray:
        import cv2

        for class_name, boxes in detections.items():
            for box in boxes:
                if box["conf"] < min_confidence:
                    continue

                x1, y1, x2, y2 = [int(v) for v in box["xyxy"]]
                color = self.colors[box["cls"]]

                cv2.rectangle(image, (x1, y1), (x2, y2), color, line_thickness)

                label = f"{class_name} {box['conf']:.2f}"
                font_scale = line_thickness / 3
                font_thickness = max(line_thickness - 1, 1)

                (text_w, text_h), _ = cv2.getTextSize(
                    label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, font_thickness
                )

                cv2.rectangle(
                    image,
                    (x1, y1 - text_h - 4),
                    (x1 + text_w, y1),
                    color,
                    -1,
                )

                cv2.putText(
                    image,
                    label,
                    (x1, y1 - 2),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    font_scale,
                    (255, 255, 255),
                    font_thickness,
                )

        return image

    def draw_aim_point(
        self,
        image: np.ndarray,
        x: float,
        y: float,
        color: Tuple[int, int, int] = (0, 255, 0),
        radius: int = 5,
    ) -> np.ndarray:
        import cv2

        cv2.circle(image, (int(x), int(y)), radius, color, -1)
        cv2.circle(image, (int(x), int(y)), radius + 2, (255, 255, 255), 1)
        return image
