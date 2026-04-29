"""
Демо: захват CS2, YOLOv8, рамки + собственное прицеливание (логика 1920×1080).

Наводка на противника включена сразу (удержание каждый кадр).
При включённом auto_shoot — ЛКМ, когда цель в допуске по логическим px и conf (как csgobot).
Caps Lock — пауза / возобновление.
Ctrl+T — смена стороны (CT ↔ T).
Q в окне предпросмотра — выход.

Запуск: python main.py
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import cv2
import keyboard
import numpy as np

from aiming import LogicalScreenAim, TargetSelector
from config import CaptureRegion, DemoConfig, Team, adjust_region_to_multiple
from controls.mouse import get_mouse
from detectors import YOLOv8Detector
from grabbers import get_grabber
from utils.win32 import get_window_rect, set_dpi_aware

_LOG = logging.getLogger("cs2_detection_demo")

_PREFERRED_WEIGHTS = "cs2_yolov8m_640_augmented_v4.pt"


def _is_git_lfs_pointer(path: Path) -> bool:
    """True, если файл — текстовый указатель Git LFS, а не чекпоинт PyTorch."""
    try:
        with path.open("rb") as f:
            head = f.read(80)
    except OSError:
        return True
    return head.startswith(b"version https://git-lfs.github.com/spec/v1")


def resolve_weights_path() -> Path:
    root = Path(__file__).resolve().parent
    candidates = [
        root / "models" / _PREFERRED_WEIGHTS,
        root.parent / "csgobot" / "yolov8" / _PREFERRED_WEIGHTS,
        root.parent / "csgobot-main" / "yolov8" / _PREFERRED_WEIGHTS,
    ]
    for path in candidates:
        if not path.is_file():
            continue
        if _is_git_lfs_pointer(path):
            _LOG.warning(
                "Пропуск (Git LFS-заглушка, не веса): %s — скачай реальный .pt см. README/ниже.",
                path,
            )
            continue
        return path
    csgobot_yolov8 = root.parent / "csgobot" / "yolov8"
    raise FileNotFoundError(
        f"Нет реального файла весов {_PREFERRED_WEIGHTS} (не LFS-указатель).\n\n"
        "В репозитории на GitHub часто лежат только LFS-заглушки — нужны настоящие мегабайты модели.\n"
        "Варианты:\n"
        "  1) В клоне csgobot: git lfs install && git lfs pull\n"
        f"  2) Положить скачанный чекпоинт в: {root / 'models' / _PREFERRED_WEIGHTS}\n"
        f"  3) Источник весов — у автора (см. README Priler/csgobot)\n\n"
        f"Папка с шаблонами весов: {csgobot_yolov8}",
    )


def bgra_to_bgr(img: np.ndarray) -> np.ndarray:
    if img is None or img.size == 0:
        return img
    if len(img.shape) == 3 and img.shape[2] == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return img


def _primary_screen_wh() -> tuple[int, int]:
    import mss

    with mss.mss() as sct:
        mon = sct.monitors[1]
        return int(mon["width"]), int(mon["height"])


def layout_preview(
    cap: CaptureRegion,
    cfg_max_w: int,
    cfg_max_h: int,
) -> tuple[tuple[int, int], int, int]:
    """
    (x, y) окна предпросмотра и максимальные ширина/высота для scale_for_display.

    На 1080p под окном игры часто нет места под 720p — раньше срабатывал запасной
    (0,0) и снова возникало пересечение с регионом захвата («дубликаты»).
    """
    sw, sh = _primary_screen_wh()
    margin = 12
    L, T, W, H = cap.left, cap.top, cap.width, cap.height
    right_excl = L + W
    bottom_excl = T + H

    left_max_w = L - 2 * margin
    left_max_h = sh - 2 * margin
    below_max_h = sh - bottom_excl - margin
    above_max_h = T - 2 * margin
    min_read_w = 360
    min_read_h = 160

    # Колонка слева от клиентской области CS2 (типично 600+ px на 1080p)
    if left_max_w >= min_read_w:
        eff_w = min(cfg_max_w, left_max_w)
        eff_h = min(cfg_max_h, left_max_h)
        return (margin, T), eff_w, eff_h

    # Полоса под окном игры
    if below_max_h >= min_read_h:
        eff_w = min(cfg_max_w, W, sw - L - margin)
        eff_h = min(cfg_max_h, below_max_h)
        return (L, bottom_excl + margin), eff_w, eff_h

    # Полоса над окном
    if above_max_h >= min_read_h:
        eff_w = min(cfg_max_w, W, sw - L - margin)
        eff_h = min(cfg_max_h, above_max_h)
        y = T - eff_h - margin
        return (L, max(margin, y)), eff_w, eff_h

    # Справа от игры
    right_x = right_excl + margin
    if right_x + min_read_w <= sw - margin:
        eff_w = min(cfg_max_w, sw - right_x - margin)
        eff_h = min(cfg_max_h, sh - 2 * margin)
        return (right_x, T), eff_w, eff_h

    eff_w = max(280, min(cfg_max_w, max(left_max_w, 280)))
    eff_h = min(cfg_max_h, max(200, left_max_h))
    return (margin, margin), eff_w, eff_h


def _rects_overlap(
    ax: int,
    ay: int,
    aw: int,
    ah: int,
    bx: int,
    by: int,
    bw: int,
    bh: int,
) -> bool:
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by


def scale_for_display(
    img: np.ndarray,
    max_w: int,
    max_h: int,
) -> np.ndarray:
    h, w = img.shape[:2]
    scale = min(max_w / w, max_h / h, 1.0)
    if scale >= 1.0:
        return img
    return cv2.resize(
        img,
        (int(w * scale), int(h * scale)),
        interpolation=cv2.INTER_AREA,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    set_dpi_aware()

    import argparse
    parser = argparse.ArgumentParser(description="CS2 YOLO aim assist")
    parser.add_argument(
        "--team", choices=["T", "CT"], default="CT",
        help="Враги: T (террористы) или CT (контртеррористы). По умолчанию CT.",
    )
    args = parser.parse_args()

    weights = resolve_weights_path()
    cfg = DemoConfig(weights_path=str(weights))

    # Применяем команду из аргумента
    from config import Team as _Team
    cfg.aim_select.current_team = _Team.T if args.team == "T" else _Team.CT
    _LOG.info("Стартовая команда врагов: %s  (Ctrl+T — сменить)", args.team)

    try:
        rect = get_window_rect(cfg.window_title, cfg.border_offsets)
        cfg.capture_region = CaptureRegion(
            left=rect[0],
            top=rect[1],
            width=rect[2],
            height=rect[3],
        )
        cfg.capture_region = adjust_region_to_multiple(cfg.capture_region, 32)
        _LOG.info("Регион захвата: %s", cfg.capture_region)
    except Exception as e:
        _LOG.warning("Окно «%s» не найдено (%s). Используется регион по умолчанию.", cfg.window_title, e)

    detector = YOLOv8Detector(
        class_names=cfg.class_names,
        weights_path=cfg.weights_path,
        confidence_threshold=cfg.confidence_threshold,
        iou_threshold=cfg.iou_threshold,
    )
    detector.set_colors(cfg.class_colors)

    grabber = get_grabber("mss")
    area = cfg.capture_region.to_dict()

    aimer = LogicalScreenAim(
        cfg.capture_region,
        cfg.fov_mouse,
        cfg.logical_aim,
        crosshair_offset_x=cfg.aim_runtime.crosshair_offset_x,
        crosshair_offset_y=cfg.aim_runtime.crosshair_offset_y,
        invert_mouse_x=cfg.aim_runtime.invert_mouse_x,
        invert_mouse_y=cfg.aim_runtime.invert_mouse_y,
    )
    target_selector = TargetSelector(cfg.aim_select, cfg.capture_region)
    mouse = get_mouse()

    aim_state: dict[str, bool] = {"on": cfg.aim_runtime.aim_enabled_by_default}
    mouse_acc_x = 0.0
    mouse_acc_y = 0.0

    def _toggle_aim() -> None:
        aim_state["on"] = not aim_state["on"]
        _LOG.info("Прицеливание: %s", "ВКЛ" if aim_state["on"] else "ВЫКЛ")

    def _toggle_team() -> None:
        cfg.aim_select.current_team = (
            Team.T if cfg.aim_select.current_team == Team.CT else Team.CT
        )
        _LOG.info("Команда (враги): %s", cfg.aim_select.current_team.value.upper())

    keyboard.add_hotkey(cfg.aim_runtime.activation_hotkey_scan_code, _toggle_aim)
    keyboard.add_hotkey(cfg.aim_runtime.team_toggle_hotkey, _toggle_team)

    _LOG.info("Веса: %s", cfg.weights_path)
    _LOG.info(
        "Наводка: %s | auto_shoot: %s | Caps — пауза | Ctrl+T — CT/T | Q — выход",
        "ВКЛ" if aim_state["on"] else "ВЫКЛ",
        "да" if cfg.aim_runtime.auto_shoot else "нет",
    )

    (pv_x, pv_y), p_max_w, p_max_h = layout_preview(
        cfg.capture_region,
        cfg.viewer.max_display_size[0],
        cfg.viewer.max_display_size[1],
    )
    _LOG.info(
        "Предпросмотр вне зоны захвата: угол (%d, %d), макс. размер %d×%d",
        pv_x,
        pv_y,
        p_max_w,
        p_max_h,
    )

    cv2.namedWindow(cfg.viewer.title, cv2.WINDOW_NORMAL)
    cv2.moveWindow(cfg.viewer.title, pv_x, pv_y)

    frame_i = 0
    last_auto_shot = 0.0
    _NAV_PAUSE_FLAG = Path(__file__).parent / "nav_pause.flag"
    try:
        while True:
            raw = grabber.get_image(area)
            if raw is None:
                continue

            frame = bgra_to_bgr(np.asarray(raw))
            if frame is None or frame.size == 0:
                continue

            vis = frame.copy()
            detections = detector.detect(vis, verbose=False)
            detector.draw_boxes(vis, detections)

            target = None
            if aim_state["on"] and detections:
                target = target_selector.select_best_target(
                    detections,
                    max_distance=cfg.aim_runtime.max_assist_distance_capture_px,
                )
                if target is not None:
                    ar = (
                        aimer.get_move_clamped_step(
                            target.aim_x,
                            target.aim_y,
                            smoothing=cfg.aim_runtime.smoothing_factor,
                            max_step_logical=cfg.aim_runtime.closed_loop_max_pixel_logical,
                        )
                        if cfg.aim_runtime.use_closed_loop
                        else aimer.get_move(
                            target.aim_x,
                            target.aim_y,
                            smoothing=cfg.aim_runtime.smoothing_factor,
                        )
                    )
                    g = cfg.aim_runtime.aim_mouse_gain
                    if cfg.aim_runtime.use_subpixel_mouse_accumulator:
                        mouse_acc_x += ar.mouse_x * g
                        mouse_acc_y += ar.mouse_y * g
                        imx = int(round(mouse_acc_x))
                        imy = int(round(mouse_acc_y))
                        mouse_acc_x -= imx
                        mouse_acc_y -= imy
                        if imx != 0 or imy != 0:
                            mouse.move_relative(imx, imy)
                    elif ar.pixel_distance > cfg.aim_runtime.dead_zone_logical:
                        mouse.move_relative(
                            int(round(ar.mouse_x * g)),
                            int(round(ar.mouse_y * g)),
                        )
                    rt = cfg.aim_runtime
                    if rt.auto_shoot:
                        ok_conf = (
                            (target.is_head and target.confidence >= rt.head_confidence)
                            or (
                                not target.is_head
                                and target.confidence >= rt.body_confidence
                            )
                        )
                        if (
                            ok_conf
                            and ar.pixel_distance <= rt.auto_shoot_pixel_tolerance
                        ):
                            now = time.time()
                            if now - last_auto_shot >= rt.auto_shoot_cooldown_s:
                                mouse.click("left")
                                last_auto_shot = now
                    detector.draw_aim_point(
                        vis,
                        target.aim_x,
                        target.aim_y,
                        color=(0, 255, 0),
                    )
                else:
                    mouse_acc_x = 0.0
                    mouse_acc_y = 0.0
            else:
                mouse_acc_x = 0.0
                mouse_acc_y = 0.0

            # Синхронизация с nav_demo.py: пауза навигации при наличии цели
            if target is not None:
                _NAV_PAUSE_FLAG.touch()
            else:
                _NAV_PAUSE_FLAG.unlink(missing_ok=True)

            display = scale_for_display(vis, p_max_w, p_max_h)
            dh, dw = display.shape[:2]
            if frame_i == 0:
                cr = cfg.capture_region
                if _rects_overlap(pv_x, pv_y, dw, dh, cr.left, cr.top, cr.width, cr.height):
                    _LOG.warning(
                        "Окно предпросмотра пересекается с регионом захвата — возможны «дубликаты» "
                        "в картинке. Перетяни окно мышью или выстави масштаб Windows 100%% для проверки.",
                    )
                nobj = sum(len(v) for v in detections.values())
                _LOG.info("Первый кадр: объектов после NMS=%d (классы: %s)", nobj, list(detections))

            cv2.imshow(cfg.viewer.title, display)
            frame_i += 1
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        grabber.cleanup()
        cv2.destroyAllWindows()
        _NAV_PAUSE_FLAG.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FileNotFoundError as err:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        logging.getLogger("cs2_detection_demo").error("%s", err)
        sys.exit(1)
