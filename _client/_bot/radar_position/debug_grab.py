"""Диагностика захвата радара.

Запуск (пока CS2 запущен):
    python -m radar_position.debug_grab

Показывает:
  - Найдено ли окно CS2
  - Какой регион захватывается на экране
  - Живое окно с кадром радара и HSV-маской точки
  - Q — выход
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar_position.config import RadarConfig
from radar_position.radar_grabber import RadarGrabber, _get_cs2_window_offset
from radar_position.calibration.calibrator import find_player_dot_hsv


def main() -> None:
    cfg     = RadarConfig()
    grabber = RadarGrabber(cfg)
    grabber.open()

    # --- Показываем что нашли ---
    offset = _get_cs2_window_offset()
    if offset:
        print(f"[OK] Окно CS2 найдено: content offset = {offset}")
    else:
        print("[!] Окно CS2 НЕ найдено — CS2 запущен? Используем абс. координаты.")

    region = grabber.abs_region
    print(f"[OK] Захват: left={region['left']}  top={region['top']}  "
          f"width={region['width']}  height={region['height']}")
    print("Нажми Q чтобы выйти")

    while True:
        frame = grabber.grab_bgr()
        if frame is None:
            print("[!] Не удалось захватить кадр")
            break

        # HSV-маска точки
        hsv  = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(
            hsv,
            np.array(cfg.hsv_lower, dtype=np.uint8),
            np.array(cfg.hsv_upper, dtype=np.uint8),
        )

        # Найти точку
        dot = find_player_dot_hsv(
            frame, cfg.hsv_lower, cfg.hsv_upper,
            cfg.dot_min_area, cfg.dot_max_area,
        )

        vis = frame.copy()
        if dot:
            cx, cy = int(dot[0]), int(dot[1])
            cv2.circle(vis, (cx, cy), 6, (0, 255, 0), 2)
            cv2.putText(vis, f"dot ({cx},{cy})", (cx + 8, cy),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
        else:
            cv2.putText(vis, "dot NOT FOUND", (5, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

        cv2.putText(vis, f"left={region['left']} top={region['top']}", (5, 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1)

        # Показываем кадр и маску рядом
        mask_bgr = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
        combined = np.hstack([vis, mask_bgr])
        cv2.imshow("Radar (left) | HSV mask (right)  [Q=quit]", combined)

        if cv2.waitKey(30) & 0xFF == ord("q"):
            break

    grabber.close()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
