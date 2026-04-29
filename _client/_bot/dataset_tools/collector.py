"""Авто-сбор датасета для обучения YOLOv12 (2 класса: player_me, enemy).

Запуск:
    # Competitive (жёлтый значок):
    python -m dataset_tools.collector --map de_dust2 --count 500

    # Wingman / с ботами (бордовый значок):
    python -m dataset_tools.collector --map de_poseidon --count 600 --mode wingman

Алгоритм:
    1. Подключается к cs2.exe через memory reader (gt-координаты игрока)
    2. Захватывает кадр радара через MSS
    3. Для player_me: конвертирует world → pixel (точная метка из памяти) → class 0
    4. Для enemy: HSV-детекция красных пятен на радаре → class 1
    5. Сохраняет frame_XXXXX.png + frame_XXXXX.txt (YOLO multi-label)

Формат аннотации (YOLO, одна строка на объект):
    <class_id> <cx> <cy> <w> <h>   (всё нормализовано 0–1)

Классы:
    0 = player_me  (бордовый/жёлтый значок игрока)
    1 = enemy      (красный ромб, красный "?")
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar_position.config import RadarConfig
from radar_position.coord_converter import CoordConverter
from radar_position.radar_grabber import RadarGrabber
from position.memory_reader import MemoryPositionReader

_LOG = logging.getLogger("collector")

# Размер bbox точки в долях от размера изображения (17px / 295px ≈ 0.058)
_DOT_SIZE_NORM: float = 0.058

# ID классов
CLASS_PLAYER_ME = 0
CLASS_ENEMY = 1


def _detect_blobs_hsv(
    frame_bgr: np.ndarray,
    hsv_lower: tuple,
    hsv_upper: tuple,
    min_area: int,
    max_area: int,
    hsv_lower2: Optional[tuple] = None,
    hsv_upper2: Optional[tuple] = None,
) -> list[tuple[float, float]]:
    """Находит все пятна заданного HSV-цвета в кадре.

    Возвращает список (cx, cy) в пикселях кадра.
    Поддерживает два диапазона (для бордового/красного, оборачивающегося через 0°).
    """
    hsv = cv2.cvtColor(frame_bgr[:, :, :3], cv2.COLOR_BGR2HSV)
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

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_DILATE, kernel)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    results: list[tuple[float, float]] = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area or area > max_area:
            continue
        M = cv2.moments(cnt)
        if M["m00"] == 0:
            continue
        cx = M["m10"] / M["m00"]
        cy = M["m01"] / M["m00"]
        results.append((cx, cy))
    return results


def _yolo_line(class_id: int, cx_norm: float, cy_norm: float,
               w_norm: float = _DOT_SIZE_NORM, h_norm: float = _DOT_SIZE_NORM) -> str:
    return f"{class_id} {cx_norm:.6f} {cy_norm:.6f} {w_norm:.6f} {h_norm:.6f}"


def collect(
    cfg: RadarConfig,
    out_dir: Path,
    count: int = 500,
    min_move: float = 300.0,
    preview: bool = True,
    capture_hz: float = 1.0,
) -> None:
    """Собирает датасет с мульти-классовой разметкой.

    Args:
        cfg:        конфигурация радара (game_mode определяет HSV-профиль)
        out_dir:    корень датасета (создаст images/ и labels/ внутри)
        count:      сколько кадров собрать
        min_move:   мин. перемещение (мировые ед.) между снимками
        preview:    показывать окно предпросмотра
        capture_hz: частота захвата кадров
    """
    img_dir = out_dir / "images"
    lbl_dir = out_dir / "labels"
    img_dir.mkdir(parents=True, exist_ok=True)
    lbl_dir.mkdir(parents=True, exist_ok=True)

    # Калибровка
    if not cfg.calibration_path.is_file():
        _LOG.error(
            "Калибровка не найдена: %s\n"
            "Сначала запусти: python -m radar_position.calibration.calibrator --map %s",
            cfg.calibration_path, cfg.map_name,
        )
        sys.exit(1)

    converter = CoordConverter.from_file(cfg.calibration_path)
    grabber = RadarGrabber(cfg)
    grabber.open()

    reader = MemoryPositionReader()
    try:
        reader.attach()
    except (ImportError, RuntimeError) as exc:
        grabber.close()
        _LOG.error("Не удалось подключиться к cs2.exe: %s", exc)
        sys.exit(1)

    _LOG.info(
        "Сбор датасета: карта=%s | режим=%s | цель=%d кадров | выход=%s",
        cfg.map_name, cfg.game_mode, count, out_dir,
    )
    _LOG.info("Классы: 0=player_me (бордовый/жёлтый)  1=enemy (красный ромб/знак вопроса)")
    _LOG.info("Ходи по ВСЕЙ карте — враги будут размечаться автоматически при появлении на радаре")

    saved = 0
    last_wx: Optional[float] = None
    last_wy: Optional[float] = None
    interval = 1.0 / capture_hz

    # Цвета для превью
    _COLOR_ME    = (0, 255, 0)    # зелёный — player_me
    _COLOR_ENEMY = (0, 0, 255)    # красный — enemy

    try:
        while saved < count:
            t0 = time.perf_counter()

            valid, wx, wy, _, _ = reader.snapshot()
            if not valid:
                time.sleep(0.05)
                continue

            frame = grabber.grab_bgr()
            if frame is None:
                time.sleep(0.05)
                continue

            h_px, w_px = frame.shape[:2]

            # --- Расстояние от последней сохранённой позиции ---
            cur_dist = 0.0
            if last_wx is not None:
                cur_dist = ((wx - last_wx) ** 2 + (wy - last_wy) ** 2) ** 0.5
            ready = last_wx is None or cur_dist >= min_move

            # --- Формируем аннотации ---
            lines: list[str] = []

            # class 0: player_me — из памяти (точная позиция)
            try:
                px_me, py_me = converter.world_to_pixel(wx, wy)
                if 0 <= px_me < w_px and 0 <= py_me < h_px:
                    lines.append(_yolo_line(
                        CLASS_PLAYER_ME,
                        px_me / w_px,
                        py_me / h_px,
                    ))
            except Exception:
                pass

            # class 1: enemy — HSV-детекция красных маркеров
            enemy_dots = _detect_blobs_hsv(
                frame,
                cfg.enemy_hsv_lower,
                cfg.enemy_hsv_upper,
                cfg.dot_min_area,
                cfg.dot_max_area,
                hsv_lower2=cfg.enemy_hsv_lower2,
                hsv_upper2=cfg.enemy_hsv_upper2,
            )
            for ex, ey in enemy_dots:
                lines.append(_yolo_line(CLASS_ENEMY, ex / w_px, ey / h_px))

            # --- Превью (всегда, не только при сохранении) ---
            if preview:
                vis = frame.copy()

                # Рисуем player_me
                if lines and lines[0].startswith("0 "):
                    cv2.circle(vis, (int(px_me), int(py_me)), 8, _COLOR_ME, 2)

                # Рисуем enemy
                for ex, ey in enemy_dots:
                    cv2.rectangle(
                        vis,
                        (int(ex) - 8, int(ey) - 8),
                        (int(ex) + 8, int(ey) + 8),
                        _COLOR_ENEMY, 2,
                    )

                ready_text = "SAVING..." if ready else f"Move: {min_move - cur_dist:.0f}"
                color_txt  = _COLOR_ME if ready else (0, 165, 255)

                cv2.putText(vis, f"{saved}/{count}", (5, 14),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 0), 1)
                cv2.putText(vis, ready_text, (5, 28),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.42, color_txt, 1)
                cv2.putText(vis, f"enemies={len(enemy_dots)}", (5, 42),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.38, _COLOR_ENEMY, 1)

                cv2.imshow("Dataset Collector — Q=quit", vis)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    _LOG.info("Прерывание по Q")
                    break

            if not ready:
                time.sleep(0.02)
                continue

            # Пропускаем кадры без player_me (позиция не прочиталась)
            if not any(ln.startswith("0 ") for ln in lines):
                last_wx, last_wy = wx, wy
                continue

            # --- Сохраняем ---
            name = f"frame_{saved:05d}"
            cv2.imwrite(str(img_dir / f"{name}.png"), frame)
            (lbl_dir / f"{name}.txt").write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )

            saved += 1
            last_wx, last_wy = wx, wy

            if saved % 50 == 0:
                _LOG.info("Собрано: %d / %d кадров", saved, count)

            elapsed = time.perf_counter() - t0
            sleep = interval - elapsed
            if sleep > 0:
                time.sleep(sleep)

    finally:
        reader.detach()
        grabber.close()
        if preview:
            cv2.destroyAllWindows()

    _LOG.info("Готово! Сохранено %d кадров в %s", saved, out_dir)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="CS2 radar multi-class dataset collector")
    parser.add_argument("--map",      default="de_dust2", help="Название карты")
    parser.add_argument("--count",    type=int, default=500, help="Кол-во кадров")
    parser.add_argument(
        "--mode", default="competitive",
        choices=["competitive", "wingman"],
        help="Режим игры: competitive (жёлтый значок) или wingman (бордовый). По умолч. competitive",
    )
    parser.add_argument(
        "--out", default=None,
        help="Папка датасета (по умолч. dataset_tools/dataset/{map_name})",
    )
    parser.add_argument(
        "--min-move", type=float, default=300.0,
        help="Мин. перемещение (ед.) между снимками (дефолт: 300)",
    )
    parser.add_argument("--hz",        type=float, default=1.0, help="Частота захвата кадров/сек")
    parser.add_argument("--no-preview", action="store_true")
    args = parser.parse_args()

    cfg = RadarConfig(map_name=args.map, game_mode=args.mode)

    out_dir = Path(
        args.out if args.out
        else Path(__file__).parent / "dataset" / args.map
    )

    collect(
        cfg=cfg,
        out_dir=out_dir,
        count=args.count,
        min_move=args.min_move,
        preview=not args.no_preview,
        capture_hz=args.hz,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
