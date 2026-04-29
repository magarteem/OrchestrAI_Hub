"""Выбор цели: только враги по команде, приоритет головы, расстояние от центра кадра."""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Any, Dict, List, Optional, Tuple

from config import AimSelectConfig, CaptureRegion


@dataclass
class Target:
    class_name: str
    class_id: int
    confidence: float
    x1: float
    y1: float
    x2: float
    y2: float
    aim_x: float
    aim_y: float
    distance: float
    is_head: bool

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1


class TargetSelector:
    HEAD_CLASSES = {"ch", "th"}

    def __init__(self, aim_config: AimSelectConfig, screen: CaptureRegion) -> None:
        self.config = aim_config
        self.screen = screen
        self._center_x = screen.width / 2
        self._center_y = screen.height / 2

    def _is_enemy(self, class_name: str) -> bool:
        return class_name in self.config.enemy_classes

    def _is_head(self, class_name: str) -> bool:
        return class_name in self.HEAD_CLASSES

    def _calculate_aim_point(self, bbox: Dict[str, Any]) -> Tuple[float, float]:
        x1, y1, x2, y2 = bbox["xyxy"]
        w, h = x2 - x1, y2 - y1
        cx = x1 + w / 2
        tcls = bbox.get("tcls", bbox.get("cls_name", ""))
        if tcls not in self.HEAD_CLASSES:
            fy = self.config.body_aim_y_fraction
            cy = y1 + h * fy
        else:
            fy = self.config.head_aim_y_fraction
            cy = y1 + h * fy
        return cx, cy

    def _bbox_to_target(self, bbox: Dict[str, Any]) -> Target:
        x1, y1, x2, y2 = bbox["xyxy"]
        class_name = bbox.get("tcls", bbox.get("cls_name", "unknown"))
        class_id = int(bbox.get("cls", 0))
        aim_x, aim_y = self._calculate_aim_point(bbox)
        dist = sqrt((aim_x - self._center_x) ** 2 + (aim_y - self._center_y) ** 2)
        return Target(
            class_name=class_name,
            class_id=class_id,
            confidence=float(bbox.get("conf", 0.0)),
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            aim_x=aim_x,
            aim_y=aim_y,
            distance=dist,
            is_head=self._is_head(class_name),
        )

    def select_best_target(
        self,
        detections: Dict[str, List[Dict[str, Any]]],
        max_distance: Optional[float] = None,
    ) -> Optional[Target]:
        enemies: List[Target] = []
        for class_name, boxes in detections.items():
            if self._is_enemy(class_name):
                for box in boxes:
                    bc = box.copy()
                    bc["tcls"] = class_name
                    enemies.append(self._bbox_to_target(bc))
        if not enemies:
            return None
        if max_distance is not None:
            enemies = [t for t in enemies if t.distance <= max_distance]
        if not enemies:
            return None
        if self.config.prioritize_heads:
            heads = [t for t in enemies if t.is_head]
            if heads:
                nh = min(heads, key=lambda t: t.distance)
                nb = min(enemies, key=lambda t: t.distance)
                if nh.distance <= nb.distance * 1.5:
                    return nh
        return min(enemies, key=lambda t: t.distance)
