"""Конфигурация модуля radar_position.

Зафиксируй значения один раз и не меняй во время работы с датасетом.
CS2 конвары для радара:
    cl_radar_rotate 0
    cl_radar_always_centered 0
    cl_hud_radar_scale 1.0
    cl_radar_scale 0.7
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RadarRegion:
    """Пиксельные координаты радара на экране (1920×1080)."""
    left:   int = 5
    top:    int = 5
    width:  int = 295
    height: int = 295

    def to_mss_dict(self) -> dict:
        return {
            "left":   self.left,
            "top":    self.top,
            "width":  self.width,
            "height": self.height,
        }

    @property
    def center_px(self) -> float:
        return self.left + self.width / 2

    @property
    def center_py(self) -> float:
        return self.top + self.height / 2


# ---------------------------------------------------------------------------
# HSV-профили по режиму игры
# ---------------------------------------------------------------------------

# Competitive / Deathmatch — жёлтый значок игрока
_HSV_COMPETITIVE = {
    "player_me":  dict(lower=(15, 120, 180), upper=(35, 255, 255), lower2=None, upper2=None),
    "enemy":      dict(lower=(0, 130, 100),  upper=(10, 255, 255), lower2=(170, 130, 100), upper2=(180, 255, 255)),
}

# Wingman 2v2 / с ботами — розово-малиновый значок игрока (H≈158, #b72b92)
_HSV_WINGMAN = {
    "player_me":  dict(lower=(148, 130, 120), upper=(168, 255, 255), lower2=None, upper2=None),
    "enemy":      dict(lower=(0, 130, 100),   upper=(10, 255, 255),  lower2=(170, 130, 100), upper2=(180, 255, 255)),
}

HSV_PROFILES: dict[str, dict] = {
    "competitive": _HSV_COMPETITIVE,
    "wingman":     _HSV_WINGMAN,
}


@dataclass
class RadarConfig:
    """Полная конфигурация модуля позиции по радару."""

    # --- Регион захвата ---
    region: RadarRegion = field(default_factory=RadarRegion)

    # --- CS2 конвары (зафиксированные) ---
    cl_hud_radar_scale: float = 1.0
    cl_radar_scale:     float = 0.7

    # --- Карта и режим игры ---
    map_name:  str = "de_dust2"
    # "competitive" — жёлтый значок, "wingman" — бордовый значок
    game_mode: str = "competitive"

    # --- Модель ---
    model_weights: str = ""
    confidence:    float = 0.50
    iou:           float = 0.45

    # --- HSV для player_me (берётся из профиля, если не переопределено вручную) ---
    # Используется при калибровке и сборе датасета (до обучения YOLO).
    # Competitive (жёлтый):  lower=(15,120,180) upper=(35,255,255)
    # Wingman (бордовый):    lower=(0,100,60)   upper=(12,255,180) + lower2/upper2
    hsv_lower:  tuple = (15, 120, 180)
    hsv_upper:  tuple = (35, 255, 255)
    # Второй диапазон для бордового/красного (HSV оборачивается через 0°)
    hsv_lower2: tuple | None = None
    hsv_upper2: tuple | None = None

    # --- HSV для enemy (красный ромб / красный ?) ---
    # Оба диапазона нужны т.к. красный оборачивается через 0° в HSV
    enemy_hsv_lower:  tuple = (0,   130, 100)
    enemy_hsv_upper:  tuple = (10,  255, 255)
    enemy_hsv_lower2: tuple = (170, 130, 100)
    enemy_hsv_upper2: tuple = (180, 255, 255)

    dot_min_area: int = 4     # мин. площадь пятна (пиксели²)
    dot_max_area: int = 400   # макс. площадь пятна

    # --- Рабочая частота RadarPositionReader ---
    poll_hz: float = 20.0

    def __post_init__(self) -> None:
        """Подставляет HSV из профиля в зависимости от game_mode."""
        profile = HSV_PROFILES.get(self.game_mode)
        if profile is None:
            return

        pm = profile["player_me"]
        self.hsv_lower  = pm["lower"]
        self.hsv_upper  = pm["upper"]
        self.hsv_lower2 = pm["lower2"]
        self.hsv_upper2 = pm["upper2"]

        en = profile["enemy"]
        self.enemy_hsv_lower  = en["lower"]
        self.enemy_hsv_upper  = en["upper"]
        self.enemy_hsv_lower2 = en["lower2"]
        self.enemy_hsv_upper2 = en["upper2"]

    # --- Пути ---
    @property
    def calibration_path(self) -> Path:
        return (
            Path(__file__).resolve().parent
            / "calibration"
            / f"{self.map_name}.json"
        )

    @property
    def weights_path(self) -> Path:
        if self.model_weights:
            return Path(self.model_weights)
        return (
            Path(__file__).resolve().parent
            / "weights"
            / f"{self.map_name}_best.pt"
        )
