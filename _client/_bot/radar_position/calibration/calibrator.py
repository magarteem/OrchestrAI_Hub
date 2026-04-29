"""Авто-калибровка: memory reader + HSV-детекция жёлтой точки на радаре.

Запуск:
    python -m radar_position.calibration.calibrator --map de_dust2
    python -m radar_position.calibration.calibrator --map de_dust2 --points 20

Алгоритм:
    1. Подключиться к cs2.exe через memory reader (получаем точные wx, wy)
    2. Захватить скриншот радара через MSS
    3. Найти жёлтую точку игрока через HSV color detection
    4. Получить пару (px, py) ↔ (wx, wy) с точностью до 1 пикселя
    5. Накопить N точек пока игрок ходит по карте
    6. Вычислить аффинное преобразование (least-squares)
    7. Сохранить в calibration/{map_name}.json
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np

# Добавляем корень cs2_player_detection_demo в путь
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from radar_position.config import RadarConfig
from radar_position.coord_converter import CoordConverter
from radar_position.radar_grabber import RadarGrabber
from position.memory_reader import MemoryPositionReader

_LOG = logging.getLogger("calibrator")


def find_player_dot_hsv(
    frame: np.ndarray,
    hsv_lower: tuple,
    hsv_upper: tuple,
    min_area: int,
    max_area: int,
    hsv_lower2: Optional[tuple] = None,
    hsv_upper2: Optional[tuple] = None,
) -> Optional[Tuple[float, float]]:
    """Находит центр значка игрока в кадре радара через HSV маску.

    Поддерживает два диапазона HSV — для бордового/красного цвета,
    который оборачивается через 0° (0–10 И 170–180).

    Args:
        frame: BGR изображение кропа радара
        hsv_lower / hsv_upper: основной диапазон HSV
        min_area / max_area: допустимая площадь пятна (фильтр шума)
        hsv_lower2 / hsv_upper2: второй диапазон (для бордового/красного)

    Returns:
        (cx, cy) в пикселях кропа или None если не найдено
    """
    # BGR → HSV
    hsv = cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(
        hsv,
        np.array(hsv_lower, dtype=np.uint8),
        np.array(hsv_upper, dtype=np.uint8),
    )
    if hsv_lower2 is not None and hsv_upper2 is not None:
        mask2 = cv2.inRange(
            hsv,
            np.array(hsv_lower2, dtype=np.uint8),
            np.array(hsv_upper2, dtype=np.uint8),
        )
        mask = cv2.bitwise_or(mask, mask2)

    # Небольшая морфология для устранения шума
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best: Optional[Tuple[float, float]] = None
    best_area = 0

    h, w = frame.shape[:2]
    edge_margin = 8  # отступ от краёв кадра (пиксели) — отсекает ложные срабатывания

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if min_area <= area <= max_area:
            M = cv2.moments(cnt)
            if M["m00"] > 0:
                cx = M["m10"] / M["m00"]
                cy = M["m01"] / M["m00"]
                # Отбрасываем детекции слишком близко к краю кадра
                if cx < edge_margin or cx > w - edge_margin:
                    continue
                if cy < edge_margin or cy > h - edge_margin:
                    continue
                if area > best_area:
                    best = (cx, cy)
                    best_area = area

    return best


def run_calibration(
    cfg: RadarConfig,
    target_points: int = 15,
    min_move_dist: float = 100.0,
    min_interval_sec: float = 10.0,
    preview: bool = True,
) -> CoordConverter:
    """Запускает процесс авто-калибровки.

    Args:
        cfg: конфигурация радара
        target_points: сколько точек собрать
        min_move_dist: минимальное расстояние (мировые ед.) между точками
        min_interval_sec: минимальная пауза в секундах между точками.
                          Точка добавляется только если прошло достаточно
                          времени И пройдено достаточно расстояния.
        preview: показывать окно OpenCV с радаром и найденной точкой

    Returns:
        CoordConverter с вычисленным преобразованием
    """
    _LOG.info("=== Авто-калибровка ===")
    _LOG.info(
        "Карта: %s | цель: %d точек | интервал: %.0f сек",
        cfg.map_name, target_points, min_interval_sec,
    )
    _LOG.info("Ходи по ВСЕЙ карте: T спавн → туннели → CT → A → B → середина")
    _LOG.info("Точка добавляется каждые %.0f секунд (при движении)", min_interval_sec)
    _LOG.info("Q — завершить и сохранить")

    # Подключение к CS2
    reader = MemoryPositionReader()
    try:
        reader.attach()
    except (ImportError, RuntimeError) as exc:
        _LOG.error("Не удалось подключиться к cs2.exe: %s", exc)
        sys.exit(1)

    converter = CoordConverter()
    grabber   = RadarGrabber(cfg)
    grabber.open()

    last_wx:   Optional[float] = None
    last_wy:   Optional[float] = None
    last_time: float = 0.0  # время последней записанной точки

    try:
        while converter.point_count < target_points:
            # --- Снимаем позицию из памяти ---
            valid, wx_raw, wy_raw, _, _ = reader.snapshot()
            if not valid:
                time.sleep(0.05)
                continue

            # --- Снимаем радар (через RadarGrabber — учитывает позицию окна) ---
            frame = grabber.grab_bgr()
            if frame is None:
                time.sleep(0.05)
                continue

            # --- HSV детекция точки ---
            dot = find_player_dot_hsv(
                frame,
                cfg.hsv_lower,
                cfg.hsv_upper,
                cfg.dot_min_area,
                cfg.dot_max_area,
                hsv_lower2=cfg.hsv_lower2,
                hsv_upper2=cfg.hsv_upper2,
            )

            now = time.perf_counter()
            elapsed_since_last = now - last_time
            moved_dist = 0.0
            if last_wx is not None:
                moved_dist = ((wx_raw - last_wx) ** 2 + (wy_raw - last_wy) ** 2) ** 0.5

            time_ok = elapsed_since_last >= min_interval_sec
            move_ok = last_wx is None or moved_dist >= min_move_dist
            ready   = time_ok and move_ok

            if preview:
                vis = frame.copy()
                if dot:
                    cv2.circle(vis, (int(dot[0]), int(dot[1])), 7, (0, 255, 0), 2)
                    # Показываем координаты детекции — если py почти не меняется, что-то не то
                    cv2.putText(vis, f"dot=({dot[0]:.0f},{dot[1]:.0f})",
                                (5, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 255, 0), 1)
                else:
                    cv2.putText(vis, "dot=None", (5, 58),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 0, 255), 1)

                pts_text = f"Points: {converter.point_count}/{target_points}"
                cv2.putText(vis, pts_text, (5, 15),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 0), 1)

                if ready and dot is not None:
                    status_text = "CAPTURING..."
                    color = (0, 255, 0)
                else:
                    wait_sec = max(0.0, min_interval_sec - elapsed_since_last)
                    status_text = f"Wait: {wait_sec:.0f}s | move: {moved_dist:.0f}"
                    color = (0, 165, 255)

                cv2.putText(vis, status_text, (5, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)

                cv2.imshow("Radar Calibration (Q=quit)", vis)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    _LOG.info("Завершение по Q")
                    break

            if dot is None:
                time.sleep(0.05)
                continue

            px, py = dot

            if not ready:
                time.sleep(0.05)
                continue

            converter.add_point(px, py, wx_raw, wy_raw)
            last_wx, last_wy = wx_raw, wy_raw
            last_time = time.perf_counter()

            _LOG.info(
                "[%2d/%d]  px=(%.1f, %.1f)  world=(%.0f, %.0f)",
                converter.point_count, target_points,
                px, py, wx_raw, wy_raw,
            )

            time.sleep(0.1)

    finally:
        reader.detach()
        grabber.close()
        if preview:
            cv2.destroyAllWindows()

    # --- Вычисляем и сохраняем ---
    if converter.point_count < 3:
        _LOG.error("Собрано меньше 3 точек (%d) — сохранение невозможно", converter.point_count)
        sys.exit(1)

    _LOG.info("Вычисляю аффинное преобразование по %d точкам...", converter.point_count)
    transform = converter.fit()
    _LOG.info(
        "Масштаб: %.2f ед/px (X)  %.2f ед/px (Y)",
        transform.scale_x, transform.scale_y,
    )

    converter.save(cfg.calibration_path)
    return converter


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Radar calibration tool")
    parser.add_argument("--map",    default="de_dust2", help="Название карты")
    parser.add_argument(
        "--mode", default="competitive",
        choices=["competitive", "wingman"],
        help="Режим игры: competitive (жёлтый значок) или wingman (бордовый). По умолч. competitive",
    )
    parser.add_argument("--points", type=int, default=20,
                        help="Кол-во точек калибровки (дефолт: 20)")
    parser.add_argument("--min-move", type=float, default=100.0,
                        help="Мин. перемещение (ед.) между точками (дефолт: 100)")
    parser.add_argument("--interval", type=float, default=8.0,
                        help="Мин. пауза в секундах между точками (дефолт: 8)")
    parser.add_argument("--no-preview", action="store_true", help="Без окна OpenCV")
    args = parser.parse_args()

    cfg = RadarConfig(map_name=args.map, game_mode=args.mode)
    _LOG.info(
        "Режим: %s | HSV player_me: lower=%s upper=%s",
        args.mode, cfg.hsv_lower, cfg.hsv_upper,
    )
    if cfg.hsv_lower2:
        _LOG.info("HSV второй диапазон: lower2=%s upper2=%s", cfg.hsv_lower2, cfg.hsv_upper2)

    run_calibration(
        cfg=cfg,
        target_points=args.points,
        min_move_dist=args.min_move,
        min_interval_sec=args.interval,
        preview=not args.no_preview,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
