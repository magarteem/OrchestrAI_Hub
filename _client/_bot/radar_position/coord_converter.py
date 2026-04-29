"""Преобразование координат: пиксели радара ↔ мировые координаты CS2.

Используется аффинное преобразование (6 параметров):
    wx = a*px + b*py + tx
    wy = c*px + d*py + ty

Для фиксированного радара (cl_radar_rotate 0, cl_radar_always_centered 0)
преобразование линейное и стабильное — не меняется пока зафиксированы
cl_hud_radar_scale и cl_radar_scale.

Сохранение/загрузка из JSON по имени карты.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

_LOG = logging.getLogger(__name__)


@dataclass
class CalibrationPoint:
    px: float   # пиксель X в кропе радара (0..width)
    py: float   # пиксель Y в кропе радара (0..height)
    wx: float   # мировая X (CS2)
    wy: float   # мировая Y (CS2)


@dataclass
class AffineTransform:
    """Параметры аффинного преобразования pixel → world."""
    a:  float   # px → wx коэффициент
    b:  float   # py → wx коэффициент
    tx: float   # wx смещение
    c:  float   # px → wy коэффициент
    d:  float   # py → wy коэффициент
    ty: float   # wy смещение

    def pixel_to_world(self, px: float, py: float) -> Tuple[float, float]:
        wx = self.a * px + self.b * py + self.tx
        wy = self.c * px + self.d * py + self.ty
        return wx, wy

    def world_to_pixel(self, wx: float, wy: float) -> Tuple[float, float]:
        """Обратное преобразование (для автоматической калибровки и сбора датасета)."""
        # Решаем систему 2×2:  [a b][px]   [wx - tx]
        #                       [c d][py] = [wy - ty]
        det = self.a * self.d - self.b * self.c
        if abs(det) < 1e-9:
            raise ValueError("Матрица преобразования вырождена — пересчитай калибровку")
        dx = wx - self.tx
        dy = wy - self.ty
        px = (self.d * dx - self.b * dy) / det
        py = (self.a * dy - self.c * dx) / det
        return px, py

    @property
    def scale_x(self) -> float:
        """Масштаб: мировых единиц на пиксель по оси X."""
        return math.hypot(self.a, self.c)

    @property
    def scale_y(self) -> float:
        """Масштаб: мировых единиц на пиксель по оси Y."""
        return math.hypot(self.b, self.d)


class CoordConverter:
    """Хранит точки калибровки и вычисляет аффинное преобразование.

    Минимум 3 точки для расчёта (6 уравнений → 6 неизвестных).
    Больше точек → least-squares, точнее результат.
    """

    def __init__(self) -> None:
        self._points: List[CalibrationPoint] = []
        self._transform: Optional[AffineTransform] = None

    # ------------------------------------------------------------------
    # Работа с точками
    # ------------------------------------------------------------------

    def add_point(self, px: float, py: float, wx: float, wy: float) -> None:
        self._points.append(CalibrationPoint(px=px, py=py, wx=wx, wy=wy))
        self._transform = None  # инвалидируем кэш

    def clear_points(self) -> None:
        self._points.clear()
        self._transform = None

    @property
    def point_count(self) -> int:
        return len(self._points)

    # ------------------------------------------------------------------
    # Расчёт трансформации
    # ------------------------------------------------------------------

    def fit(self) -> AffineTransform:
        """Вычисляет аффинное преобразование методом наименьших квадратов.

        Требует минимум 3 точки. При 3 точках — точное решение,
        при большем числе — overdetermined least-squares.
        """
        n = len(self._points)
        if n < 3:
            raise ValueError(
                f"Нужно минимум 3 точки калибровки, сейчас: {n}"
            )

        # Составляем матрицу A (n×3) и векторы bx, by
        A = np.array([[p.px, p.py, 1.0] for p in self._points], dtype=np.float64)
        bx = np.array([p.wx for p in self._points], dtype=np.float64)
        by = np.array([p.wy for p in self._points], dtype=np.float64)

        # Решение наименьших квадратов: A @ [a,b,tx]^T = bx
        (a, b, tx), *_ = np.linalg.lstsq(A, bx, rcond=None)
        (c, d, ty), *_ = np.linalg.lstsq(A, by, rcond=None)

        self._transform = AffineTransform(
            a=float(a), b=float(b), tx=float(tx),
            c=float(c), d=float(d), ty=float(ty),
        )

        self._log_residuals()
        return self._transform

    def _log_residuals(self) -> None:
        if self._transform is None:
            return
        errors = []
        for p in self._points:
            wx_pred, wy_pred = self._transform.pixel_to_world(p.px, p.py)
            err = math.hypot(wx_pred - p.wx, wy_pred - p.wy)
            errors.append(err)
        mean_err = sum(errors) / len(errors)
        max_err  = max(errors)
        _LOG.info(
            "Калибровка: %d точек | средняя ошибка %.1f ед. | макс %.1f ед.",
            len(self._points), mean_err, max_err,
        )

    @property
    def transform(self) -> Optional[AffineTransform]:
        return self._transform

    # ------------------------------------------------------------------
    # Быстрый доступ к преобразованию
    # ------------------------------------------------------------------

    def pixel_to_world(self, px: float, py: float) -> Tuple[float, float]:
        if self._transform is None:
            raise RuntimeError("Сначала вызови fit() или load()")
        return self._transform.pixel_to_world(px, py)

    def world_to_pixel(self, wx: float, wy: float) -> Tuple[float, float]:
        if self._transform is None:
            raise RuntimeError("Сначала вызови fit() или load()")
        return self._transform.world_to_pixel(wx, wy)

    # ------------------------------------------------------------------
    # Сохранение / загрузка
    # ------------------------------------------------------------------

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "points": [asdict(p) for p in self._points],
            "transform": asdict(self._transform) if self._transform else None,
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        _LOG.info("Калибровка сохранена: %s (%d точек)", path, len(self._points))

    def load(self, path: str | Path) -> AffineTransform:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"Файл калибровки не найден: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))

        self._points = [CalibrationPoint(**p) for p in data.get("points", [])]

        tr = data.get("transform")
        if tr:
            self._transform = AffineTransform(**tr)
        else:
            # Пересчитываем из точек
            self._transform = self.fit()

        _LOG.info(
            "Калибровка загружена: %s | %d точек | масштаб %.1f ед/px",
            path, len(self._points),
            (self._transform.scale_x + self._transform.scale_y) / 2,
        )
        return self._transform

    @classmethod
    def from_file(cls, path: str | Path) -> "CoordConverter":
        conv = cls()
        conv.load(path)
        return conv
