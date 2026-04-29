"""Интерактивный редактор navmesh поверх радара.

Запуск:
    python tools/navmesh_editor.py --map de_poseidon [--scale 4.0] [--mode wingman]

═══════════════════════════════════════════════════════
РЕЖИМЫ  (переключать клавишами M / C / G / L  или  ESC)
═══════════════════════════════════════════════════════
    M — MOVE    — выбрать, перетащить, удалить узел  (режим по умолчанию)
    C — CREATE  — ЛКМ на пустом месте создаёт новый узел
    G — EDGE    — клик на двух узлах добавляет ребро между ними
    L — LOOK    — назначение точки взгляда для направленного ребра A->B
  ESC — вернуться в MOVE из любого режима
        (в режиме EDGE первый ESC отменяет выбор узла A,
         второй ESC переключает в MOVE)

═══════════════════════════════════════════════════════
 МЫШЬ
═══════════════════════════════════════════════════════
  В режиме MOVE:
    ЛКМ на узле       — выбрать узел
    ЛКМ + перетащить  — переместить узел (вес рёбер пересчитается)
  В режиме CREATE:
    ЛКМ на пустом месте — создать узел
    ЛКМ на узле         — выбрать узел
  В режиме EDGE:
    ЛКМ на узле A → ЛКМ на узле B — добавить двустороннее ребро

  ПКМ на узле — удалить узел и все его рёбра  (работает в любом режиме)

═══════════════════════════════════════════════════════
 КЛАВИШИ
═══════════════════════════════════════════════════════
  D — удалить выбранный узел (или узел под курсором)  — любой режим
  A — назначить/сменить action выбранного узла (листать по кругу)
      в режиме LOOK — назначить fixed_yaw для выбранного A->B по курсору
  K — удалить look-настройку для выбранного A->B (режим LOOK)
  X — удалить ребро между выбранным узлом и узлом под курсором
  U — отменить последнее действие (Undo, до 100 шагов)
  S — сохранить изменения в navmesh/<map>.json
  N — показать / скрыть номера узлов
  E — показать / скрыть рёбра
  Q — выход (при несохранённых изменениях нажать Q ещё раз или S)

═══════════════════════════════════════════════════════
 ЗНАЧЕНИЯ ACTION  (назначить клавишей A)
═══════════════════════════════════════════════════════
    (пусто)     — обычное движение
    jump        — одиночный прыжок при достижении узла
    crouch      — приседать (зажать Ctrl) до следующего узла
    shift       — идти тихо (зажать Shift) до следующего узла
    look_north  — повернуть взгляд на север
    look_south  — повернуть взгляд на юг
    look_east   — повернуть взгляд на восток
    look_west   — повернуть взгляд на запад

═══════════════════════════════════════════════════════
 ЦВЕТА УЗЛОВ
═══════════════════════════════════════════════════════
    Зелёный    — обычный узел
    Синий      — corner (угловой)
    Оранжевый  — action=jump
    Синий тёмный — action=crouch
    Фиолетовый — action=shift
    Циан       — action=look_*
    Жёлтый     — выбранный узел
    Циан обводка — узел A в режиме EDGE
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
import time
from enum import Enum
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from radar_position.config import RadarConfig
from radar_position.coord_converter import CoordConverter
from radar_position.radar_grabber import RadarGrabber
from navigation.movement import NODE_ACTIONS

CLICK_RADIUS = 14
RANDOM_TARGET_MARKER = ROOT / "nav_random_target_id.txt"


class Mode(Enum):
    MOVE   = "MOVE"
    CREATE = "CREATE"
    EDGE   = "EDGE"
    LOOK   = "LOOK"


# Цвета узлов по action
_ACTION_COLORS: dict[str, tuple[int, int, int]] = {
    "jump":       (0, 165, 255),
    "crouch":     (255, 100,   0),
    "shift":      (200,   0, 200),
    "look_north": (0,  220, 220),
    "look_south": (0,  220, 220),
    "look_east":  (0,  220, 220),
    "look_west":  (0,  220, 220),
}

# Цвета режимов (BGR)
_MODE_COLORS: dict[Mode, tuple[int, int, int]] = {
    Mode.MOVE:   (60, 180,  60),
    Mode.CREATE: (40, 140, 255),
    Mode.EDGE:   (0,  200, 220),
    Mode.LOOK:   (255, 180, 60),
}

_MODE_HINTS: dict[Mode, list[str]] = {
    Mode.MOVE: [
        "M/C/G = режим  ESC = MOVE",
        "ЛКМ = выбрать/тащить",
        "ПКМ / D = удалить узел",
        "A = action  X = удалить ребро",
        "/ = найти узел по ID",
        "U = undo   S = сохранить",
        "N = id     E = рёбра",
    ],
    Mode.CREATE: [
        "M/C/G = режим  ESC = MOVE",
        "ЛКМ пусто = создать узел",
        "ЛКМ узел  = выбрать",
        "ПКМ / D   = удалить узел",
        "/ = найти узел по ID",
        "U = undo   S = сохранить",
    ],
    Mode.EDGE: [
        "M/C/G = режим  ESC = MOVE",
        "Клик узел A → узел B",
        "= двустороннее ребро",
        "ESC (дважды) = отмена A",
        "ПКМ / D = удалить узел",
        "/ = найти узел по ID",
        "U = undo   S = сохранить",
    ],
    Mode.LOOK: [
        "L = LOOK mode  ESC = MOVE",
        "Клик узел A → узел B",
        "ЛКМ в пустое место = look_point",
        "K = удалить look для A->B",
        "A = fixed_yaw по курсору",
        "U = undo   S = сохранить",
    ],
}


def load_navmesh(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_navmesh(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=None), encoding="utf-8")


def _edge_weight(ax: float, ay: float, bx: float, by: float) -> float:
    return round(math.hypot(bx - ax, by - ay), 4)


class NavmeshEditor:
    def __init__(self, navmesh_path: Path, converter: CoordConverter,
                 grabber: RadarGrabber, scale: float = 4.0) -> None:
        self._path      = navmesh_path
        self._converter = converter
        self._grabber   = grabber
        self._scale     = scale
        self._click_r   = max(10, int(scale * 3.5))

        self._data  = load_navmesh(navmesh_path)
        self._nodes = self._data["nodes"]
        self._edges = self._data["edges"]
        self._look_edges: dict[str, dict] = self._data.get("look_edges", {})
        if not isinstance(self._look_edges, dict):
            self._look_edges = {}
        self._data["look_edges"] = self._look_edges

        self._mode: Mode = Mode.MOVE

        self._selected:  int | None = None   # id выбранного узла (MOVE)
        self._edge_src:  int | None = None   # первый узел в режиме EDGE
        self._look_src:  int | None = None   # from в режиме LOOK
        self._look_dst:  int | None = None   # to в режиме LOOK
        self._hover_node: int | None = None  # узел под курсором мыши

        self._dragging = False
        self._history: list[tuple[list, list, dict]] = []
        self._modified = False

        self._show_ids   = False
        self._show_edges = True

        self._mouse_pos: tuple[int, int] = (0, 0)

        # Поиск узла по ID
        self._search_mode = False
        self._search_buf  = ""          # накопленные цифры
        self._found_id: int | None = None  # подсвеченный узел
        self._random_target_id: int | None = None
        self._random_target_mtime_ns: int | None = None

        self._win = f"Navmesh Editor — {navmesh_path.stem}"
        cv2.namedWindow(self._win, cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(self._win, self._on_mouse)

    # ------------------------------------------------------------------
    # История
    # ------------------------------------------------------------------
    def _push_history(self) -> None:
        self._history.append(
            (
                copy.deepcopy(self._nodes),
                copy.deepcopy(self._edges),
                copy.deepcopy(self._look_edges),
            )
        )
        if len(self._history) > 100:
            self._history.pop(0)

    def _undo(self) -> None:
        if not self._history:
            print("Нечего отменять")
            return
        self._nodes, self._edges, self._look_edges = self._history.pop()
        self._data["nodes"] = self._nodes
        self._data["edges"] = self._edges
        self._data["look_edges"] = self._look_edges
        self._selected = None
        self._edge_src = None
        self._look_src = None
        self._look_dst = None
        self._modified = True
        print("Отменено")

    def _save(self) -> None:
        self._data["nodes"] = self._nodes
        self._data["edges"] = self._edges
        self._data["look_edges"] = self._look_edges
        save_navmesh(self._path, self._data)
        self._modified = False
        print(f"Сохранено: {self._path.name}  ({len(self._nodes)} узлов, {len(self._edges)} рёбер)")

    # ------------------------------------------------------------------
    # Координатные утилиты
    # ------------------------------------------------------------------
    def _node_screen_pos(self, node: dict) -> tuple[int, int] | None:
        try:
            px, py = self._converter.world_to_pixel(node["x"], node["y"])
            return int(round(px * self._scale)), int(round(py * self._scale))
        except Exception:
            return None

    def _screen_to_world(self, sx: int, sy: int) -> tuple[float, float]:
        px, py = sx / self._scale, sy / self._scale
        return self._converter.pixel_to_world(px, py)

    def _find_nearest_node(self, sx: int, sy: int) -> int | None:
        best_id, best_d = None, self._click_r ** 2
        for n in self._nodes:
            p = self._node_screen_pos(n)
            if p is None:
                continue
            d = (p[0] - sx) ** 2 + (p[1] - sy) ** 2
            if d < best_d:
                best_d = d
                best_id = n["id"]
        return best_id

    # ------------------------------------------------------------------
    # Операции над данными
    # ------------------------------------------------------------------
    def _next_id(self) -> int:
        if not self._nodes:
            return 0
        return max(n["id"] for n in self._nodes) + 1

    def _add_node(self, sx: int, sy: int) -> int:
        self._push_history()
        wx, wy = self._screen_to_world(sx, sy)
        nid = self._next_id()
        node = {"id": nid, "x": round(wx, 2), "y": round(wy, 2), "z": 0.0,
                "corner": False, "action": ""}
        self._nodes.append(node)
        self._data["nodes"] = self._nodes
        self._modified = True
        print(f"Создан узел {nid}  ({wx:.0f}, {wy:.0f})")
        return nid

    def _delete_node(self, node_id: int) -> None:
        self._push_history()
        self._nodes = [n for n in self._nodes if n["id"] != node_id]
        self._edges = [e for e in self._edges
                       if e["from"] != node_id and e["to"] != node_id]
        keep: dict[str, dict] = {}
        for edge_key, payload in self._look_edges.items():
            parts = edge_key.split("->")
            if len(parts) != 2:
                continue
            try:
                src = int(parts[0])
                dst = int(parts[1])
            except ValueError:
                continue
            if src == node_id or dst == node_id:
                continue
            keep[edge_key] = payload
        self._look_edges = keep
        self._data["nodes"] = self._nodes
        self._data["edges"] = self._edges
        self._data["look_edges"] = self._look_edges
        if self._selected == node_id:
            self._selected = None
        if self._edge_src == node_id:
            self._edge_src = None
        if self._look_src == node_id:
            self._look_src = None
        if self._look_dst == node_id:
            self._look_dst = None
        self._modified = True
        print(f"Удалён узел {node_id}  (осталось {len(self._nodes)} узлов)")

    def _set_look_point(self, src_id: int, dst_id: int, sx: int, sy: int) -> None:
        self._push_history()
        wx, wy = self._screen_to_world(sx, sy)
        key = f"{src_id}->{dst_id}"
        self._look_edges[key] = {"mode": "look_point", "x": round(wx, 2), "y": round(wy, 2)}
        self._data["look_edges"] = self._look_edges
        self._modified = True
        print(f"LOOK {key}: look_point=({wx:.1f},{wy:.1f})")

    def _set_fixed_yaw_from_cursor(self, src_id: int, dst_id: int, sx: int, sy: int) -> None:
        src_node = next((n for n in self._nodes if n["id"] == src_id), None)
        if src_node is None:
            return
        wx, wy = self._screen_to_world(sx, sy)
        yaw = math.degrees(math.atan2(wy - src_node["y"], wx - src_node["x"]))
        self._push_history()
        key = f"{src_id}->{dst_id}"
        self._look_edges[key] = {"mode": "fixed_yaw", "yaw": round(yaw, 2)}
        self._data["look_edges"] = self._look_edges
        self._modified = True
        print(f"LOOK {key}: fixed_yaw={yaw:.1f}")

    def _clear_look_edge(self, src_id: int, dst_id: int) -> None:
        key = f"{src_id}->{dst_id}"
        if key not in self._look_edges:
            print(f"LOOK {key}: не задан")
            return
        self._push_history()
        self._look_edges.pop(key, None)
        self._data["look_edges"] = self._look_edges
        self._modified = True
        print(f"LOOK {key}: удален")

    def _move_node(self, node_id: int, sx: int, sy: int) -> None:
        wx, wy = self._screen_to_world(sx, sy)
        for n in self._nodes:
            if n["id"] == node_id:
                n["x"] = round(wx, 2)
                n["y"] = round(wy, 2)
                self._modified = True
                # пересчитать веса смежных рёбер
                for e in self._edges:
                    if e["from"] == node_id or e["to"] == node_id:
                        a = next((x for x in self._nodes if x["id"] == e["from"]), None)
                        b = next((x for x in self._nodes if x["id"] == e["to"]),   None)
                        if a and b:
                            e["weight"] = _edge_weight(a["x"], a["y"], b["x"], b["y"])
                break

    def _add_edge(self, id_a: int, id_b: int) -> None:
        if id_a == id_b:
            return
        exists = any(
            (e["from"] == id_a and e["to"] == id_b) or
            (e["from"] == id_b and e["to"] == id_a)
            for e in self._edges
        )
        if exists:
            print(f"Ребро {id_a}↔{id_b} уже существует")
            return
        self._push_history()
        a = next((n for n in self._nodes if n["id"] == id_a), None)
        b = next((n for n in self._nodes if n["id"] == id_b), None)
        if a and b:
            w = _edge_weight(a["x"], a["y"], b["x"], b["y"])
            self._edges.append({"from": id_a, "to": id_b, "weight": w})
            self._edges.append({"from": id_b, "to": id_a, "weight": w})
            self._data["edges"] = self._edges
            self._modified = True
            print(f"Ребро {id_a}↔{id_b}  (вес {w:.1f})")

    def _delete_edge_between(self, id_a: int, id_b: int) -> None:
        before = len(self._edges)
        self._push_history()
        self._edges = [e for e in self._edges
                       if not ((e["from"] == id_a and e["to"] == id_b) or
                               (e["from"] == id_b and e["to"] == id_a))]
        self._data["edges"] = self._edges
        removed = before - len(self._edges)
        self._modified = bool(removed)
        if removed:
            print(f"Удалено рёбер {id_a}↔{id_b}: {removed}")
        else:
            print(f"Нет ребра между {id_a} и {id_b}")

    def _cycle_action(self, node_id: int) -> None:
        self._push_history()
        for n in self._nodes:
            if n["id"] == node_id:
                current = n.get("action", "")
                try:
                    idx = list(NODE_ACTIONS).index(current)
                except ValueError:
                    idx = 0
                new_action = NODE_ACTIONS[(idx + 1) % len(NODE_ACTIONS)]
                n["action"] = new_action
                self._modified = True
                print(f"Узел {node_id}: action = {new_action!r}")
                break

    # ------------------------------------------------------------------
    # Обработка мыши
    # ------------------------------------------------------------------
    def _on_mouse(self, event, x, y, flags, param) -> None:
        self._mouse_pos = (x, y)
        self._hover_node = self._find_nearest_node(x, y)

        # ПКМ удаляет узел в любом режиме
        if event == cv2.EVENT_RBUTTONDOWN:
            nid = self._find_nearest_node(x, y)
            if nid is not None:
                self._delete_node(nid)
            return

        if self._mode == Mode.MOVE:
            self._mouse_move(event, x, y)
        elif self._mode == Mode.CREATE:
            self._mouse_create(event, x, y)
        elif self._mode == Mode.EDGE:
            self._mouse_edge(event, x, y)
        elif self._mode == Mode.LOOK:
            self._mouse_look(event, x, y)

    def _mouse_move(self, event, x, y) -> None:
        if event == cv2.EVENT_LBUTTONDOWN:
            nid = self._find_nearest_node(x, y)
            if nid is not None:
                self._selected = nid
                self._dragging = True
                self._push_history()
                if nid != self._found_id:
                    self._found_id = None  # снять подсветку поиска при выборе другого
            else:
                self._selected = None
                self._dragging = False
                self._found_id = None

        elif event == cv2.EVENT_MOUSEMOVE:
            if self._dragging and self._selected is not None:
                self._move_node(self._selected, x, y)

        elif event == cv2.EVENT_LBUTTONUP:
            self._dragging = False

        elif event == cv2.EVENT_RBUTTONDOWN:
            nid = self._find_nearest_node(x, y)
            if nid is not None:
                self._delete_node(nid)

    def _mouse_create(self, event, x, y) -> None:
        if event == cv2.EVENT_LBUTTONDOWN:
            nid = self._find_nearest_node(x, y)
            if nid is not None:
                self._selected = nid  # выбрать существующий
            else:
                new_id = self._add_node(x, y)
                self._selected = new_id

    def _mouse_edge(self, event, x, y) -> None:
        if event == cv2.EVENT_LBUTTONDOWN:
            nid = self._find_nearest_node(x, y)
            if nid is None:
                return
            if self._edge_src is None:
                self._edge_src = nid
                print(f"Ребро: выбран узел A={nid} — кликни узел B")
            else:
                self._add_edge(self._edge_src, nid)
                self._edge_src = None

    def _mouse_look(self, event, x, y) -> None:
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        nid = self._find_nearest_node(x, y)
        if self._look_src is None:
            if nid is not None:
                self._look_src = nid
                self._look_dst = None
                print(f"LOOK: from A={nid} выбран")
            return
        if self._look_dst is None:
            if nid is not None and nid != self._look_src:
                self._look_dst = nid
                print(f"LOOK: to B={nid} выбран. Кликни точку взгляда")
            return
        if nid is None:
            self._set_look_point(self._look_src, self._look_dst, x, y)
        elif nid == self._look_src:
            self._look_dst = None
            print("LOOK: сброшен узел B, выбери новый")

    # ------------------------------------------------------------------
    # Отрисовка
    # ------------------------------------------------------------------
    def _draw(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        vis_w = int(w * self._scale)
        vis_h = int(h * self._scale)
        vis = cv2.resize(frame, (vis_w, vis_h), interpolation=cv2.INTER_LINEAR)

        self._refresh_random_target_marker()
        self._draw_edges(vis)
        self._draw_look_edges(vis)
        self._draw_nodes(vis)
        self._draw_random_target_node(vis)
        self._draw_found_node(vis)
        self._draw_edge_preview(vis)
        self._draw_ui(vis, vis_w, vis_h)
        return vis

    def _refresh_random_target_marker(self) -> None:
        try:
            stat = RANDOM_TARGET_MARKER.stat()
        except OSError:
            self._random_target_id = None
            self._random_target_mtime_ns = None
            return
        if self._random_target_mtime_ns == stat.st_mtime_ns:
            return
        self._random_target_mtime_ns = stat.st_mtime_ns
        try:
            raw = RANDOM_TARGET_MARKER.read_text(encoding="utf-8").strip()
            self._random_target_id = int(raw)
        except (OSError, ValueError):
            self._random_target_id = None

    def _draw_random_target_node(self, vis: np.ndarray) -> None:
        """Подсветка узла, который nav_demo выбрал как случайную цель."""
        if self._random_target_id is None:
            return
        node = next((n for n in self._nodes if n["id"] == self._random_target_id), None)
        if node is None:
            return
        p = self._node_screen_pos(node)
        if p is None:
            return
        pulse = int(time.monotonic() * 3) % 2
        outer_r = 14 + pulse * 3
        cv2.circle(vis, p, outer_r, (255, 120, 0), 2, cv2.LINE_AA)
        cv2.circle(vis, p, 8, (255, 180, 40), -1, cv2.LINE_AA)
        cv2.circle(vis, p, 10, (0, 0, 0), 1, cv2.LINE_AA)
        tag = f"RAND #{self._random_target_id}"
        cv2.putText(
            vis,
            tag,
            (p[0] + 12, p[1] - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (255, 220, 120),
            1,
            cv2.LINE_AA,
        )

    def _draw_look_edges(self, vis: np.ndarray) -> None:
        for edge_key, payload in self._look_edges.items():
            parts = edge_key.split("->")
            if len(parts) != 2:
                continue
            try:
                src_id = int(parts[0])
                dst_id = int(parts[1])
            except ValueError:
                continue
            src_node = next((n for n in self._nodes if n["id"] == src_id), None)
            dst_node = next((n for n in self._nodes if n["id"] == dst_id), None)
            if src_node is None or dst_node is None:
                continue
            src_p = self._node_screen_pos(src_node)
            dst_p = self._node_screen_pos(dst_node)
            if src_p is None or dst_p is None:
                continue
            mid_x = int((src_p[0] + dst_p[0]) / 2)
            mid_y = int((src_p[1] + dst_p[1]) / 2)
            mode = str(payload.get("mode", ""))
            if mode == "look_point":
                try:
                    lx, ly = self._converter.world_to_pixel(float(payload["x"]), float(payload["y"]))
                except (KeyError, TypeError, ValueError):
                    continue
                look_p = (int(round(lx * self._scale)), int(round(ly * self._scale)))
                cv2.arrowedLine(vis, (mid_x, mid_y), look_p, (0, 200, 255), 2, cv2.LINE_AA, tipLength=0.2)
                cv2.circle(vis, look_p, 5, (0, 200, 255), -1, cv2.LINE_AA)
            elif mode == "fixed_yaw":
                try:
                    yaw = float(payload["yaw"])
                except (KeyError, TypeError, ValueError):
                    continue
                r = 26
                rad = math.radians(yaw)
                tip = (int(mid_x + math.cos(rad) * r), int(mid_y + math.sin(rad) * r))
                cv2.arrowedLine(vis, (mid_x, mid_y), tip, (255, 200, 0), 2, cv2.LINE_AA, tipLength=0.35)

    def _draw_edges(self, vis: np.ndarray) -> None:
        if not self._show_edges:
            return
        sel   = self._selected
        e_src = self._edge_src
        for e in self._edges:
            a_node = next((n for n in self._nodes if n["id"] == e["from"]), None)
            b_node = next((n for n in self._nodes if n["id"] == e["to"]),   None)
            if not (a_node and b_node):
                continue
            a = self._node_screen_pos(a_node)
            b = self._node_screen_pos(b_node)
            if not (a and b):
                continue
            # Подсветить рёбра выбранного узла
            if sel is not None and (e["from"] == sel or e["to"] == sel):
                color, thick = (100, 200, 100), 2
            elif e_src is not None and (e["from"] == e_src or e["to"] == e_src):
                color, thick = (0, 220, 220), 2
            else:
                color, thick = (55, 55, 55), 1
            cv2.line(vis, a, b, color, thick, cv2.LINE_AA)

    def _draw_nodes(self, vis: np.ndarray) -> None:
        for n in self._nodes:
            p = self._node_screen_pos(n)
            if p is None:
                continue
            nid    = n["id"]
            action = n.get("action", "")

            is_selected  = nid == self._selected
            is_edge_src  = nid == self._edge_src
            is_hover     = nid == self._hover_node
            r = 6 if is_hover else 4

            if is_edge_src:
                cv2.circle(vis, p, r + 5, (0, 220, 220), 2, cv2.LINE_AA)
                cv2.circle(vis, p, r + 2, (0, 220, 220), -1, cv2.LINE_AA)
            elif is_selected:
                cv2.circle(vis, p, r + 5, (0, 255, 255), -1, cv2.LINE_AA)
                cv2.circle(vis, p, r + 6, (0, 0, 0),      1, cv2.LINE_AA)
            elif action:
                color = _ACTION_COLORS.get(action, (128, 128, 128))
                cv2.circle(vis, p, r + 1, color, -1, cv2.LINE_AA)
                if is_hover:
                    cv2.circle(vis, p, r + 3, color, 1, cv2.LINE_AA)
            elif n.get("corner"):
                cv2.circle(vis, p, r, (80, 80, 255), -1, cv2.LINE_AA)
            else:
                cv2.circle(vis, p, r, (0, 200, 80), -1, cv2.LINE_AA)
                if is_hover:
                    cv2.circle(vis, p, r + 2, (0, 200, 80), 1, cv2.LINE_AA)

            if self._show_ids:
                label = str(nid)
                if action:
                    label += f"[{action[0]}]"
                cv2.putText(vis, label, (p[0] + 6, p[1] - 2),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.40, (160, 80, 30), 1)

    def _draw_found_node(self, vis: np.ndarray) -> None:
        """Мигающая подсветка найденного узла поверх остальных."""
        if self._found_id is None:
            return
        node = next((n for n in self._nodes if n["id"] == self._found_id), None)
        if node is None:
            return
        p = self._node_screen_pos(node)
        if p is None:
            return
        # Мигание: 4 раза в секунду
        phase = int(time.monotonic() * 4) % 2
        outer_r = 18 + phase * 4
        cv2.circle(vis, p, outer_r, (255, 255, 255), 3, cv2.LINE_AA)
        cv2.circle(vis, p, 10, (255, 255, 255), -1, cv2.LINE_AA)
        cv2.circle(vis, p, 12, (0, 0, 0), 1, cv2.LINE_AA)
        label = f"#{node['id']}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(vis, (p[0] + 14, p[1] - th - 4), (p[0] + 14 + tw + 4, p[1] + 2), (0, 0, 0), -1)
        cv2.putText(vis, label, (p[0] + 16, p[1]),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

    def _draw_edge_preview(self, vis: np.ndarray) -> None:
        """Пунктирная линия от выбранного узла A к курсору в режиме EDGE."""
        if self._mode != Mode.EDGE or self._edge_src is None:
            return
        src_node = next((n for n in self._nodes if n["id"] == self._edge_src), None)
        if src_node is None:
            return
        src_p = self._node_screen_pos(src_node)
        if src_p is None:
            return
        dst = self._mouse_pos
        # Пунктир через сегменты
        dx = dst[0] - src_p[0]
        dy = dst[1] - src_p[1]
        dist = math.hypot(dx, dy)
        if dist < 1:
            return
        seg, gap = 10, 6
        step = seg + gap
        total = int(dist / step)
        ux, uy = dx / dist, dy / dist
        for i in range(total):
            x0 = int(src_p[0] + ux * i * step)
            y0 = int(src_p[1] + uy * i * step)
            x1 = int(src_p[0] + ux * (i * step + seg))
            y1 = int(src_p[1] + uy * (i * step + seg))
            cv2.line(vis, (x0, y0), (x1, y1), (0, 220, 220), 1, cv2.LINE_AA)

    def _draw_ui(self, vis: np.ndarray, vis_w: int, vis_h: int) -> None:
        FS       = 0.48   # основной размер шрифта
        FS_SMALL = 0.40   # мелкий (соседи, легенда, id узлов)
        FS_MODE  = 0.72   # бейдж режима
        LINE_H   = 19     # межстрочный интервал подсказок

        # ---------- Панель режима (верхний-правый угол) ----------
        mode_color = _MODE_COLORS[self._mode]
        mode_label = f" {self._mode.value} "
        (tw, th), _ = cv2.getTextSize(mode_label, cv2.FONT_HERSHEY_SIMPLEX, FS_MODE, 2)
        rx = vis_w - tw - 14
        cv2.rectangle(vis, (rx - 6, 4), (vis_w - 4, th + 14), mode_color, -1)
        cv2.putText(vis, mode_label, (rx, th + 8),
                    cv2.FONT_HERSHEY_SIMPLEX, FS_MODE, (0, 0, 0), 2)

        # ---------- Подсказки (левый верх) ----------
        hints = _MODE_HINTS[self._mode]
        panel_w = 240
        panel_h = len(hints) * LINE_H + 10
        overlay = vis.copy()
        cv2.rectangle(overlay, (0, 0), (panel_w, panel_h), (20, 20, 20), -1)
        cv2.addWeighted(overlay, 0.55, vis, 0.45, 0, vis)
        for i, txt in enumerate(hints):
            color = (200, 200, 200) if i > 0 else (160, 220, 160)
            cv2.putText(vis, txt, (6, LINE_H + i * LINE_H - 2),
                        cv2.FONT_HERSHEY_SIMPLEX, FS, color, 1)

        # ---------- Легенда (цветные узлы) ----------
        legend = [
            ((0, 200, 80),  "обычный"),
            ((80, 80, 255), "corner"),
            ((0, 165, 255), "jump"),
            ((255, 100, 0), "crouch"),
            ((200, 0, 200), "shift"),
            ((0, 220, 220), "look"),
        ]
        lx = vis_w - 115
        for i, (col, lbl) in enumerate(legend):
            ly = vis_h - 12 - i * 18
            cv2.circle(vis, (lx, ly - 4), 6, col, -1, cv2.LINE_AA)
            cv2.putText(vis, lbl, (lx + 11, ly),
                        cv2.FONT_HERSHEY_SIMPLEX, FS_SMALL, (200, 200, 200), 1)

        # ---------- Статус (нижняя строка) ----------
        mod_mark = " *" if self._modified else ""
        status = (
            f"Узлов: {len(self._nodes)}  Рёбер: {len(self._edges) // 2}  "
            f"LOOK: {len(self._look_edges)}{mod_mark}"
        )
        cv2.putText(vis, status, (6, vis_h - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, FS, (200, 200, 100), 1)

        # ---------- Инфо выбранного узла ----------
        if self._selected is not None:
            sel = next((n for n in self._nodes if n["id"] == self._selected), None)
            if sel:
                action = sel.get("action", "") or "(нет)"
                neighbors = sorted(set(
                    e["to"] for e in self._edges if e["from"] == self._selected
                ) | set(
                    e["from"] for e in self._edges if e["to"] == self._selected
                ))
                nb_str = ",".join(str(x) for x in neighbors[:8])
                if len(neighbors) > 8:
                    nb_str += "…"
                line1 = f"id={sel['id']}  ({sel['x']:.0f}, {sel['y']:.0f})  action={action}"
                line2 = f"  соседи: [{nb_str}]  (всего {len(neighbors)})"
                cv2.putText(vis, line1, (6, vis_h - 34),
                            cv2.FONT_HERSHEY_SIMPLEX, FS, (0, 255, 255), 1)
                cv2.putText(vis, line2, (6, vis_h - 18),
                            cv2.FONT_HERSHEY_SIMPLEX, FS_SMALL, (140, 230, 230), 1)

        # ---------- Подсказка режима EDGE — узел A выбран ----------
        if self._mode == Mode.EDGE and self._edge_src is not None:
            msg = f"  EDGE: A={self._edge_src} → кликни узел B  (ESC = отмена)  "
            cv2.putText(vis, msg, (6, vis_h - 54),
                        cv2.FONT_HERSHEY_SIMPLEX, FS, (0, 220, 220), 1)
        if self._mode == Mode.LOOK:
            if self._look_src is None:
                msg = "  LOOK: выбери узел A"
            elif self._look_dst is None:
                msg = f"  LOOK: A={self._look_src}, выбери узел B"
            else:
                key = f"{self._look_src}->{self._look_dst}"
                msg = f"  LOOK: {key}  ЛКМ=look_point  A=fixed_yaw  K=clear"
            cv2.putText(vis, msg, (6, vis_h - 54),
                        cv2.FONT_HERSHEY_SIMPLEX, FS, (0, 190, 255), 1)

        # ---------- Строка поиска ----------
        if self._search_mode:
            cursor = "_" if (int(time.monotonic() * 2) % 2 == 0) else " "
            search_txt = f"  / Поиск id: {self._search_buf}{cursor}  (Enter=найти  ESC=отмена)"
            color = (80, 80, 255) if self._search_buf and self._found_id is None and self._search_buf.isdigit() and not any(n["id"] == int(self._search_buf) for n in self._nodes) else (255, 255, 255)
            (sw, sh), _ = cv2.getTextSize(search_txt, cv2.FONT_HERSHEY_SIMPLEX, FS, 2)
            cv2.rectangle(vis, (0, vis_h - sh - 20), (sw + 12, vis_h), (20, 20, 20), -1)
            cv2.putText(vis, search_txt, (6, vis_h - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, FS, color, 2)

    # ------------------------------------------------------------------
    # Главный цикл
    # ------------------------------------------------------------------
    def run(self) -> None:
        print(f"Редактор: {self._path.name}  |  узлов={len(self._nodes)}  рёбер={len(self._edges) // 2}")
        print("M=MOVE  C=CREATE  G=EDGE  L=LOOK  |  S=сохранить  U=undo  Q=выход")

        while True:
            frame = self._grabber.grab_bgr()
            if frame is None:
                cv2.waitKey(30)
                continue

            vis = self._draw(frame)
            cv2.imshow(self._win, vis)

            key = cv2.waitKey(30) & 0xFF
            if key == 255:
                continue

            # --- Режим поиска узла по ID ---
            if self._search_mode:
                if ord("0") <= key <= ord("9"):
                    self._search_buf += chr(key)
                    self._found_id = None  # сброс при вводе
                elif key == 8:  # Backspace
                    self._search_buf = self._search_buf[:-1]
                    self._found_id = None
                elif key == 13:  # Enter — найти
                    if self._search_buf:
                        target_id = int(self._search_buf)
                        found = next((n for n in self._nodes if n["id"] == target_id), None)
                        if found:
                            self._found_id = target_id
                            self._selected = target_id
                            print(f"Найден узел {target_id}  ({found['x']:.0f}, {found['y']:.0f})")
                        else:
                            print(f"Узел {target_id} не найден")
                            self._found_id = None
                elif key == 27:  # ESC — выйти из поиска
                    self._search_mode = False
                    self._search_buf  = ""
                    self._found_id    = None
                continue  # не передавать клавишу дальше пока в режиме поиска

            # --- Смена режима ---
            if key == ord("m"):
                self._mode = Mode.MOVE
                self._edge_src = None
                print("Режим: MOVE")

            elif key == ord("c"):
                self._mode = Mode.CREATE
                self._edge_src = None
                print("Режим: CREATE — клик ЛКМ на пустом месте создаёт узел")

            elif key == ord("g"):
                self._mode = Mode.EDGE
                self._edge_src = None
                print("Режим: EDGE — кликни два узла для соединения")

            elif key == ord("l"):
                self._mode = Mode.LOOK
                self._edge_src = None
                self._look_src = None
                self._look_dst = None
                print("Режим: LOOK — выбери A -> B, затем кликни точку взгляда")

            elif key == 27:  # ESC — сбросить в MOVE
                if self._mode == Mode.EDGE and self._edge_src is not None:
                    print("Выбор узла A отменён")
                    self._edge_src = None
                elif self._mode == Mode.LOOK and self._look_src is not None and self._look_dst is None:
                    self._look_src = None
                    print("LOOK: выбор узла A отменен")
                elif self._mode == Mode.LOOK and self._look_src is not None and self._look_dst is not None:
                    self._look_dst = None
                    print("LOOK: выбор узла B отменен")
                else:
                    self._mode = Mode.MOVE
                    self._edge_src = None
                    self._look_src = None
                    self._look_dst = None
                    print("Режим: MOVE")

            # --- Общие действия ---
            elif key == ord("s"):
                self._save()

            elif key == ord("u"):
                self._undo()

            elif key == ord("/"):
                self._search_mode = True
                self._search_buf  = ""
                self._found_id    = None

            elif key == ord("n"):
                self._show_ids = not self._show_ids

            elif key == ord("e"):
                self._show_edges = not self._show_edges

            elif key == ord("q"):
                if self._modified:
                    print("[!] Несохранённые изменения! Нажми S чтобы сохранить, или Q ещё раз.")
                    k2 = cv2.waitKey(4000) & 0xFF
                    if k2 == ord("q"):
                        break
                    elif k2 == ord("s"):
                        self._save()
                        break
                else:
                    break

            # --- Действия над выбранным узлом (только в MOVE) ---
            elif key == ord("d"):
                # Удаление работает в любом режиме
                if self._selected is not None:
                    self._delete_node(self._selected)
                    self._selected = None
                else:
                    hover = self._find_nearest_node(*self._mouse_pos)
                    if hover is not None:
                        self._delete_node(hover)

            elif key == ord("a"):
                if self._mode == Mode.LOOK and self._look_src is not None and self._look_dst is not None:
                    mx, my = self._mouse_pos
                    self._set_fixed_yaw_from_cursor(self._look_src, self._look_dst, mx, my)
                elif self._selected is not None:
                    self._cycle_action(self._selected)

            elif key == ord("k"):
                if self._mode == Mode.LOOK and self._look_src is not None and self._look_dst is not None:
                    self._clear_look_edge(self._look_src, self._look_dst)

            elif key == ord("x"):
                if self._selected is not None:
                    hover = self._find_nearest_node(*self._mouse_pos)
                    if hover is not None and hover != self._selected:
                        self._delete_edge_between(self._selected, hover)

        cv2.destroyAllWindows()


def main() -> None:
    parser = argparse.ArgumentParser(description="Navmesh editor with radar overlay")
    parser.add_argument("--map",   default="de_poseidon")
    parser.add_argument("--mode",  default="wingman", choices=["competitive", "wingman"])
    parser.add_argument("--scale", type=float, default=4.0,
                        help="Масштаб окна (дефолт 4.0)")
    args = parser.parse_args()

    cfg      = RadarConfig(map_name=args.map, game_mode=args.mode)
    cal_path = cfg.calibration_path

    if not cal_path.is_file():
        print(f"[ERROR] Калибровка не найдена: {cal_path}")
        sys.exit(1)

    navmesh_path = ROOT / "navmesh" / f"{args.map}.json"
    if not navmesh_path.is_file():
        print(f"[ERROR] Navmesh не найден: {navmesh_path}")
        sys.exit(1)

    converter = CoordConverter.from_file(cal_path)
    grabber   = RadarGrabber(cfg)
    grabber.open()

    try:
        editor = NavmeshEditor(navmesh_path, converter, grabber, scale=args.scale)
        editor.run()
    finally:
        grabber.close()


if __name__ == "__main__":
    main()
