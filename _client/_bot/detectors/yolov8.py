from typing import Any, Dict, List, Optional

import numpy as np
import torch

from .base import BaseDetector


class YOLOv8Detector(BaseDetector):

    def __init__(
        self,
        class_names: List[str],
        weights_path: str,
        confidence_threshold: float = 0.5,
        iou_threshold: float = 0.45,
        device: Optional[str] = None,
        half_precision: Optional[bool] = None,
    ):
        super().__init__(
            class_names=class_names,
            weights_path=weights_path,
            confidence_threshold=confidence_threshold,
            iou_threshold=iou_threshold,
        )

        try:
            from ultralytics import YOLO
        except ImportError as e:
            raise ImportError(
                "Нужен пакет ultralytics: pip install ultralytics",
            ) from e

        if device is None:
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            self.device = device

        if half_precision is None:
            self.half_precision = self.device == "cuda"
        else:
            self.half_precision = half_precision

        self.model = YOLO(weights_path)
        self.model.to(self.device)

        self._warmup()

    def _warmup(self) -> None:
        dummy = np.zeros((640, 640, 3), dtype=np.uint8)
        self.detect(dummy, verbose=False)

    def detect(
        self,
        image: np.ndarray,
        verbose: bool = False,
    ) -> Dict[str, List[Dict[str, Any]]]:
        if image is None or image.size == 0:
            return {}

        if len(image.shape) != 3 or image.shape[2] > 4:
            return {}

        if image.shape[2] == 4:
            image = image[:, :, :3]

        results = self.model.predict(
            source=image,
            verbose=verbose,
            half=self.half_precision,
            conf=self.confidence_threshold,
            iou=self.iou_threshold,
        )

        detections: Dict[str, List[Dict]] = {}

        for result in results:
            boxes = result.boxes

            for i, cls_id in enumerate(boxes.cls):
                cls_id_int = int(cls_id)
                class_name = self.get_class_name(cls_id_int)

                if class_name not in detections:
                    detections[class_name] = []

                xyxy = boxes[i].xyxy.cpu().numpy()[0].tolist()
                conf = boxes.conf[i].item()

                detections[class_name].append(
                    {
                        "cls": cls_id_int,
                        "conf": conf,
                        "xyxy": xyxy,
                    }
                )

        return detections

    def detect_and_filter(
        self,
        image: np.ndarray,
        include_classes: tuple,
        verbose: bool = False,
    ) -> List[Dict[str, Any]]:
        detections = self.detect(image, verbose)
        return self.filter_by_classes(detections, include_classes)
