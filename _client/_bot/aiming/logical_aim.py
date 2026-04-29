"""Прицеливание: логическая сетка 1920×1080 + atan(FOV) + x360."""

from __future__ import annotations

from dataclasses import dataclass
from math import atan, degrees, radians, sqrt, tan
from typing import Optional, Tuple

from config import CaptureRegion, FovMouseConfig, LogicalAimSettings


@dataclass
class LogicalAimResult:
    angle_x: float
    angle_y: float
    mouse_x: int
    mouse_y: int
    pixel_distance: float
    angular_distance: float


class LogicalScreenAim:
    def __init__(
        self,
        capture: CaptureRegion,
        fov: FovMouseConfig,
        logical: LogicalAimSettings,
        *,
        crosshair_offset_x: float = 0.0,
        crosshair_offset_y: float = 0.0,
        invert_mouse_x: bool = False,
        invert_mouse_y: bool = False,
    ):
        self._lw = max(1, logical.width)
        self._lh = max(1, logical.height)
        cw = max(1, capture.width)
        ch = max(1, capture.height)
        self._sx = self._lw / cw
        self._sy = self._lh / ch

        off_x = crosshair_offset_x * self._sx
        off_y = crosshair_offset_y * self._sy
        self._cx = self._lw / 2.0 + off_x
        self._cy = self._lh / 2.0 + off_y

        hfov = fov.horizontal_fov_deg
        if fov.use_aspect_vertical:
            vfov = degrees(
                2 * atan(tan(radians(hfov / 2)) * self._lh / self._lw),
            )
        else:
            vfov = fov.vertical_fov_deg

        self._focal_x = (self._lw / 2.0) / tan(radians(hfov / 2))
        self._focal_y = (self._lh / 2.0) / tan(radians(vfov / 2))
        self._ppd = fov.x360 / 360.0
        self._inv_x = invert_mouse_x
        self._inv_y = invert_mouse_y

    def capture_to_logical(self, x: float, y: float) -> Tuple[float, float]:
        return x * self._sx, y * self._sy

    def pixel_distance_logical(self, tx: float, ty: float) -> float:
        lx, ly = self.capture_to_logical(tx, ty)
        return sqrt((lx - self._cx) ** 2 + (ly - self._cy) ** 2)

    def get_move(self, tx: float, ty: float, smoothing: float = 1.0) -> LogicalAimResult:
        lx, ly = self.capture_to_logical(tx, ty)
        ox = lx - self._cx
        oy = ly - self._cy
        ax = degrees(atan(ox / self._focal_x))
        ay = degrees(atan(oy / self._focal_y))
        mx = int(ax * self._ppd)
        my = int(ay * self._ppd)
        if smoothing > 1.0:
            mx = int(mx / smoothing)
            my = int(my / smoothing)
        if self._inv_x:
            mx = -mx
        if self._inv_y:
            my = -my
        pix = sqrt(ox * ox + oy * oy)
        ang = sqrt(ax * ax + ay * ay)
        return LogicalAimResult(ax, ay, mx, my, pix, ang)

    def get_move_clamped_step(
        self,
        tx: float,
        ty: float,
        smoothing: float = 1.0,
        max_step_logical: Optional[float] = None,
    ) -> LogicalAimResult:
        if max_step_logical is None or max_step_logical <= 0:
            return self.get_move(tx, ty, smoothing)
        lx, ly = self.capture_to_logical(tx, ty)
        dx, dy = lx - self._cx, ly - self._cy
        dist = sqrt(dx * dx + dy * dy)
        if dist <= 1e-9:
            return self.get_move(tx, ty, smoothing)
        if dist > max_step_logical:
            s = max_step_logical / dist
            lx = self._cx + dx * s
            ly = self._cy + dy * s
        return self.get_move(lx / self._sx, ly / self._sy, smoothing)
