"""
Граф waypoints: JSON на диске -> соседи -> A*.

Единицы координат - те же, что ты потом будешь сравнивать с позицией игрока
(мировые Source или своя шкала; главное - единообразие).
"""
from __future__ import annotations

import heapq
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple


@dataclass
class Node:
    id: str
    x: float
    y: float
    z: float = 0.0
    tags: List[str] = field(default_factory=list)


def _dist(a: Node, b: Node) -> float:
    dx = a.x - b.x
    dy = a.y - b.y
    dz = a.z - b.z
    return math.sqrt(dx * dx + dy * dy + dz * dz)


class WaypointGraph:
    map_name: str
    nodes: Dict[str, Node]

    def __init__(
        self,
        map_name: str,
        nodes: Dict[str, Node],
        edges: List[Tuple[str, str, float]],
    ) -> None:
        self.map_name = map_name
        self.nodes = nodes
        self._adj: Dict[str, List[Tuple[str, float]]] = {nid: [] for nid in nodes}
        for u, v, d in edges:
            if u in self._adj and v in self._adj:
                self._adj[u].append((v, d))
                self._adj[v].append((u, d))

    @classmethod
    def from_json_path(cls, path: str | Path) -> WaypointGraph:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WaypointGraph:
        map_name = str(data.get("map", "unknown"))
        raw_nodes = data.get("nodes", [])
        nodes: Dict[str, Node] = {}
        for item in raw_nodes:
            nid = str(item["id"])
            t = item.get("tags", [])
            nodes[nid] = Node(
                id=nid,
                x=float(item["x"]),
                y=float(item["y"]),
                z=float(item.get("z", 0.0)),
                tags=list(t),
            )
        edges: List[Tuple[str, str, float]] = []
        for pair in data.get("edges", []):
            if len(pair) >= 3:
                u, v, d = str(pair[0]), str(pair[1]), float(pair[2])
            else:
                u, v = str(pair[0]), str(pair[1])
                d = _dist(nodes[u], nodes[v]) if (u in nodes and v in nodes) else 0.0
            edges.append((u, v, d))
        return cls(map_name, nodes, edges)

    def neighbors(self, node_id: str) -> Iterator[Tuple[str, float]]:
        return iter(self._adj.get(node_id, ()))

    def nearest_node_id(self, x: float, y: float, z: float) -> str:
        probe = Node(id="__probe__", x=x, y=y, z=z)
        best: Optional[str] = None
        best_d = math.inf
        for nid, n in self.nodes.items():
            d = _dist(probe, n)
            if d < best_d:
                best_d = d
                best = nid
        if best is None:
            raise ValueError("empty graph")
        return best

    def astar(self, start_id: str, goal_id: str) -> Optional[List[str]]:
        def h(nid: str) -> float:
            return _dist(self.nodes[nid], self.nodes[goal_id])

        open_heap: List[Tuple[float, float, str]] = [(h(start_id), 0.0, start_id)]
        g_score: Dict[str, float] = {start_id: 0.0}
        came: Dict[str, Optional[str]] = {start_id: None}

        while open_heap:
            _, g, current = heapq.heappop(open_heap)
            if current == goal_id:
                path: List[str] = []
                c: Optional[str] = current
                while c is not None:
                    path.append(c)
                    c = came[c]
                path.reverse()
                return path
            for nb, step in self.neighbors(current):
                tentative = g_score[current] + step
                if tentative < g_score.get(nb, math.inf):
                    g_score[nb] = tentative
                    came[nb] = current
                    heapq.heappush(open_heap, (tentative + h(nb), tentative, nb))
        return None
