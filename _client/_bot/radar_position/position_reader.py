"""Главный класс модуля: RadarPositionReader.

Полный аналог MemoryPositionReader по API — взаимозаменяем в nav_demo.py.

    # Вместо:
    reader = MemoryPositionReader()
    reader.attach()

    # Используй:
    reader = RadarPositionReader(map_name="de_dust2")
    reader.attach()

    # Интерфейс одинаковый:
    valid, x, y, z, yaw = reader.snapshot()

    # Дополнительно — позиции врагов (мировые координаты):
    enemies = reader.enemies   # list[tuple[float, float]] — [(wx, wy), ...]

Ограничения:
    z   = 0.0  (высота недоступна из радара)
    yaw = вычисляется из дельты позиции (приближение)
"""

from __future__ import annotations

import logging
import math
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from .config import RadarConfig
from .coord_converter import CoordConverter
from .detector import RadarDotDetector, CLASS_ENEMY
from .radar_grabber import RadarGrabber

_LOG = logging.getLogger(__name__)


class RadarPositionReader:
    """Определяет позицию игрока (x, y) по скриншоту радара через YOLOv12.

    Полностью совместим с MemoryPositionReader.snapshot().
    Дополнительно предоставляет позиции врагов через свойство enemies.

    Пример:
        cfg    = RadarConfig(map_name="de_poseidon", game_mode="wingman")
        reader = RadarPositionReader(cfg=cfg)
        reader.attach()

        valid, x, y, z, yaw = reader.snapshot()
        for wx, wy in reader.enemies:
            print(f"Враг: ({wx:.0f}, {wy:.0f})")
    """

    def __init__(self, cfg: Optional[RadarConfig] = None, *, map_name: str = "de_dust2") -> None:
        if isinstance(cfg, str):
            # RadarPositionReader("de_dust2") — обратная совместимость
            map_name = cfg
            cfg = None
        self._cfg = cfg or RadarConfig(map_name=map_name)

        self._converter = CoordConverter()
        self._detector  = RadarDotDetector(
            weights_path=self._cfg.weights_path,
            confidence=self._cfg.confidence,
            iou=self._cfg.iou,
        )
        self._grabber = RadarGrabber(self._cfg)

        # Состояние игрока
        self._x:     float = 0.0
        self._y:     float = 0.0
        self._z:     float = 0.0
        self._yaw:   float = 0.0
        self._valid: bool  = False

        # Для вычисления yaw из дельты позиции
        self._prev_x: Optional[float] = None
        self._prev_y: Optional[float] = None

        # Позиции врагов (мировые координаты, обновляются каждый тик)
        self._enemies: list[tuple[float, float]] = []

        self._lock    = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # ------------------------------------------------------------------
    # Инициализация
    # ------------------------------------------------------------------

    def attach(self) -> None:
        """Загружает калибровку и модель, запускает фоновый поллинг."""
        self._load_calibration()
        self._detector.load()

        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="radar-reader",
        )
        self._thread.start()
        _LOG.info("RadarPositionReader запущен (карта: %s)", self._cfg.map_name)

    def detach(self) -> None:
        self._running = False

    def _load_calibration(self) -> None:
        path = self._cfg.calibration_path
        if not path.is_file():
            raise FileNotFoundError(
                f"Файл калибровки не найден: {path}\n"
                "Сначала запусти калибровку:\n"
                f"  python -m radar_position.calibration.calibrator --map {self._cfg.map_name}"
            )
        self._converter.load(path)

    # ------------------------------------------------------------------
    # Фоновый поллинг
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        self._grabber.open()
        _LOG.info("RadarReader thread: grabber открыт")

        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.set_device(0)
        except Exception:
            pass

        interval = 1.0 / self._cfg.poll_hz
        try:
            while self._running:
                t0 = time.perf_counter()
                try:
                    self._read_once()
                except Exception as exc:
                    _LOG.warning("RadarReader poll error: %s", exc)
                    with self._lock:
                        self._valid = False
                elapsed = time.perf_counter() - t0
                sleep = interval - elapsed
                if sleep > 0:
                    time.sleep(sleep)
        finally:
            self._grabber.close()

    def _read_once(self) -> None:
        frame = self._grabber.grab_bgr()
        if frame is None:
            with self._lock:
                self._valid = False
            return

        detections = self._detector.detect_all(frame)

        # Позиция игрока (class 0 = player_me)
        player_dot = next((d for d in detections if d.class_id == 0), None)
        if player_dot is None:
            with self._lock:
                self._valid = False
                self._enemies = []
            return

        wx, wy = self._converter.pixel_to_world(player_dot.cx, player_dot.cy)
        yaw = self._compute_yaw(wx, wy)

        # Позиции врагов (class 1 = enemy) → конвертируем в мировые координаты
        enemies: list[tuple[float, float]] = []
        for det in detections:
            if det.class_id == CLASS_ENEMY:
                try:
                    ewx, ewy = self._converter.pixel_to_world(det.cx, det.cy)
                    enemies.append((ewx, ewy))
                except Exception:
                    pass

        with self._lock:
            self._x = wx
            self._y = wy
            self._z = 0.0
            self._yaw = yaw
            self._valid = True
            self._enemies = enemies

    def _compute_yaw(self, wx: float, wy: float) -> float:
        """Приближённый yaw из направления движения."""
        if self._prev_x is None:
            self._prev_x = wx
            self._prev_y = wy
            return self._yaw

        dx = wx - self._prev_x
        dy = wy - self._prev_y
        dist = math.hypot(dx, dy)

        if dist > 5.0:
            new_yaw = math.degrees(math.atan2(dy, dx))
            self._prev_x = wx
            self._prev_y = wy
            return new_yaw

        return self._yaw

    # ------------------------------------------------------------------
    # Публичный API (совместим с MemoryPositionReader)
    # ------------------------------------------------------------------

    def snapshot(self) -> Tuple[bool, float, float, float, float]:
        """Возвращает (valid, x, y, z, yaw_deg) атомарно.

        z   = 0.0 (недоступно из радара)
        yaw = приближение по направлению движения
        """
        with self._lock:
            return self._valid, self._x, self._y, self._z, self._yaw

    @property
    def valid(self) -> bool:
        with self._lock:
            return self._valid

    @property
    def position(self) -> Tuple[float, float, float]:
        with self._lock:
            return self._x, self._y, self._z

    @property
    def yaw_deg(self) -> float:
        with self._lock:
            return self._yaw

    @property
    def enemies(self) -> list[tuple[float, float]]:
        """Список мировых координат врагов [(wx, wy), ...] из последнего кадра.

        Включает класс 1 (enemy): красный ромб и красный "?".
        Пустой список если враги не обнаружены или модель обучена с 1 классом.
        """
        with self._lock:
            return list(self._enemies)
