"""Визуализация navmesh поверх живого радара.

Запуск:
    python tools/navmesh_overlay.py --map de_poseidon
    python tools/navmesh_overlay.py --map de_poseidon --reader memory
    python tools/navmesh_overlay.py --map de_poseidon --reader radar --mode wingman

Управление:
    N — показать/скрыть номера узлов
    E — показать/скрыть рёбра
    Q — выход
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from radar_position.config import RadarConfig
from radar_position.coord_converter import CoordConverter
from radar_position.radar_grabber import RadarGrabber


def load_navmesh(map_name: str) -> tuple[list[dict], list[dict]]:
    path = ROOT / "navmesh" / f"{map_name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["nodes"], data["edges"]


def make_reader(reader_type: str, cfg: RadarConfig):
    if reader_type == "memory":
        from position.memory_reader import MemoryPositionReader
        r = MemoryPositionReader()
        r.attach()
        return r
    else:
        from radar_position import RadarPositionReader
        r = RadarPositionReader(cfg=cfg)
        r.attach()
        return r


def main() -> None:
    parser = argparse.ArgumentParser(description="Navmesh overlay on radar")
    parser.add_argument("--map",    default="de_poseidon")
    parser.add_argument("--reader", default="memory", choices=["memory", "radar"])
    parser.add_argument("--mode",   default="wingman", choices=["competitive", "wingman"])
    parser.add_argument("--scale",  type=float, default=2.5,
                        help="Масштаб окна (дефолт 2.5 = 295→738 px)")
    args = parser.parse_args()

    # --- Загрузка данных ---
    print(f"Загрузка navmesh: {args.map}...")
    nodes, edges = load_navmesh(args.map)
    node_by_id = {n["id"]: n for n in nodes}

    cfg = RadarConfig(map_name=args.map, game_mode=args.mode)

    cal_path = cfg.calibration_path
    if not cal_path.is_file():
        print(f"[ERROR] Калибровка не найдена: {cal_path}")
        print(f"  Запусти: python -m radar_position.calibration.calibrator --map {args.map} --mode {args.mode}")
        sys.exit(1)

    converter = CoordConverter.from_file(cal_path)
    grabber   = RadarGrabber(cfg)
    grabber.open()

    print(f"Подключение ридера ({args.reader})...")
    try:
        reader = make_reader(args.reader, cfg)
    except Exception as exc:
        grabber.close()
        print(f"[ERROR] {exc}")
        sys.exit(1)

    # Конвертируем все узлы в пиксели радара (один раз)
    node_px: dict[int, tuple[int, int]] = {}
    for n in nodes:
        try:
            px, py = converter.world_to_pixel(n["x"], n["y"])
            node_px[n["id"]] = (int(round(px)), int(round(py)))
        except Exception:
            pass

    SCALE    = args.scale
    WIN_NAME = f"Navmesh: {args.map}  (N=IDs, E=edges, Q=quit)"

    show_ids   = False
    show_edges = True

    # Цвета
    C_EDGE   = (60,  60,  60)   # тёмно-серый — рёбра
    C_NODE   = (0,   200, 80)   # зелёный — узел
    C_CORNER = (80,  80,  255)  # синий — угловой узел
    C_PLAYER = (0,   255, 255)  # жёлтый — игрок
    C_ENEMY  = (0,   0,   255)  # красный — враг (если reader поддерживает)
    C_NEAREST= (255, 140, 0)    # оранжевый — ближайший к игроку узел

    def _nearest_node(wx: float, wy: float) -> int | None:
        best_id, best_d = None, float("inf")
        for n in nodes:
            d = (n["x"] - wx) ** 2 + (n["y"] - wy) ** 2
            if d < best_d:
                best_d = d
                best_id = n["id"]
        return best_id

    print(f"Узлов: {len(nodes)} | Рёбер: {len(edges)}")
    print("N — номера узлов  |  E — рёбра  |  Q — выход")

    try:
        while True:
            frame = grabber.grab_bgr()
            if frame is None:
                time.sleep(0.03)
                continue

            h, w = frame.shape[:2]
            # Масштабируем кадр для удобного просмотра
            vis_w = int(w * SCALE)
            vis_h = int(h * SCALE)
            vis = cv2.resize(frame, (vis_w, vis_h), interpolation=cv2.INTER_LINEAR)

            def sp(nid: int) -> tuple[int, int] | None:
                """Пиксель узла в масштабированном окне."""
                p = node_px.get(nid)
                if p is None:
                    return None
                return (int(p[0] * SCALE), int(p[1] * SCALE))

            # --- Рёбра ---
            if show_edges:
                for e in edges:
                    a = sp(e["from"])
                    b = sp(e["to"])
                    if a and b:
                        cv2.line(vis, a, b, C_EDGE, 1, cv2.LINE_AA)

            # --- Узлы ---
            for n in nodes:
                p = sp(n["id"])
                if p is None:
                    continue
                color  = C_CORNER if n.get("corner") else C_NODE
                radius = 3
                cv2.circle(vis, p, radius, color, -1, cv2.LINE_AA)
                if show_ids:
                    cv2.putText(vis, str(n["id"]), (p[0] + 4, p[1] - 2),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.28, (220, 220, 220), 1)

            # --- Позиция игрока ---
            valid, px_w, py_w, _, _ = reader.snapshot()
            if valid:
                try:
                    rx, ry = converter.world_to_pixel(px_w, py_w)
                    sx, sy = int(rx * SCALE), int(ry * SCALE)
                    cv2.circle(vis, (sx, sy), 7, C_PLAYER, -1, cv2.LINE_AA)
                    cv2.circle(vis, (sx, sy), 8, (0, 0, 0), 1, cv2.LINE_AA)

                    # Ближайший узел
                    nid = _nearest_node(px_w, py_w)
                    if nid is not None:
                        np_ = sp(nid)
                        if np_:
                            cv2.circle(vis, np_, 6, C_NEAREST, 2, cv2.LINE_AA)

                    cv2.putText(vis, f"({px_w:.0f}, {py_w:.0f})",
                                (5, vis_h - 8), cv2.FONT_HERSHEY_SIMPLEX,
                                0.38, C_PLAYER, 1)
                except Exception:
                    pass

            # --- Враги (если RadarPositionReader) ---
            if hasattr(reader, "enemies"):
                for ewx, ewy in reader.enemies:
                    try:
                        ex, ey = converter.world_to_pixel(ewx, ewy)
                        esx, esy = int(ex * SCALE), int(ey * SCALE)
                        cv2.drawMarker(vis, (esx, esy), C_ENEMY,
                                       cv2.MARKER_DIAMOND, 10, 2, cv2.LINE_AA)
                    except Exception:
                        pass

            # --- Легенда ---
            cv2.circle(vis, (10, 12), 4, C_NODE,    -1)
            cv2.putText(vis, "узел",   (18, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.35, C_NODE,    1)
            cv2.circle(vis, (10, 26), 4, C_CORNER,  -1)
            cv2.putText(vis, "угол",   (18, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.35, C_CORNER,  1)
            cv2.circle(vis, (10, 40), 5, C_PLAYER,  -1)
            cv2.putText(vis, "игрок",  (18, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.35, C_PLAYER,  1)
            cv2.circle(vis, (10, 54), 5, C_NEAREST, 1, 2)
            cv2.putText(vis, "ближний",(18, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.35, C_NEAREST, 1)

            cv2.imshow(WIN_NAME, vis)
            key = cv2.waitKey(30) & 0xFF
            if key == ord("q"):
                break
            if key == ord("n"):
                show_ids = not show_ids
            if key == ord("e"):
                show_edges = not show_edges

    finally:
        reader.detach()
        grabber.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
