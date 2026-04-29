"""A* поиск кратчайшего пути в navmesh-графе."""

from __future__ import annotations

import heapq
from typing import Dict, List, Optional, Tuple

from .navmesh import NavmeshGraph, NavNode


class AStarPathfinder:
    def __init__(self, graph: NavmeshGraph) -> None:
        self._graph = graph

    # ------------------------------------------------------------------
    def find_path(self, from_id: int, to_id: int) -> List[int]:
        """Возвращает список id узлов от from_id до to_id включительно.

        Возвращает пустой список, если путь не найден.
        """
        if from_id == to_id:
            return [from_id]

        # (f_score, node_id)
        open_heap: List[Tuple[float, int]] = []
        heapq.heappush(open_heap, (0.0, from_id))

        g: Dict[int, float] = {from_id: 0.0}
        came_from: Dict[int, int] = {}

        while open_heap:
            _, current = heapq.heappop(open_heap)

            if current == to_id:
                return self._reconstruct(came_from, current)

            for neighbor_id, weight in self._graph.get_neighbors(current):
                tentative_g = g[current] + weight
                if tentative_g < g.get(neighbor_id, float("inf")):
                    g[neighbor_id] = tentative_g
                    h = self._graph.distance_3d(neighbor_id, to_id)
                    heapq.heappush(open_heap, (tentative_g + h, neighbor_id))
                    came_from[neighbor_id] = current

        return []

    @staticmethod
    def _reconstruct(came_from: Dict[int, int], current: int) -> List[int]:
        path = [current]
        while current in came_from:
            current = came_from[current]
            path.append(current)
        path.reverse()
        return path

    # ------------------------------------------------------------------
    def find_path_nodes(self, from_id: int, to_id: int) -> List[NavNode]:
        ids = self.find_path(from_id, to_id)
        return [n for i in ids if (n := self._graph.get_node(i)) is not None]

    def find_path_from_pos(
        self,
        fx: float,
        fy: float,
        fz: float,
        tx: float,
        ty: float,
        tz: float,
    ) -> List[NavNode]:
        """Найти путь от позиции (fx,fy,fz) к позиции (tx,ty,tz)."""
        start = self._graph.find_nearest_node(fx, fy, fz)
        end = self._graph.find_nearest_node(tx, ty, tz)
        if start is None or end is None:
            return []
        return self.find_path_nodes(start.id, end.id)

    def find_path_to_node(
        self,
        fx: float,
        fy: float,
        fz: float,
        target_node_id: int,
    ) -> List[NavNode]:
        """Путь от текущей позиции к конкретному узлу navmesh."""
        start = self._graph.find_nearest_node(fx, fy, fz)
        if start is None:
            return []
        return self.find_path_nodes(start.id, target_node_id)
