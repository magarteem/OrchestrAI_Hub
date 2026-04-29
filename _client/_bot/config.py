"""Конфигурация демо: детекция + прицеливание (логика 1920×1080)."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Tuple


class Team(str, Enum):
    CT = "ct"
    T = "t"


@dataclass
class CaptureRegion:
    left: int = 0
    top: int = 0
    width: int = 1920
    height: int = 1080

    def to_dict(self) -> Dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


@dataclass
class LogicalAimSettings:
    """Логическая сетка прицеливания (игра Full HD)."""

    width: int = 1920
    height: int = 1080


@dataclass
class FovMouseConfig:
    horizontal_fov_deg: float = 106.26
    vertical_fov_deg: float = 73.74
    x360: int = 7792
    use_aspect_vertical: bool = True


@dataclass
class AimSelectConfig:
    current_team: Team = Team.CT
    prioritize_heads: bool = True
    body_aim_y_fraction: float = 0.38
    head_aim_y_fraction: float = 0.52

    @property
    def enemy_classes(self) -> Tuple[str, str]:
        if self.current_team == Team.CT:
            return ("t", "th")
        return ("c", "ch")


@dataclass
class AimRuntimeConfig:
    """Прицел по умолчанию включён; Caps Lock — только пауза."""

    aim_enabled_by_default: bool = True
    activation_hotkey_scan_code: int = 58
    team_toggle_hotkey: str = "ctrl+t"
    # Если выключен subpixel-накопитель — не двигать мышь, пока ошибка не больше этого (лог. px).
    dead_zone_logical: float = 2.0
    max_assist_distance_capture_px: float = 450.0
    smoothing_factor: float = 1.0
    invert_mouse_x: bool = False
    invert_mouse_y: bool = False
    crosshair_offset_x: float = 0.0
    crosshair_offset_y: float = 0.0
    # При use_closed_loop: макс. сдвиг «логических» px к цели за один кадр (больше — быстрее догон).
    closed_loop_max_pixel_logical: float = 96.0
    # Множитель к dx/dy мыши после расчёта (основной рычаг «быстрее наводится»).
    aim_mouse_gain: float = 1.35
    use_closed_loop: bool = True
    # Копит дробную часть dx/dy — прицел не «отлипает» из‑за int(); держит цель каждый кадр.
    use_subpixel_mouse_accumulator: bool = True

    # Авто-выстрел, когда прицел уже на цели (логика как в csgobot; для сетевой игры выключи).
    auto_shoot: bool = True
    auto_shoot_pixel_tolerance: float = 45.0
    auto_shoot_cooldown_s: float = 0.15
    head_confidence: float = 0.8
    body_confidence: float = 0.7


@dataclass
class ViewerConfig:
    """Окно предпросмотра OpenCV."""

    # Только ASCII: иначе на Windows заголовок cv2.imshow может отображаться как \\uXXXX.
    title: str = "CS2 detection (preview)"
    max_display_size: Tuple[int, int] = (1920, 1080)


@dataclass
class DemoConfig:
    """Полный набор настроек демо."""

    window_title: str = "Counter-Strike 2"
    border_offsets: Tuple[int, int, int, int] = (8, 30, 16, 39)
    capture_region: CaptureRegion = field(default_factory=CaptureRegion)

    weights_path: str = ""
    # 0.7 часто мало рамок на ботах/в дыме; при необходимости подними обратно.
    confidence_threshold: float = 0.45
    iou_threshold: float = 0.2

    class_names: List[str] = field(
        default_factory=lambda: ["c", "ch", "t", "th"],
    )
    class_colors: List[Tuple[int, int, int]] = field(
        default_factory=lambda: [
            (245, 185, 115),
            (255, 50, 0),
            (0, 208, 247),
            (0, 82, 247),
        ],
    )

    logical_aim: LogicalAimSettings = field(default_factory=LogicalAimSettings)
    fov_mouse: FovMouseConfig = field(default_factory=FovMouseConfig)
    aim_select: AimSelectConfig = field(default_factory=AimSelectConfig)
    aim_runtime: AimRuntimeConfig = field(default_factory=AimRuntimeConfig)

    viewer: ViewerConfig = field(default_factory=ViewerConfig)


@dataclass
class NavConfig:
    """Настройки навигации по navmesh."""

    map_name: str = "de_dust2"
    # Порт GSI-сервера (CS2 → POST → localhost:port)
    gsi_port: int = 3000
    # Минимальный угол рассогласования курса (°) для поворота мыши
    yaw_dead_zone: float = 15.0
    # Доля delta_yaw, передаваемая в мышь за один тик навигации
    rotation_speed: float = 0.25
    # Радиус (единицы карты) для считывания вейпоинта достигнутым
    waypoint_radius: float = 80.0
    # Частота тика навигационного цикла (Гц)
    nav_hz: float = 20.0


def round_to_multiple(number: int, multiple: int) -> int:
    return multiple * round(number / multiple)


def adjust_region_to_multiple(
    region: CaptureRegion,
    multiple: int = 32,
) -> CaptureRegion:
    return CaptureRegion(
        left=region.left,
        top=region.top,
        width=round_to_multiple(region.width, multiple),
        height=round_to_multiple(region.height, multiple),
    )
