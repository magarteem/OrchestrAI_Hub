"""Загрузка и работа с navmesh-графом из JSON (формат CS2-AI)."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class NavNode:
    id: int
    x: float
    y: float
    z: float
    corner: bool
    # Действие при достижении узла:
    #   ""          — ничего (обычное движение)
    #   "jump"      — прыжок (пробел)
    #   "crouch"    — присесть до следующего узла (ctrl)
    #   "shift"     — идти на шифте (тихо) до следующего узла
    #   "look_north" / "look_south" / "look_east" / "look_west" — повернуть взгляд
    action: str = ""


class NavmeshGraph:
    """Граф navmesh: узлы (x,y,z) и взвешенные рёбра."""

    def __init__(self, json_path: str | Path) -> None:
        self._path = Path(json_path)
        self._nodes: Dict[int, NavNode] = {}
        # id → список (сосед_id, вес)
        self._adjacency: Dict[int, List[Tuple[int, float]]] = {}
        # look-настройки для направленных ребер: "a->b" -> {"mode": "...", ...}
        self._look_edges: Dict[str, dict] = {}
        self._map_name: str = ""
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        data = json.loads(self._path.read_text(encoding="utf-8"))
        self._map_name = data.get("map_name", "")

        for n in data["nodes"]:
            node = NavNode(
                id=n["id"],
                x=float(n["x"]),
                y=float(n["y"]),
                z=float(n["z"]),
                corner=bool(n.get("corner", False)),
                action=str(n.get("action", "")),
            )
            self._nodes[node.id] = node
            self._adjacency[node.id] = []

        for e in data["edges"]:
            src = int(e["from"])
            dst = int(e["to"])
            w = float(e["weight"])
            if src in self._adjacency:
                self._adjacency[src].append((dst, w))

        raw_look_edges = data.get("look_edges", {})
        if isinstance(raw_look_edges, dict):
            for edge_key, payload in raw_look_edges.items():
                if isinstance(edge_key, str) and isinstance(payload, dict):
                    self._look_edges[edge_key] = payload

    # ------------------------------------------------------------------
    @property
    def map_name(self) -> str:
        return self._map_name

    @property
    def nodes(self) -> Dict[int, NavNode]:
        return self._nodes

    def get_node(self, node_id: int) -> Optional[NavNode]:
        return self._nodes.get(node_id)

    def get_neighbors(self, node_id: int) -> List[Tuple[int, float]]:
        return self._adjacency.get(node_id, [])

    def get_look_edge(self, src_id: int, dst_id: int) -> Optional[dict]:
        return self._look_edges.get(f"{src_id}->{dst_id}")

    # ------------------------------------------------------------------
    def find_nearest_node(self, x: float, y: float, z: float) -> Optional[NavNode]:
        """Ближайший узел к точке (x, y, z) по евклидову расстоянию."""
        if not self._nodes:
            return None
        return min(
            self._nodes.values(),
            key=lambda n: (n.x - x) ** 2 + (n.y - y) ** 2 + (n.z - z) ** 2,
        )

    def distance_3d(self, a_id: int, b_id: int) -> float:
        a = self._nodes.get(a_id)
        b = self._nodes.get(b_id)
        if a is None or b is None:
            return float("inf")
        return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)
