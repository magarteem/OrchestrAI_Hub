"""Проверка точности координат RadarPositionReader.

Запуск:
    python test_radar_pos.py --map de_poseidon

Выводит координаты каждую секунду.
Сравни с командой в консоли CS2:  getpos
"""

from __future__ import annotations

import argparse
import time
import sys

from radar_position import RadarPositionReader, RadarConfig


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", default="de_poseidon")
    parser.add_argument("--mode", default="wingman", choices=["competitive", "wingman"])
    args = parser.parse_args()

    cfg = RadarConfig(map_name=args.map, game_mode=args.mode)
    reader = RadarPositionReader(cfg=cfg)

    print(f"Загрузка модели для {args.map}...")
    try:
        reader.attach()
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)

    print("Готово! Координаты обновляются каждую секунду.")
    print("В консоли CS2 введи:  getpos")
    print("Сравни значения. Q — выход.\n")
    print(f"{'Radar X':>10}  {'Radar Y':>10}  {'Enemies':>8}  {'Valid':>6}")
    print("-" * 45)

    try:
        while True:
            valid, x, y, z, yaw = reader.snapshot()
            enemies = reader.enemies

            if valid:
                enemy_str = str(len(enemies)) if enemies else "0"
                print(f"{x:>10.1f}  {y:>10.1f}  {enemy_str:>8}  {'OK':>6}", end="\r")
            else:
                print(f"{'---':>10}  {'---':>10}  {'---':>8}  {'NO':>6}", end="\r")

            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\nВыход.")
    finally:
        reader.detach()


if __name__ == "__main__":
    main()
