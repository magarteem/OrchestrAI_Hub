"""
Запись waypoints из бота: бот передаёт (x, y, z) - строится цепочка узлов и рёбра.

Вызов из своего кода:
    rec = WaypointRecorder(map_name="de_dust2")
    rec.add_point(x, y, z, tags=["recorded"])
    rec.save_json(path)

Или запусти record_server.py - бот шлёт HTTP POST на /add с JSON {"x","y","z","tags":[]}.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .graph import Node


def _dist3(a: Tuple[float, float, float], b: Tuple[float, float, float]) -> float:
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    dz = a[2] - b[2]
    return (dx * dx + dy * dy + dz * dz) ** 0.5


@dataclass
class WaypointRecorder:
    map_name: str
    dedup_radius: float = 32.0
    units: str = "bot_world"
    _nodes: List[Node] = field(default_factory=list)
    _edges: List[Tuple[str, str]] = field(default_factory=list)
    _counter: int = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"wp_{self._counter:05d}"

    def add_point(
        self,
        x: float,
        y: float,
        z: float,
        tags: Optional[List[str]] = None,
        node_id: Optional[str] = None,
        dedup: bool = True,
        link_previous: bool = True,
    ) -> Optional[str]:
        """
        Добавить узел. Если dedup и слишком близко к последнему - пропуск (None).
        При link_previous соединяет с предыдущим узлом ребром (неориентированное в JSON).
        """
        tag_list = list(tags) if tags else []
        pos = (float(x), float(y), float(z))

        if dedup and self._nodes:
            last = self._nodes[-1]
            last_pos = (last.x, last.y, last.z)
            if _dist3(pos, last_pos) < self.dedup_radius:
                return None

        prev_id: Optional[str] = self._nodes[-1].id if self._nodes else None
        nid = node_id if node_id is not None else self._next_id()

        self._nodes.append(Node(id=nid, x=float(x), y=float(y), z=float(z), tags=tag_list))

        if link_previous and prev_id is not None:
            self._edges.append((prev_id, nid))

        return nid

    def to_dict(self) -> Dict[str, Any]:
        return {
            "map": self.map_name,
            "units": self.units,
            "nodes": [
                {"id": n.id, "x": n.x, "y": n.y, "z": n.z, "tags": list(n.tags)}
                for n in self._nodes
            ],
            "edges": [[a, b] for a, b in self._edges],
        }

    def save_json(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def load_merge_points(
        cls, path: str | Path, map_name: Optional[str] = None
    ) -> WaypointRecorder:
        """Дозагрузить существующий граф и продолжить добавлять точки."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        rec = cls(
            map_name=str(map_name or data.get("map", "recorded")),
            units=str(data.get("units", "bot_world")),
        )
        for item in data.get("nodes", []):
            nid = str(item.get("id", ""))
            t = item.get("tags", [])
            rec._nodes.append(
                Node(
                    id=nid,
                    x=float(item.get("x", 0.0)),
                    y=float(item.get("y", 0.0)),
                    z=float(item.get("z", 0.0)),
                    tags=list(t),
                )
            )
            if nid.startswith("wp_"):
                try:
                    num = int(nid[3:])
                    rec._counter = max(rec._counter, num)
                except ValueError:
                    pass
        for pair in data.get("edges", []):
            if len(pair) >= 2:
                rec._edges.append((str(pair[0]), str(pair[1])))
        return rec
