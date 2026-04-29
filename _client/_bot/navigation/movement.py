"""Управление клавишами WASD и вспомогательные функции расчёта курса."""

from __future__ import annotations

import math
import threading
import time

import keyboard

from .navmesh import NavNode

# Расстояние (2D, единицы карты), при котором вейпоинт считается достигнутым
WAYPOINT_REACH_RADIUS: float = 80.0

# Поддерживаемые значения action на узлах
NODE_ACTIONS = ("", "jump", "crouch", "shift", "look_north", "look_south", "look_east", "look_west")

# Направления для look_* (в градусах, формат CS2: 0°=восток, 90°=север)
_LOOK_ANGLES: dict[str, float] = {
    "look_north": 90.0,
    "look_south": -90.0,
    "look_east":  0.0,
    "look_west":  180.0,
}


class MovementController:
    """Управляет нажатием клавиш через библиотеку keyboard.

    Поддерживает WASD + прыжок, приседание, шифт, повороты взгляда.
    Хранит состояние нажатых клавиш, чтобы не посылать повторные press/release.
    """

    _KEYS = ("w", "a", "s", "d", "space", "ctrl", "shift")

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _press(self, key: str) -> None:
        if key not in self._active:
            keyboard.press(key)
            self._active.add(key)

    def _release(self, key: str) -> None:
        if key in self._active:
            keyboard.release(key)
            self._active.discard(key)

    # ------------------------------------------------------------------
    def move_forward(self) -> None:
        with self._lock:
            self._press("w")
            self._release("s")

    def move_back(self) -> None:
        with self._lock:
            self._press("s")
            self._release("w")

    def strafe_left(self) -> None:
        with self._lock:
            self._press("a")
            self._release("d")

    def strafe_right(self) -> None:
        with self._lock:
            self._press("d")
            self._release("a")

    def stop_lateral(self) -> None:
        with self._lock:
            self._release("a")
            self._release("d")

    def stop(self) -> None:
        with self._lock:
            self._release("w")
            self._release("s")
            self._release("a")
            self._release("d")

    def move_local(self, forward: float, right: float, threshold: float = 0.15) -> None:
        """Движение в локальных осях игрока.

        forward: >0 вперед, <0 назад
        right:   >0 вправо, <0 влево
        """
        with self._lock:
            if forward > threshold:
                self._press("w")
                self._release("s")
            elif forward < -threshold:
                self._press("s")
                self._release("w")
            else:
                self._release("w")
                self._release("s")

            if right > threshold:
                self._press("d")
                self._release("a")
            elif right < -threshold:
                self._press("a")
                self._release("d")
            else:
                self._release("a")
                self._release("d")

    def release_all(self) -> None:
        with self._lock:
            for k in list(self._active):
                keyboard.release(k)
            self._active.clear()

    # ------------------------------------------------------------------
    # Действия на узлах (action)
    # ------------------------------------------------------------------

    def jump(self) -> None:
        """Одиночный прыжок (пробел)."""
        with self._lock:
            keyboard.press("space")
        time.sleep(0.08)
        with self._lock:
            keyboard.release("space")
            self._active.discard("space")

    def crouch_press(self) -> None:
        """Начать приседать (зажать Ctrl)."""
        with self._lock:
            self._press("ctrl")

    def crouch_release(self) -> None:
        """Встать (отпустить Ctrl)."""
        with self._lock:
            self._release("ctrl")

    def shift_press(self) -> None:
        """Начать идти тихо (зажать Shift)."""
        with self._lock:
            self._press("shift")

    def shift_release(self) -> None:
        """Отпустить Shift."""
        with self._lock:
            self._release("shift")


# ------------------------------------------------------------------
# Утилиты для расчёта угла поворота
# ------------------------------------------------------------------

def yaw_to_target(
    pos_x: float,
    pos_y: float,
    target_x: float,
    target_y: float,
) -> float:
    """Угол (градусы) от pos к target в плоскости XY.

    Использует math.atan2: 0° = восток (+X), 90° = север (+Y).
    """
    return math.degrees(math.atan2(target_y - pos_y, target_x - pos_x))


def delta_yaw(current_deg: float, desired_deg: float) -> float:
    """Кратчайший угловой сдвиг из current в desired, диапазон [-180, 180]."""
    return (desired_deg - current_deg + 180.0) % 360.0 - 180.0


def reached_waypoint(
    pos_x: float,
    pos_y: float,
    node: NavNode,
    radius: float = WAYPOINT_REACH_RADIUS,
) -> bool:
    dx = node.x - pos_x
    dy = node.y - pos_y
    return math.sqrt(dx * dx + dy * dy) <= radius
