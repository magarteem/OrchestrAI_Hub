"""Отдельный контроллер взгляда: выбор yaw и движение мыши."""

from __future__ import annotations

from dataclasses import dataclass

from controls.mouse import get_mouse

from .movement import delta_yaw, yaw_to_target
from .navmesh import NavNode


@dataclass
class LookConfig:
    yaw_dead_zone: float = 6.0
    rotation_speed: float = 0.25
    max_mouse_step: int = 120


class LookController:
    def __init__(self, fov_x360: int, cfg: LookConfig | None = None) -> None:
        self._mouse = get_mouse()
        self._fov_x360 = fov_x360
        self._cfg = cfg or LookConfig()

    def resolve_desired_yaw(
        self,
        pos_x: float,
        pos_y: float,
        fallback_target: NavNode,
        edge_look: dict | None,
    ) -> float:
        if edge_look:
            mode = str(edge_look.get("mode", "")).strip().lower()
            if mode == "fixed_yaw":
                try:
                    return float(edge_look["yaw"])
                except (KeyError, TypeError, ValueError):
                    pass
            elif mode == "look_point":
                try:
                    tx = float(edge_look["x"])
                    ty = float(edge_look["y"])
                    return yaw_to_target(pos_x, pos_y, tx, ty)
                except (KeyError, TypeError, ValueError):
                    pass
        return yaw_to_target(pos_x, pos_y, fallback_target.x, fallback_target.y)

    def apply(self, current_yaw: float, desired_yaw: float) -> None:
        dyaw = delta_yaw(current_yaw, desired_yaw)
        if abs(dyaw) <= self._cfg.yaw_dead_zone:
            return
        mouse_dx = int((-dyaw / 360.0) * self._fov_x360 * self._cfg.rotation_speed)
        if mouse_dx == 0:
            return
        mouse_dx = max(-self._cfg.max_mouse_step, min(self._cfg.max_mouse_step, mouse_dx))
        self._mouse.move_relative(mouse_dx, 0)
