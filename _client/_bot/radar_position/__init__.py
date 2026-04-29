"""Модуль определения позиции игрока по радару CS2 через YOLOv12.

Быстрый старт:
    1. Калибровка (один раз):
       python -m radar_position.calibration.calibrator --map de_dust2

    2. Сбор датасета (ходи по карте ~5 минут):
       python -m dataset_tools.collector --map de_dust2 --count 500

    3. Обучение YOLOv12:
       python -m dataset_tools.trainer --map de_dust2

    4. Использование в nav_demo.py:
       reader = RadarPositionReader(map_name="de_dust2")
       reader.attach()
       valid, x, y, z, yaw = reader.snapshot()
"""

from .position_reader import RadarPositionReader
from .config import RadarConfig, RadarRegion
from .coord_converter import CoordConverter, AffineTransform, CalibrationPoint

__all__ = [
    "RadarPositionReader",
    "RadarConfig",
    "RadarRegion",
    "CoordConverter",
    "AffineTransform",
    "CalibrationPoint",
]
