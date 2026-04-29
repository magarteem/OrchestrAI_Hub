"""
Конвертер бинарного .nav файла CS2 → формат CS2-AI navmesh JSON.

Зависимости: только стандартная библиотека Python (3.9+).

Использование:
    python tools/navmesh_from_nav.py --nav navmesh/de_inferno.nav --map de_inferno

Параметры:
    --nav       путь к .nav файлу
    --map       имя карты (используется в map_name и имени выходного файла)
    --min-dist  минимальное расстояние между узлами (default: 180)
    --out       путь к выходному JSON (default: navmesh/<map>.json)
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from pathlib import Path
from typing import BinaryIO

_NAVMESH_DIR = Path(__file__).resolve().parent.parent / "navmesh"
_NAV_MAGIC = 0xFEEDFACE


# ---------------------------------------------------------------------------
# Минимальный парсер CS2 .nav (портировано из awpy/nav.py, MIT)
# ---------------------------------------------------------------------------

class _Vec3:
    __slots__ = ("x", "y", "z")

    def __init__(self, x: float, y: float, z: float) -> None:
        self.x = x
        self.y = y
        self.z = z


class _NavArea:
    __slots__ = ("area_id", "hull_index", "corners", "connections")

    def __init__(
        self,
        area_id: int,
        hull_index: int,
        corners: list[_Vec3],
        connections: list[int],
    ) -> None:
        self.area_id = area_id
        self.hull_index = hull_index
        self.corners = corners
        self.connections: set[int] = set(connections)

    @property
    def centroid(self) -> _Vec3:
        n = len(self.corners)
        if not n:
            return _Vec3(0.0, 0.0, 0.0)
        return _Vec3(
            sum(c.x for c in self.corners) / n,
            sum(c.y for c in self.corners) / n,
            sum(c.z for c in self.corners) / n,
        )


def _read_u32(f: BinaryIO) -> int:
    return struct.unpack("I", f.read(4))[0]


def _read_connections(f: BinaryIO) -> list[int]:
    count = _read_u32(f)
    ids = []
    for _ in range(count):
        area_id = _read_u32(f)
        f.read(4)  # edge_id
        ids.append(area_id)
    return ids


def _read_polygons(f: BinaryIO, version: int) -> list[list[_Vec3]]:
    corner_count = _read_u32(f)
    corners: list[_Vec3] = []
    for _ in range(corner_count):
        x, y, z = struct.unpack("fff", f.read(12))
        corners.append(_Vec3(x, y, z))

    polygon_count = _read_u32(f)
    polygons: list[list[_Vec3]] = []
    for _ in range(polygon_count):
        poly_corner_count = f.read(1)[0]
        poly: list[_Vec3] = []
        for _ in range(poly_corner_count):
            idx = _read_u32(f)
            poly.append(corners[idx])
        if version >= 35:
            f.read(4)
        polygons.append(poly)
    return polygons


def _read_area(f: BinaryIO, version: int, polygons: list[list[_Vec3]] | None) -> _NavArea:
    area_id = _read_u32(f)
    f.read(8)  # dynamic_attribute_flags (int64)
    hull_index = f.read(1)[0]

    if version >= 31 and polygons is not None:
        poly_idx = _read_u32(f)
        area_corners = polygons[poly_idx]
    else:
        corner_count = _read_u32(f)
        area_corners = []
        for _ in range(corner_count):
            x, y, z = struct.unpack("fff", f.read(12))
            area_corners.append(_Vec3(x, y, z))

    f.read(4)  # almost always 0

    all_connections: list[int] = []
    for _ in range(len(area_corners)):
        all_connections.extend(_read_connections(f))

    f.read(5)  # LegacyHidingSpotData + LegacySpotEncounterData counts

    ladder_above_count = _read_u32(f)
    f.read(4 * ladder_above_count)

    ladder_below_count = _read_u32(f)
    f.read(4 * ladder_below_count)

    return _NavArea(area_id, hull_index, area_corners, all_connections)


def parse_nav(nav_path: Path) -> dict[int, _NavArea]:
    with open(nav_path, "rb") as f:
        magic = _read_u32(f)
        if magic != _NAV_MAGIC:
            sys.exit(f"Неверный magic: 0x{magic:X} — это не .nav файл CS2")

        version = _read_u32(f)
        if not (30 <= version <= 36):
            sys.exit(f"Неподдерживаемая версия nav: {version}")

        _read_u32(f)  # sub_version
        _read_u32(f)  # unk1 / is_analyzed

        polygons = None
        if version >= 31:
            polygons = _read_polygons(f, version)
        if version >= 32:
            f.read(4)  # unk2
        if version >= 35:
            f.read(4)  # unk3

        area_count = _read_u32(f)
        areas: dict[int, _NavArea] = {}
        for _ in range(area_count):
            area = _read_area(f, version, polygons)
            areas[area.area_id] = area

    return areas


# ---------------------------------------------------------------------------
# Конвертация
# ---------------------------------------------------------------------------

def _dist3(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _greedy_downsample(areas: list[_NavArea], min_dist: float) -> list[int]:
    kept_ids: list[int] = []
    kept_pos: list[tuple[float, float, float]] = []
    for area in areas:
        c = area.centroid
        pos = (c.x, c.y, c.z)
        if all(_dist3(pos, p) >= min_dist for p in kept_pos):
            kept_ids.append(area.area_id)
            kept_pos.append(pos)
    return kept_ids


def _nearest_kept(
    area_id: int,
    all_areas: dict[int, _NavArea],
    kept_set: set[int],
    cache: dict[int, int],
) -> int:
    if area_id in cache:
        return cache[area_id]
    if area_id in kept_set:
        cache[area_id] = area_id
        return area_id
    c = all_areas[area_id].centroid
    pos = (c.x, c.y, c.z)
    best = min(
        kept_set,
        key=lambda kid: _dist3(
            pos,
            (all_areas[kid].centroid.x, all_areas[kid].centroid.y, all_areas[kid].centroid.z),
        ),
    )
    cache[area_id] = best
    return best


def convert(nav_path: Path, map_name: str, min_dist: float, out_path: Path) -> None:
    print(f"Загрузка {nav_path} ...")
    all_areas = parse_nav(nav_path)
    print(f"Всего областей: {len(all_areas)}")

    hull_areas = [a for a in all_areas.values() if a.hull_index == 0]
    print(f"Hull=0: {len(hull_areas)}")

    kept_ids = _greedy_downsample(hull_areas, min_dist)
    kept_set = set(kept_ids)
    print(f"После прореживания (min_dist={min_dist:.0f}): {len(kept_ids)} узлов")

    area_to_node: dict[int, int] = {aid: i for i, aid in enumerate(kept_ids)}

    nodes = []
    for node_id, area_id in enumerate(kept_ids):
        area = all_areas[area_id]
        c = area.centroid
        is_corner = len(area.connections) <= 2
        nodes.append({
            "id": node_id,
            "x": round(c.x, 4),
            "y": round(c.y, 4),
            "z": round(c.z, 4),
            "corner": is_corner,
        })

    nearest_cache: dict[int, int] = {}
    edge_pairs: set[tuple[int, int]] = set()

    for area_id, area in all_areas.items():
        if area.hull_index != 0:
            continue
        src_node = area_to_node[_nearest_kept(area_id, all_areas, kept_set, nearest_cache)]
        for conn_id in area.connections:
            if conn_id not in all_areas:
                continue
            if all_areas[conn_id].hull_index != 0:
                continue
            dst_node = area_to_node[_nearest_kept(conn_id, all_areas, kept_set, nearest_cache)]
            if src_node != dst_node:
                edge_pairs.add((src_node, dst_node))

    seen_undirected: set[frozenset[int]] = set()
    edges = []
    for src, dst in sorted(edge_pairs):
        key = frozenset((src, dst))
        w = round(_dist3(
            (nodes[src]["x"], nodes[src]["y"], nodes[src]["z"]),
            (nodes[dst]["x"], nodes[dst]["y"], nodes[dst]["z"]),
        ), 4)
        edges.append({"from": src, "to": dst, "weight": w})
        if key not in seen_undirected:
            edges.append({"from": dst, "to": src, "weight": w})
            seen_undirected.add(key)

    result = {"map_name": map_name, "nodes": nodes, "edges": edges}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    print(f"Сохранено: {out_path}")
    print(f"Узлов: {len(nodes)}, направленных рёбер: {len(edges)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Конвертер CS2 .nav → CS2-AI navmesh JSON")
    parser.add_argument("--nav", required=True, help="Путь к .nav файлу")
    parser.add_argument("--map", required=True, dest="map_name", help="Имя карты")
    parser.add_argument("--min-dist", type=float, default=180.0, help="Мин. расстояние между узлами (default: 180)")
    parser.add_argument("--out", default=None, help="Путь к выходному JSON")
    args = parser.parse_args()

    nav_path = Path(args.nav)
    if not nav_path.exists():
        sys.exit(f"Файл не найден: {nav_path}")

    out_path = Path(args.out) if args.out else (_NAVMESH_DIR / f"{args.map_name}.json")
    convert(nav_path, args.map_name, args.min_dist, out_path)


if __name__ == "__main__":
    main()
