"""Отладка HSV цветов на радаре.

Запуск:
    python -m radar_position.debug_hsv

Управление:
    - Наведи курсор на свой значок — видишь HSV в реальном времени
    - Кликни левой кнопкой — зафиксирует HSV в точке клика
    - S — сохранить скриншот радара в radar_debug.png
    - Q — выход
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar_position.config import RadarConfig, HSV_PROFILES
from radar_position.radar_grabber import RadarGrabber

_hover_pos: tuple[int, int] = (0, 0)
_clicked: list[tuple[int, int]] = []


def _on_mouse(event, x, y, flags, param) -> None:
    global _hover_pos
    _hover_pos = (x, y)
    if event == cv2.EVENT_LBUTTONDOWN:
        _clicked.append((x, y))


def _apply_hsv_mask(frame: np.ndarray, lower: tuple, upper: tuple,
                    lower2: tuple | None = None, upper2: tuple | None = None) -> np.ndarray:
    hsv = cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array(lower, np.uint8), np.array(upper, np.uint8))
    if lower2 and upper2:
        mask2 = cv2.inRange(hsv, np.array(lower2, np.uint8), np.array(upper2, np.uint8))
        mask = cv2.bitwise_or(mask, mask2)
    return mask


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="wingman", choices=["competitive", "wingman"])
    args = parser.parse_args()

    cfg = RadarConfig(game_mode=args.mode)
    grabber = RadarGrabber(cfg)
    grabber.open()

    win = "HSV Debug (Q=quit, S=save, M=mask, click=print HSV)"
    cv2.namedWindow(win)
    cv2.setMouseCallback(win, _on_mouse)

    show_mask = False

    print("=" * 60)
    print(f"Режим: {args.mode}")
    print(f"HSV player_me: lower={cfg.hsv_lower}  upper={cfg.hsv_upper}")
    if cfg.hsv_lower2:
        print(f"HSV lower2={cfg.hsv_lower2}  upper2={cfg.hsv_upper2}")
    print("Наведи мышь на свой значок — видишь HSV в реальном времени.")
    print("Кликни по значку — запишет точные HSV в консоль.")
    print("M — показать/скрыть маску текущего HSV-диапазона.")
    print("Q — выход,  S — сохранить скриншот radar_debug.png")
    print("=" * 60)

    last_clicked_count = 0

    while True:
        frame = grabber.grab_bgr()
        if frame is None:
            continue

        hsv_frame = cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2HSV)
        h_fr, w_fr = frame.shape[:2]

        if show_mask:
            mask = _apply_hsv_mask(frame, cfg.hsv_lower, cfg.hsv_upper,
                                   cfg.hsv_lower2, cfg.hsv_upper2)
            vis = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
            # Накладываем маску поверх оригинала
            vis = cv2.addWeighted(frame, 0.4, vis, 0.6, 0)
        else:
            vis = frame.copy()

        # HSV в позиции курсора
        mx, my = _hover_pos
        mx = max(0, min(mx, w_fr - 1))
        my = max(0, min(my, h_fr - 1))
        h_val, s_val, v_val = hsv_frame[my, mx]
        b_val, g_val, r_val = frame[my, mx, :3]

        # Рисуем перекрестие
        cv2.line(vis, (mx, 0), (mx, h_fr), (200, 200, 200), 1)
        cv2.line(vis, (0, my), (w_fr, my), (200, 200, 200), 1)
        cv2.circle(vis, (mx, my), 4, (0, 255, 255), 1)

        # Текст HSV
        info = f"H={h_val} S={s_val} V={v_val}"
        rgb_info = f"R={r_val} G={g_val} B={b_val}"
        cv2.putText(vis, info,     (5, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
        cv2.putText(vis, rgb_info, (5, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1)
        cv2.putText(vis, f"px=({mx},{my})", (5, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (180, 180, 180), 1)

        # Кликнутые точки
        for i, (cx, cy) in enumerate(_clicked):
            cv2.circle(vis, (cx, cy), 5, (0, 255, 0), 2)

        # Вывод в консоль при новом клике
        if len(_clicked) > last_clicked_count:
            cx, cy = _clicked[-1]
            cx = max(0, min(cx, w_fr - 1))
            cy = max(0, min(cy, h_fr - 1))
            ch, cs, cv_ = hsv_frame[cy, cx]
            cb, cg, cr  = frame[cy, cx, :3]
            print(f"\n[Клик] px=({cx}, {cy})")
            print(f"  HSV: H={ch}  S={cs}  V={cv_}")
            print(f"  BGR: B={cb}  G={cg}  R={cr}")
            print(f"  → Диапазон HSV (lower/upper):")
            print(f"    hsv_lower = ({max(0, ch-10)}, {max(0, cs-40)}, {max(0, cv_-40)})")
            print(f"    hsv_upper = ({min(180, ch+10)}, 255, 255)")
            if ch <= 10 or ch >= 170:
                print(f"  ⚠ Красный/бордовый — нужен второй диапазон:")
                if ch <= 10:
                    print(f"    hsv_lower2 = ({max(0, 170)}, {max(0, cs-40)}, {max(0, cv_-40)})")
                    print(f"    hsv_upper2 = (180, 255, 255)")
                else:
                    print(f"    hsv_lower2 = (0, {max(0, cs-40)}, {max(0, cv_-40)})")
                    print(f"    hsv_upper2 = ({min(10, ch-170)}, 255, 255)")
            last_clicked_count = len(_clicked)

        mode_txt = "[MASK ON]" if show_mask else "[M = показать маску]"
        cv2.putText(vis, mode_txt, (5, h_fr - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, (100, 200, 100), 1)

        cv2.imshow(win, vis)
        key = cv2.waitKey(30) & 0xFF
        if key == ord("q"):
            break
        if key == ord("s"):
            cv2.imwrite("radar_debug.png", frame)
            print("Скриншот сохранён: radar_debug.png")
        if key == ord("m"):
            show_mask = not show_mask
            print(f"Маска {'включена' if show_mask else 'выключена'}")

    grabber.close()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
