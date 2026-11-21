"""Core-Halo spatial/topological partitioning and zero-double-count capacitance conservation (M1).

Splits long-distance 3D interconnects into local Core-Halo windows matching the receptive
field of Geo-Graph neural surrogates while preserving seam abutment deduction, atomic
ViaCluster assignment, zero-double-count ground/coupling attribution, and per-partition
contribution reporting.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import time
from typing import Dict, List, Literal, Mapping, Optional, Sequence, Set, Tuple

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from .analytical_resistance import AnalyticalResistorEngine
from .geometry_graph import (
    RectangleElement,
    StackupConfig,
    decompose_rectilinear_polygon,
    snap_grid,
    synthetic_stackup,
)
from .via_connectivity import _boxes_touch_same_layer, resolve_layout_connectivity

PartitionMode = Literal['auto', 'none', 'path_tiles', 'subregions', 'spatial_grid']
BBox = Tuple[float, float, float, float]


@dataclass(frozen=True)
class NeighborCouplingSummary:
    """Aggregated coupling contribution from one aggressor net/layer within a partition."""

    net: str
    layer: str
    capacitance_fF: float
    min_spacing_um: float

    def to_dict(self) -> Dict[str, object]:
        return {
            'net': self.net,
            'layer': self.layer,
            'capacitance_fF': float(self.capacitance_fF),
            'min_spacing_um': float(self.min_spacing_um),
        }


@dataclass(frozen=True)
class PartitionSpec:
    """Disjoint Core conductor slice paired with its overlapping Halo context."""

    id: str
    index: int
    label: str
    mode: str
    region_id: Optional[str]
    bounds_um: BBox
    halo_bounds_um: BBox
    layers: Tuple[str, ...]
    core_elements: Tuple[RectangleElement, ...]
    halo_target_elements: Tuple[RectangleElement, ...]
    halo_bg_elements: Tuple[RectangleElement, ...]
    seam_abutments: int
    halo_target_owners: Tuple[Tuple[int, int], ...] = ()

    def element_owner_map(self) -> Dict[int, Optional[int]]:
        """Map every element ID inside this window to its owning Core partition or None (foreign)."""
        owners: Dict[int, Optional[int]] = {el.id: self.index for el in self.core_elements}
        for el_id, owner_idx in self.halo_target_owners:
            owners[el_id] = owner_idx
        for el in self.halo_bg_elements:
            owners[el.id] = None
        return owners


@dataclass(frozen=True)
class PartitionContribution:
    """Per-partition capacitance contribution breakdown with strict additive shares."""

    id: str
    index: int
    label: str
    region_id: Optional[str]
    bounds_um: BBox
    layers: Tuple[str, ...]
    ground_capacitance_fF: float
    ground_share: float
    coupling_capacitance_fF: float
    coupling_share: float
    total_capacitance_fF: float
    total_capacitance_share: float
    local_series_resistance_ohm: Optional[float]
    core_element_count: int
    seam_abutments: int
    top_coupling_neighbors: Tuple[NeighborCouplingSummary, ...]

    def to_dict(self) -> Dict[str, object]:
        return {
            'id': self.id,
            'index': self.index,
            'label': self.label,
            'region_id': self.region_id,
            'bounds_um': list(self.bounds_um),
            'layers': list(self.layers),
            'ground_capacitance_fF': float(self.ground_capacitance_fF),
            'ground_share': float(self.ground_share),
            'coupling_capacitance_fF': float(self.coupling_capacitance_fF),
            'coupling_share': float(self.coupling_share),
            'total_capacitance_fF': float(self.total_capacitance_fF),
            'total_capacitance_share': float(self.total_capacitance_share),
            'local_series_resistance_ohm': (
                float(self.local_series_resistance_ohm)
                if self.local_series_resistance_ohm is not None
                else None
            ),
            'core_element_count': self.core_element_count,
            'seam_abutments': self.seam_abutments,
            'top_coupling_neighbors': [n.to_dict() for n in self.top_coupling_neighbors],
        }


@dataclass(frozen=True)
class PartitionedCapResult:
    """Partitioned RC summary preserving conservation across Core slices."""

    partition_mode_used: str
    tile_size_um: float
    r_halo_um: float
    partition_count: int
    ground_capacitance_fF: float
    coupling_capacitance_fF: float
    total_capacitance_fF: float
    bottleneck_partition_id: Optional[str]
    partitions: Tuple[PartitionContribution, ...]
    pair_c_cpl_fF: Dict[Tuple[str, str], float]
    elapsed_ms: float

    def to_dict(self) -> Dict[str, object]:
        sum_cg = sum(p.ground_capacitance_fF for p in self.partitions)
        sum_cc = sum(p.coupling_capacitance_fF for p in self.partitions)
        return {
            'partition_mode_used': self.partition_mode_used,
            'tile_size_um': float(self.tile_size_um),
            'r_halo_um': float(self.r_halo_um),
            'partition_count': self.partition_count,
            'ground_capacitance_fF': float(self.ground_capacitance_fF),
            'coupling_capacitance_fF': float(self.coupling_capacitance_fF),
            'total_capacitance_fF': float(self.total_capacitance_fF),
            'bottleneck_partition_id': self.bottleneck_partition_id,
            'conservation_check': {
                'sum_partition_cg_fF': float(sum_cg),
                'sum_partition_cc_fF': float(sum_cc),
                'sum_partition_ctot_fF': float(sum_cg + sum_cc),
            },
            'partitions': [p.to_dict() for p in self.partitions],
            'pair_c_cpl_fF': {f'{u}|{v}': float(val) for (u, v), val in self.pair_c_cpl_fF.items()},
            'elapsed_ms': float(self.elapsed_ms),
        }


def _bboxes_intersect(a: BBox, b: BBox, pad: float = 0.0) -> bool:
    return (
        a[2] >= b[0] - pad
        and a[0] <= b[2] + pad
        and a[3] >= b[1] - pad
        and a[1] <= b[3] + pad
    )


def _bbox_distance(a: BBox, b: BBox) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _union_bounds(elements: Sequence[RectangleElement]) -> BBox:
    return (
        min(el.x0 for el in elements),
        min(el.y0 for el in elements),
        max(el.x1 for el in elements),
        max(el.y1 for el in elements),
    )


def _decompose_simple_or_holed(poly: Polygon) -> List[BBox]:
    """Slice an orthogonal Shapely Polygon (with optional holes) into disjoint rectangles."""
    if poly.is_empty or poly.area <= 1e-9:
        return []
    if not poly.interiors:
        pts = [(float(x), float(y)) for x, y in list(poly.exterior.coords)[:-1]]
        if pts and all(
            abs(pts[k][0] - pts[(k + 1) % len(pts)][0]) <= 1e-5
            or abs(pts[k][1] - pts[(k + 1) % len(pts)][1]) <= 1e-5
            for k in range(len(pts))
        ):
            rects = decompose_rectilinear_polygon(pts)
            if rects:
                return rects

    rings = [list(poly.exterior.coords)[:-1]] + [
        list(ring.coords)[:-1] for ring in poly.interiors
    ]
    xs_all = sorted({round(float(x), 6) for ring in rings for x, _ in ring})
    if len(xs_all) < 2:
        bx0, by0, bx1, by1 = poly.bounds
        return [(bx0, by0, bx1, by1)] if (bx1 - bx0 > 1e-6 and by1 - by0 > 1e-6) else []

    active: Dict[Tuple[float, float], Tuple[float, float]] = {}
    finished: List[BBox] = []
    for xa, xb in zip(xs_all[:-1], xs_all[1:]):
        if xb - xa <= 1e-6:
            continue
        xm = 0.5 * (xa + xb)
        h_ys: List[float] = []
        for ring in rings:
            m = len(ring)
            for k in range(m):
                x1, y1 = ring[k]
                x2, y2 = ring[(k + 1) % m]
                if (
                    abs(y1 - y2) <= 1e-5
                    and min(x1, x2) <= xm <= max(x1, x2)
                    and abs(x1 - x2) > 1e-6
                ):
                    h_ys.append(0.5 * (float(y1) + float(y2)))
        h_ys.sort()
        y_intervals: List[Tuple[float, float]] = []
        for idx_h in range(0, len(h_ys) - 1, 2):
            ya, yb = h_ys[idx_h], h_ys[idx_h + 1]
            if yb - ya > 1e-6:
                if y_intervals and abs(y_intervals[-1][1] - ya) <= 1e-6:
                    y_intervals[-1] = (y_intervals[-1][0], yb)
                else:
                    y_intervals.append((ya, yb))
        next_active: Dict[Tuple[float, float], Tuple[float, float]] = {}
        for y_iv in y_intervals:
            if y_iv in active and abs(active[y_iv][1] - xa) <= 1e-6:
                next_active[y_iv] = (active[y_iv][0], xb)
            else:
                next_active[y_iv] = (xa, xb)
        for y_iv, (rx0, rx1) in active.items():
            if y_iv not in next_active:
                finished.append((rx0, y_iv[0], rx1, y_iv[1]))
        active = next_active

    for y_iv, (rx0, rx1) in active.items():
        finished.append((rx0, y_iv[0], rx1, y_iv[1]))

    if not finished:
        bx0, by0, bx1, by1 = poly.bounds
        return [(bx0, by0, bx1, by1)] if (bx1 - bx0 > 1e-6 and by1 - by0 > 1e-6) else []
    return finished


def _shapely_to_rects(
    geom,
    layer: str,
    net_name: str,
    stackup: StackupConfig,
    start_id: int,
) -> Tuple[List[RectangleElement], int]:
    if geom.is_empty:
        return [], start_id
    polys = [geom] if geom.geom_type == 'Polygon' else [
        g for g in getattr(geom, 'geoms', ()) if g.geom_type == 'Polygon'
    ]
    out: List[RectangleElement] = []
    next_id = start_id
    grid = stackup.grid_snap_um
    raw_boxes: List[BBox] = []
    for poly in polys:
        raw_boxes.extend(_decompose_simple_or_holed(poly))
    raw_boxes.sort(key=lambda b: (round(b[0], 6), round(b[1], 6), round(b[2], 6), round(b[3], 6)))
    for x0, y0, x1, y1 in raw_boxes:
        sx0, sy0 = snap_grid(x0, grid), snap_grid(y0, grid)
        sx1, sy1 = snap_grid(x1, grid), snap_grid(y1, grid)
        if sx1 - sx0 <= 1e-6 or sy1 - sy0 <= 1e-6:
            continue
        out.append(
            RectangleElement(
                id=next_id,
                net_name=net_name,
                layer=layer,
                x0=sx0,
                y0=sy0,
                x1=sx1,
                y1=sy1,
                stackup=stackup,
            )
        )
        next_id += 1
    return out, next_id


def _canonicalize_planar(
    elements: Sequence[RectangleElement],
    stackup: StackupConfig,
    start_id: int,
) -> Tuple[List[RectangleElement], int]:
    """Merge overlapping same-layer/same-net planar boxes into strictly disjoint rectangles."""
    grouped: Dict[Tuple[str, str], List[RectangleElement]] = {}
    for el in elements:
        grouped.setdefault((el.layer, el.net_name), []).append(el)

    out: List[RectangleElement] = []
    next_id = start_id
    for layer, net_name in sorted(grouped):
        els = grouped[(layer, net_name)]
        if len(els) == 1:
            e0 = els[0]
            out.append(replace(e0, id=next_id, stackup=stackup))
            next_id += 1
            continue
        # Fast path when already disjoint
        already_disjoint = True
        for i in range(len(els)):
            a = els[i]
            for j in range(i + 1, len(els)):
                b = els[j]
                if min(a.x1, b.x1) - max(a.x0, b.x0) > 1e-5 and min(a.y1, b.y1) - max(a.y0, b.y0) > 1e-5:
                    already_disjoint = False
                    break
            if not already_disjoint:
                break
        if already_disjoint:
            for e0 in sorted(els, key=lambda r: (r.x0, r.y0, r.x1, r.y1)):
                out.append(replace(e0, id=next_id, stackup=stackup))
                next_id += 1
            continue
        merged = unary_union([box(*e.bbox) for e in els])
        rects, next_id = _shapely_to_rects(merged, layer, net_name, stackup, next_id)
        out.extend(rects)
    return out, next_id


def _split_long_planar_element(
    el: RectangleElement,
    tile_size_um: float,
    same_net_vias: Sequence[RectangleElement],
    stackup: StackupConfig,
    start_id: int,
) -> Tuple[List[RectangleElement], int]:
    """Split a long planar trace along its major axis while avoiding via landing zones."""
    if el.is_via or el.length <= 1.35 * tile_size_um:
        return [replace(el, id=start_id, stackup=stackup)], start_id + 1

    grid = stackup.grid_snap_um
    along_x = (el.x1 - el.x0) >= (el.y1 - el.y0)
    lo, hi = (el.x0, el.x1) if along_x else (el.y0, el.y1)
    span = hi - lo
    n_seg = max(2, int(round(span / tile_size_um)))
    min_seg = max(0.15, 2.0 * grid)
    if span / n_seg < min_seg:
        n_seg = max(1, int(span // min_seg))
    if n_seg <= 1:
        return [replace(el, id=start_id, stackup=stackup)], start_id + 1

    # Gather bounding intervals of touching same-net ViaClusters along the cut axis
    touching_via_spans: List[Tuple[float, float]] = []
    for vel in same_net_vias:
        vspec = stackup.via_stack_spec.get(vel.layer)
        if vspec is None or el.layer_idx not in (vspec.bot_idx, vspec.top_idx):
            continue
        if _bboxes_intersect(el.bbox, vel.bbox, pad=1e-5):
            touching_via_spans.append((vel.x0, vel.x1) if along_x else (vel.y0, vel.y1))

    cuts = [lo]
    margin = 0.15
    for idx_c in range(1, n_seg):
        raw_c = snap_grid(lo + span * (idx_c / n_seg), grid)
        c = raw_c
        for v0, v1 in touching_via_spans:
            if v0 - margin < c < v1 + margin:
                left = snap_grid(v0 - margin, grid)
                right = snap_grid(v1 + margin, grid)
                candidates = []
                if left > cuts[-1] + grid and left < hi - grid:
                    candidates.append((abs(left - raw_c), left))
                if right > cuts[-1] + grid and right < hi - grid:
                    candidates.append((abs(right - raw_c), right))
                if candidates:
                    c = min(candidates)[1]
                break
        if c > cuts[-1] + min_seg * 0.25 and hi - c > min_seg * 0.25:
            cuts.append(c)
    cuts.append(hi)

    pieces: List[RectangleElement] = []
    next_id = start_id
    for ca, cb in zip(cuts[:-1], cuts[1:]):
        if cb - ca <= 1e-6:
            continue
        pieces.append(
            RectangleElement(
                id=next_id,
                net_name=el.net_name,
                layer=el.layer,
                x0=ca if along_x else el.x0,
                y0=el.y0 if along_x else ca,
                x1=cb if along_x else el.x1,
                y1=el.y1 if along_x else cb,
                stackup=stackup,
            )
        )
        next_id += 1
    if not pieces:
        return [replace(el, id=start_id, stackup=stackup)], start_id + 1
    return pieces, next_id


def _assign_via_to_planar_tiles(
    vias: Sequence[RectangleElement],
    tiles: List[List[RectangleElement]],
    stackup: StackupConfig,
) -> None:
    """Assign each atomic ViaCluster to a touching same-net planar tile."""
    if not vias or not tiles:
        return
    tile_planar: List[List[RectangleElement]] = [
        [el for el in tile if not el.is_via] for tile in tiles
    ]
    for vel in vias:
        vspec = stackup.via_stack_spec.get(vel.layer)
        bot_match: Optional[int] = None
        top_match: Optional[int] = None
        best_dist = float('inf')
        nearest_tile = 0
        for tid, p_els in enumerate(tile_planar):
            for el in p_els:
                if el.net_name != vel.net_name:
                    continue
                d = _bbox_distance(el.bbox, vel.bbox)
                if d < best_dist:
                    best_dist = d
                    nearest_tile = tid
                if vspec is not None and _bboxes_intersect(el.bbox, vel.bbox, pad=1e-5):
                    if el.layer_idx == vspec.bot_idx and bot_match is None:
                        bot_match = tid
                    elif el.layer_idx == vspec.top_idx and top_match is None:
                        top_match = tid
        chosen = bot_match if bot_match is not None else (top_match if top_match is not None else nearest_tile)
        tiles[chosen].append(vel)


def _group_path_tiles(
    planar_pieces: Sequence[RectangleElement],
    via_clusters: Sequence[RectangleElement],
    tile_size_um: float,
    stackup: StackupConfig,
) -> List[Tuple[str, Optional[str], List[RectangleElement]]]:
    """Walk the 3D planar+via connectivity graph and form contiguous path tiles."""
    m = len(planar_pieces)
    if m == 0:
        return [('P0', None, list(via_clusters))] if via_clusters else []

    adj: List[Set[int]] = [set() for _ in range(m)]
    by_layer_net: Dict[Tuple[str, str], List[int]] = {}
    for idx, el in enumerate(planar_pieces):
        by_layer_net.setdefault((el.layer, el.net_name), []).append(idx)

    for indices in by_layer_net.values():
        k_len = len(indices)
        for a_i in range(k_len):
            ia = indices[a_i]
            ba = planar_pieces[ia].bbox
            for b_i in range(a_i + 1, k_len):
                ib = indices[b_i]
                if _boxes_touch_same_layer(ba, planar_pieces[ib].bbox):
                    adj[ia].add(ib)
                    adj[ib].add(ia)

    # Bridge across touching ViaClusters so cross-layer traces stay topologically linked
    for vel in via_clusters:
        vspec = stackup.via_stack_spec.get(vel.layer)
        if vspec is None:
            continue
        bots = [
            i for i in by_layer_net.get((vspec.bot_layer, vel.net_name), ())
            if _bboxes_intersect(planar_pieces[i].bbox, vel.bbox, pad=1e-5)
        ]
        tops = [
            i for i in by_layer_net.get((vspec.top_layer, vel.net_name), ())
            if _bboxes_intersect(planar_pieces[i].bbox, vel.bbox, pad=1e-5)
        ]
        for bi in bots:
            for ti in tops:
                adj[bi].add(ti)
                adj[ti].add(bi)

    # Connected components sorted deterministically by spatial minimum
    visited = [False] * m
    components: List[List[int]] = []
    for i in range(m):
        if visited[i]:
            continue
        stack = [i]
        comp = []
        visited[i] = True
        while stack:
            u = stack.pop()
            comp.append(u)
            for v in adj[u]:
                if not visited[v]:
                    visited[v] = True
                    stack.append(v)
        components.append(comp)

    components.sort(
        key=lambda comp: min(
            (planar_pieces[i].net_name, planar_pieces[i].layer_idx, planar_pieces[i].x0, planar_pieces[i].y0)
            for i in comp
        )
    )

    tiles_planar: List[List[int]] = []
    max_tile_len = 1.25 * tile_size_um
    min_tile_len = 0.55 * tile_size_um
    tiny_len = 0.35 * tile_size_um

    for comp in components:
        comp_set = set(comp)
        endpoints = [i for i in comp if len(adj[i]) <= 1]
        seeds = endpoints if endpoints else comp
        start = min(
            seeds,
            key=lambda i: (planar_pieces[i].layer_idx, planar_pieces[i].x0, planar_pieces[i].y0),
        )
        walked: Set[int] = set()
        order: List[int] = []
        dfs = [start]
        while dfs:
            cur = dfs.pop()
            if cur in walked:
                continue
            walked.add(cur)
            order.append(cur)
            nxt = sorted(
                (v for v in adj[cur] if v in comp_set and v not in walked),
                key=lambda v: (
                    0 if planar_pieces[v].layer_idx == planar_pieces[cur].layer_idx else 1,
                    _bbox_distance(planar_pieces[v].bbox, planar_pieces[cur].bbox),
                    planar_pieces[v].x0,
                    planar_pieces[v].y0,
                ),
                reverse=True,
            )
            dfs.extend(nxt)

        comp_tiles: List[List[int]] = []
        cur_tile: List[int] = []
        cur_len = 0.0
        for idx in order:
            p_el = planar_pieces[idx]
            seg_len = p_el.length
            prev_idx = cur_tile[-1] if cur_tile else None
            contiguous = prev_idx is None or idx in adj[prev_idx]
            if cur_tile and (
                not contiguous
                or (cur_len >= min_tile_len and cur_len + seg_len > max_tile_len)
                or (seg_len >= 0.85 * tile_size_um and cur_len >= 0.85 * tile_size_um)
            ):
                comp_tiles.append(cur_tile)
                cur_tile = [idx]
                cur_len = seg_len
            else:
                cur_tile.append(idx)
                cur_len += seg_len
        if cur_tile:
            comp_tiles.append(cur_tile)

        # Merge trailing tiny fragment into adjacent connected tile
        if len(comp_tiles) > 1:
            last_len = sum(planar_pieces[i].length for i in comp_tiles[-1])
            if last_len < tiny_len:
                last_set = set(comp_tiles[-1])
                for prev_t in reversed(comp_tiles[:-1]):
                    if any(v in prev_t for u in last_set for v in adj[u]):
                        prev_t.extend(comp_tiles[-1])
                        comp_tiles.pop()
                        break

        tiles_planar.extend(comp_tiles)

    tiles: List[List[RectangleElement]] = [[planar_pieces[i] for i in idxs] for idxs in tiles_planar]
    _assign_via_to_planar_tiles(via_clusters, tiles, stackup)

    return [
        (f'P{idx}', None, tile)
        for idx, tile in enumerate(tiles)
        if tile
    ]


def _group_subregions(
    target_planar: Sequence[RectangleElement],
    target_vias: Sequence[RectangleElement],
    subregion_boxes: Mapping[str, BBox],
    tile_size_um: float,
    stackup: StackupConfig,
    next_id: int,
) -> Tuple[List[Tuple[str, Optional[str], List[RectangleElement]]], int]:
    """Partition target geometry cleanly along subregion footprints plus remainder corridors."""
    groups: List[Tuple[str, Optional[str], List[RectangleElement]]] = []
    remaining_by_key: Dict[Tuple[str, str], object] = {}
    for el in target_planar:
        key = (el.layer, el.net_name)
        g = box(*el.bbox)
        remaining_by_key[key] = g if key not in remaining_by_key else remaining_by_key[key].union(g)

    def _subdivide_list(els: List[RectangleElement]) -> List[RectangleElement]:
        nonlocal next_id
        out: List[RectangleElement] = []
        for item in els:
            pieces, next_id = _split_long_planar_element(
                item, tile_size_um, target_vias, stackup, next_id
            )
            out.extend(pieces)
        return out

    claimed_vias: Set[int] = set()
    p_idx = 0
    for reg_id, rbox in subregion_boxes.items():
        rx0, ry0, rx1, ry1 = rbox
        reg_poly = box(rx0, ry0, rx1, ry1)
        reg_els: List[RectangleElement] = []
        for key in sorted(remaining_by_key):
            layer, net_name = key
            rem = remaining_by_key[key]
            if rem.is_empty:
                continue
            clipped = rem.intersection(reg_poly)
            if clipped.is_empty or clipped.area <= 1e-8:
                continue
            rects, next_id = _shapely_to_rects(clipped, layer, net_name, stackup, next_id)
            reg_els.extend(_subdivide_list(rects))
            remaining_by_key[key] = rem.difference(reg_poly)

        reg_vias = [
            vel for vel in target_vias
            if vel.id not in claimed_vias
            and rx0 - 1e-6 <= vel.center[0] <= rx1 + 1e-6
            and ry0 - 1e-6 <= vel.center[1] <= ry1 + 1e-6
        ]
        for vel in reg_vias:
            claimed_vias.add(vel.id)
        if reg_els or reg_vias:
            groups.append((f'P{p_idx}', reg_id, reg_els + reg_vias))
            p_idx += 1

    # Remaining corridor outside subregions
    corr_planar: List[RectangleElement] = []
    for key in sorted(remaining_by_key):
        layer, net_name = key
        rem = remaining_by_key[key]
        if rem.is_empty or rem.area <= 1e-8:
            continue
        rects, next_id = _shapely_to_rects(rem, layer, net_name, stackup, next_id)
        corr_planar.extend(_subdivide_list(rects))

    corr_vias = [vel for vel in target_vias if vel.id not in claimed_vias]
    if corr_planar or corr_vias:
        corr_tiles = _group_path_tiles(corr_planar, corr_vias, tile_size_um, stackup)
        for label, _, tile_els in corr_tiles:
            del label
            groups.append((f'P{p_idx} (corridor)', None, tile_els))
            p_idx += 1
    return groups, next_id


def _group_spatial_grid(
    target_planar: Sequence[RectangleElement],
    target_vias: Sequence[RectangleElement],
    tile_size_um: float,
    stackup: StackupConfig,
    next_id: int,
) -> Tuple[List[Tuple[str, Optional[str], List[RectangleElement]]], int]:
    """Slice target conductors against an axis-aligned 2D tile grid (disjoint Core)."""
    all_els = list(target_planar) + list(target_vias)
    if not all_els:
        return [], next_id
    gx0, gy0, gx1, gy1 = _union_bounds(all_els)
    grid = stackup.grid_snap_um
    nx = max(1, int(round((gx1 - gx0) / tile_size_um)))
    ny = max(1, int(round((gy1 - gy0) / tile_size_um)))

    x_cuts = [gx0] + [snap_grid(gx0 + (gx1 - gx0) * i / nx, grid) for i in range(1, nx)] + [gx1]
    y_cuts = [gy0] + [snap_grid(gy0 + (gy1 - gy0) * j / ny, grid) for j in range(1, ny)] + [gy1]
    x_cuts = sorted(set(x_cuts))
    y_cuts = sorted(set(y_cuts))

    cells: Dict[Tuple[int, int], List[RectangleElement]] = {}
    planar_polys = [(el, box(*el.bbox)) for el in target_planar]
    for ix, (xa, xb) in enumerate(zip(x_cuts[:-1], x_cuts[1:])):
        if xb - xa <= 1e-6:
            continue
        for iy, (ya, yb) in enumerate(zip(y_cuts[:-1], y_cuts[1:])):
            if yb - ya <= 1e-6:
                continue
            cell_box = box(xa, ya, xb, yb)
            cell_els: List[RectangleElement] = []
            for el, el_poly in planar_polys:
                if not _bboxes_intersect(el.bbox, (xa, ya, xb, yb), pad=1e-6):
                    continue
                inter = el_poly.intersection(cell_box)
                if inter.is_empty or inter.area <= 1e-8:
                    continue
                rects, next_id = _shapely_to_rects(inter, el.layer, el.net_name, stackup, next_id)
                cell_els.extend(rects)
            if cell_els:
                cells[(ix, iy)] = cell_els

    # Merge tiny spatial slivers (< 0.25 * tile_size_um) into nearest occupied neighbor cell
    min_cell_len = 0.25 * tile_size_um
    while len(cells) > 1:
        tiny_key = next(
            (
                k_ij
                for k_ij in sorted(cells)
                if sum(el.length for el in cells[k_ij] if not el.is_via) < min_cell_len
            ),
            None,
        )
        if tiny_key is None:
            break
        tiny_els = cells.pop(tiny_key)
        tiny_box = _union_bounds(tiny_els)
        best_target = min(
            sorted(cells),
            key=lambda cand: (_bbox_distance(tiny_box, _union_bounds(cells[cand])), cand),
        )
        cells[best_target].extend(tiny_els)

    # Assign vias atomically by center into occupied grid cell
    cell_keys = sorted(cells)
    for vel in target_vias:
        vx, vy = vel.center
        best_key = None
        for ix, iy in cell_keys:
            if x_cuts[ix] - 1e-6 <= vx <= x_cuts[ix + 1] + 1e-6 and y_cuts[iy] - 1e-6 <= vy <= y_cuts[iy + 1] + 1e-6:
                best_key = (ix, iy)
                break
        if best_key is None and cell_keys:
            best_key = min(
                cell_keys,
                key=lambda k_ij: math.hypot(
                    0.5 * (x_cuts[k_ij[0]] + x_cuts[k_ij[0] + 1]) - vx,
                    0.5 * (y_cuts[k_ij[1]] + y_cuts[k_ij[1] + 1]) - vy,
                ),
            )
        if best_key is not None:
            cells[best_key].append(vel)

    out = [
        (f'G({ix},{iy})', None, cells[(ix, iy)])
        for ix, iy in sorted(cells)
    ]
    return out, next_id


def _clip_element_to_box(
    el: RectangleElement,
    clip_box: BBox,
    stackup: StackupConfig,
    new_id: int,
) -> Optional[RectangleElement]:
    """Clip a planar background/halo rectangle to ``clip_box``; keep ViaClusters atomic."""
    if not _bboxes_intersect(el.bbox, clip_box, pad=1e-5):
        return None
    if el.is_via:
        return replace(el, id=new_id, stackup=stackup)
    grid = stackup.grid_snap_um
    nx0 = max(el.x0, snap_grid(clip_box[0], grid))
    ny0 = max(el.y0, snap_grid(clip_box[1], grid))
    nx1 = min(el.x1, snap_grid(clip_box[2], grid))
    ny1 = min(el.y1, snap_grid(clip_box[3], grid))
    if nx1 - nx0 <= 1e-6 or ny1 - ny0 <= 1e-6:
        return None
    return RectangleElement(
        id=new_id,
        net_name=el.net_name,
        layer=el.layer,
        x0=nx0,
        y0=ny0,
        x1=nx1,
        y1=ny1,
        stackup=stackup,
    )


def build_core_halo_partitions(
    target_elements: Sequence[RectangleElement],
    neighbor_elements: Sequence[RectangleElement] = (),
    *,
    partition_mode: PartitionMode = 'auto',
    tile_size_um: float = 20.0,
    auto_threshold_um: float = 30.0,
    r_halo_um: float = 12.0,
    layer_window: int = 2,
    subregion_boxes: Optional[Mapping[str, BBox]] = None,
    resolve_vias: bool = True,
    stackup: Optional[StackupConfig] = None,
) -> Tuple[str, List[PartitionSpec]]:
    """Build disjoint Core + Halo partition windows without splitting ViaClusters.

    Returns ``(partition_mode_used, list_of_PartitionSpec)``.
    """
    del layer_window
    if not (8.0 <= tile_size_um <= 100.0):
        raise ValueError(f'tile_size_um must be in [8.0, 100.0], got {tile_size_um}')
    if r_halo_um <= 0.0:
        raise ValueError(f'r_halo_um must be positive, got {r_halo_um}')

    cfg = stackup or (
        target_elements[0].stackup if target_elements and target_elements[0].stackup else synthetic_stackup()
    )
    if not target_elements:
        return ('single' if partition_mode in ('auto', 'none') else partition_mode), []

    # Re-index globally before connectivity resolution so IDs never collide
    target_raw = [replace(el, id=i + 1, stackup=cfg) for i, el in enumerate(target_elements)]
    target_id_set = {el.id for el in target_raw}
    offset = len(target_raw) + 1
    neighbor_raw = [replace(el, id=offset + i, stackup=cfg) for i, el in enumerate(neighbor_elements)]

    if resolve_vias and any(el.is_via for el in (*target_raw, *neighbor_raw)):
        conn = resolve_layout_connectivity(
            target_raw + neighbor_raw,
            via_specs=cfg.via_stack_spec,
            ground_nets=cfg.ground_nets,
        )
        all_els = conn.clustered_elements
        target_clustered: List[RectangleElement] = []
        neighbor_clustered: List[RectangleElement] = []
        target_vias_raw = [el for el in conn.resolved_raw_elements if el.is_via and el.id in target_id_set]
        for el in all_els:
            if not el.is_via:
                (target_clustered if el.id in target_id_set else neighbor_clustered).append(el)
            else:
                if el.id in target_id_set or any(
                    _bboxes_intersect(el.bbox, tv.bbox, pad=1e-4) and el.net_name == tv.net_name and el.layer == tv.layer
                    for tv in target_vias_raw
                ):
                    target_clustered.append(el)
                else:
                    neighbor_clustered.append(el)
    else:
        target_clustered = target_raw
        neighbor_clustered = neighbor_raw

    target_planar_raw = [el for el in target_clustered if not el.is_via]
    target_vias = [el for el in target_clustered if el.is_via]
    neighbor_planar_raw = [el for el in neighbor_clustered if not el.is_via]
    neighbor_vias = [el for el in neighbor_clustered if el.is_via]

    next_id = 1
    target_planar, next_id = _canonicalize_planar(target_planar_raw, cfg, next_id)
    target_vias = [replace(v, id=next_id + idx, stackup=cfg) for idx, v in enumerate(target_vias)]
    next_id += len(target_vias)

    neighbor_planar, next_id = _canonicalize_planar(neighbor_planar_raw, cfg, next_id)
    neighbor_vias = [replace(v, id=next_id + idx, stackup=cfg) for idx, v in enumerate(neighbor_vias)]
    next_id += len(neighbor_vias)
    all_neighbors = neighbor_planar + neighbor_vias

    bx0, by0, bx1, by1 = _union_bounds(target_planar + target_vias)
    span_um = max(bx1 - bx0, by1 - by0)
    total_planar_len = sum(el.length for el in target_planar)

    if partition_mode == 'none' or (
        partition_mode == 'auto'
        and not subregion_boxes
        and span_um <= auto_threshold_um
        and total_planar_len <= 1.5 * tile_size_um
    ):
        mode_used = 'single'
    elif partition_mode == 'auto' and subregion_boxes:
        mode_used = 'subregions'
    elif partition_mode == 'auto':
        mode_used = 'path_tiles'
    else:
        mode_used = partition_mode

    if mode_used == 'single':
        raw_groups: List[Tuple[str, Optional[str], List[RectangleElement]]] = [
            ('P0', None, target_planar + target_vias)
        ]
    elif mode_used == 'path_tiles':
        split_planar: List[RectangleElement] = []
        for el in target_planar:
            pieces, next_id = _split_long_planar_element(el, tile_size_um, target_vias, cfg, next_id)
            split_planar.extend(pieces)
        raw_groups = _group_path_tiles(split_planar, target_vias, tile_size_um, cfg)
    elif mode_used == 'subregions':
        if not subregion_boxes:
            raise ValueError("partition_mode='subregions' requires non-empty subregion_boxes")
        raw_groups, next_id = _group_subregions(
            target_planar, target_vias, subregion_boxes, tile_size_um, cfg, next_id
        )
    elif mode_used == 'spatial_grid':
        raw_groups, next_id = _group_spatial_grid(
            target_planar, target_vias, tile_size_um, cfg, next_id
        )
    else:
        raise ValueError(f'Unsupported partition_mode: {partition_mode}')

    # Assign unique final Core IDs across partitions
    core_groups: List[Tuple[str, Optional[str], Tuple[RectangleElement, ...]]] = []
    core_owner: Dict[int, int] = {}
    for p_idx, (label, reg_id, grp_els) in enumerate(raw_groups):
        reindexed: List[RectangleElement] = []
        for el in grp_els:
            rel = replace(el, id=next_id, stackup=cfg)
            next_id += 1
            reindexed.append(rel)
            core_owner[rel.id] = p_idx
        core_groups.append((label, reg_id, tuple(reindexed)))

    all_core_flat = [el for _, _, grp in core_groups for el in grp]

    # Assemble per-partition Halo target extensions + clipped foreign/ground Halo background
    specs: List[PartitionSpec] = []
    halo_id_counter = max(next_id + 1000, 1_000_000)
    for k, (label, reg_id, core_els) in enumerate(core_groups):
        bounds = _union_bounds(core_els)
        halo_bounds = (
            bounds[0] - r_halo_um,
            bounds[1] - r_halo_um,
            bounds[2] + r_halo_um,
            bounds[3] + r_halo_um,
        )

        halo_targets: List[RectangleElement] = []
        halo_target_owners: List[Tuple[int, int]] = []
        for other_el in all_core_flat:
            owner_m = core_owner[other_el.id]
            if owner_m == k:
                continue
            clipped = _clip_element_to_box(other_el, halo_bounds, cfg, halo_id_counter)
            if clipped is not None:
                halo_id_counter += 1
                halo_targets.append(clipped)
                halo_target_owners.append((clipped.id, owner_m))

        seam_abutments = 0
        for cel in core_els:
            if cel.is_via:
                continue
            for hel in halo_targets:
                if (
                    not hel.is_via
                    and hel.net_name == cel.net_name
                    and hel.layer == cel.layer
                    and _boxes_touch_same_layer(cel.bbox, hel.bbox)
                ):
                    seam_abutments += 1

        halo_bg: List[RectangleElement] = []
        for nbr in all_neighbors:
            clipped_nbr = _clip_element_to_box(nbr, halo_bounds, cfg, halo_id_counter)
            if clipped_nbr is not None:
                halo_id_counter += 1
                halo_bg.append(clipped_nbr)

        layers = tuple(dict.fromkeys(el.layer for el in core_els))
        specs.append(
            PartitionSpec(
                id=f'P{k}',
                index=k,
                label=label,
                mode=mode_used,
                region_id=reg_id,
                bounds_um=bounds,
                halo_bounds_um=halo_bounds,
                layers=layers,
                core_elements=core_els,
                halo_target_elements=tuple(halo_targets),
                halo_bg_elements=tuple(halo_bg),
                seam_abutments=seam_abutments,
                halo_target_owners=tuple(halo_target_owners),
            )
        )

    return mode_used, specs


def _local_series_resistance(
    core_els: Sequence[RectangleElement],
    cfg: StackupConfig,
    temp_c: float,
) -> Optional[float]:
    if not core_els:
        return None
    total = 0.0
    for el in core_els:
        if el.is_via:
            total += AnalyticalResistorEngine.calc_via_cluster_r(
                el.layer,
                num_cuts=el.num_cuts,
                temp_c=temp_c,
                stackup=cfg,
            )
        else:
            total += AnalyticalResistorEngine.calc_wire_r(
                el.layer,
                width_um=max(el.width, cfg.grid_snap_um),
                length_um=max(el.length, cfg.grid_snap_um),
                num_corners=0,
                temp_c=temp_c,
                stackup=cfg,
            )
    return float(total)


def evaluate_partitioned_capacitance(
    engine,
    target_elements: Sequence[RectangleElement],
    neighbor_elements: Sequence[RectangleElement] = (),
    *,
    partition_mode: PartitionMode = 'auto',
    tile_size_um: float = 20.0,
    auto_threshold_um: float = 30.0,
    subregion_boxes: Optional[Mapping[str, BBox]] = None,
    resolve_vias: bool = True,
    temp_c: float = 25.0,
) -> PartitionedCapResult:
    """Evaluate local RC capacitance across Core-Halo partitions with zero double counting."""
    t0 = time.perf_counter()
    cfg = engine.stackup
    ground_nets = set(cfg.ground_nets)
    mode_used, specs = build_core_halo_partitions(
        target_elements,
        neighbor_elements,
        partition_mode=partition_mode,
        tile_size_um=tile_size_um,
        auto_threshold_um=auto_threshold_um,
        r_halo_um=engine.extractor.r_halo_um,
        layer_window=engine.extractor.layer_window,
        subregion_boxes=subregion_boxes,
        resolve_vias=resolve_vias,
        stackup=cfg,
    )

    if not specs:
        return PartitionedCapResult(
            partition_mode_used=mode_used,
            tile_size_um=tile_size_um,
            r_halo_um=engine.extractor.r_halo_um,
            partition_count=0,
            ground_capacitance_fF=0.0,
            coupling_capacitance_fF=0.0,
            total_capacitance_fF=0.0,
            bottleneck_partition_id=None,
            partitions=(),
            pair_c_cpl_fF={},
            elapsed_ms=0.0,
        )

    raw_rows: List[Tuple[PartitionSpec, float, float, Optional[float], Dict[Tuple[str, str], Tuple[float, float]]]] = []
    pair_totals: Dict[Tuple[str, str], float] = {}
    layer_window = engine.extractor.layer_window

    for spec in specs:
        owner_map = spec.element_owner_map()
        core_ids = {el.id for el in spec.core_elements}
        lo_pos = min(el.layer_pos for el in spec.core_elements) - layer_window - 0.55
        hi_pos = max(el.layer_pos for el in spec.core_elements) + layer_window + 0.55

        bg_els = list(spec.core_elements) + list(spec.halo_target_elements) + list(spec.halo_bg_elements)
        active_els = (
            list(spec.core_elements)
            + [el for el in spec.halo_target_elements if lo_pos <= el.layer_pos <= hi_pos]
            + [el for el in spec.halo_bg_elements if lo_pos <= el.layer_pos <= hi_pos]
        )

        pred = engine.predict_window_rc(
            active_els,
            bg_elements=bg_els,
            resolve_vias=False,
            compute_r_nets=(),
            temp_c=temp_c,
            partition_owner_by_id=owner_map if mode_used != 'single' else None,
        )
        out_els = pred['elements']
        elem_cg = pred['element_c_gnd']
        elem_cpl = pred['element_pair_c_cpl']

        cg_k = 0.0
        for idx_el, el in enumerate(out_els):
            if el.id in core_ids and el.net_name not in ground_nets:
                cg_k += float(elem_cg[idx_el])

        cc_k = 0.0
        nbr_agg: Dict[Tuple[str, str], Tuple[float, float]] = {}
        for (u_i, v_i), c_val in elem_cpl.items():
            el_u, el_v = out_els[u_i], out_els[v_i]
            ou = owner_map.get(el_u.id)
            ov = owner_map.get(el_v.id)
            if ou != spec.index and ov != spec.index:
                continue
            # Exactly one partition owns foreign/same-partition coupling; cross-partition
            # target pairs split 0.5 + 0.5 across the two owning Core partitions.
            if (ou == spec.index and ov is None) or (ov == spec.index and ou is None) or (ou == spec.index and ov == spec.index):
                weight = 1.0
            else:
                weight = 0.5
            contrib = weight * float(c_val)
            cc_k += contrib

            src_el, dst_el = (el_u, el_v) if ou == spec.index else (el_v, el_u)
            nkey = tuple(sorted((src_el.net_name, dst_el.net_name)))
            pair_totals[nkey] = pair_totals.get(nkey, 0.0) + contrib

            sp = _bbox_distance(src_el.bbox, dst_el.bbox)
            agg_k = (dst_el.net_name, dst_el.layer)
            if agg_k in nbr_agg:
                prev_c, prev_sp = nbr_agg[agg_k]
                nbr_agg[agg_k] = (prev_c + contrib, min(prev_sp, sp))
            else:
                nbr_agg[agg_k] = (contrib, sp)

        r_local = _local_series_resistance(spec.core_elements, cfg, temp_c)
        raw_rows.append((spec, cg_k, cc_k, r_local, nbr_agg))

    total_cg = sum(row[1] for row in raw_rows)
    total_cc = sum(row[2] for row in raw_rows)
    total_ctot = total_cg + total_cc

    contributions: List[PartitionContribution] = []
    bottleneck_id: Optional[str] = None
    best_ctot = -1.0
    for spec, cg_k, cc_k, r_local, nbr_agg in raw_rows:
        ctot_k = cg_k + cc_k
        if ctot_k > best_ctot:
            best_ctot = ctot_k
            bottleneck_id = spec.id
        top_nbrs = tuple(
            NeighborCouplingSummary(
                net=net_n,
                layer=layer_n,
                capacitance_fF=c_sum,
                min_spacing_um=sp_min,
            )
            for (net_n, layer_n), (c_sum, sp_min) in sorted(
                nbr_agg.items(), key=lambda item: (-item[1][0], item[0][0], item[0][1])
            )[:5]
            if c_sum > 0.0
        )
        contributions.append(
            PartitionContribution(
                id=spec.id,
                index=spec.index,
                label=spec.label,
                region_id=spec.region_id,
                bounds_um=spec.bounds_um,
                layers=spec.layers,
                ground_capacitance_fF=cg_k,
                ground_share=(cg_k / total_cg) if total_cg > 1e-12 else 0.0,
                coupling_capacitance_fF=cc_k,
                coupling_share=(cc_k / total_cc) if total_cc > 1e-12 else 0.0,
                total_capacitance_fF=ctot_k,
                total_capacitance_share=(ctot_k / total_ctot) if total_ctot > 1e-12 else 0.0,
                local_series_resistance_ohm=r_local,
                core_element_count=len(spec.core_elements),
                seam_abutments=spec.seam_abutments,
                top_coupling_neighbors=top_nbrs,
            )
        )

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return PartitionedCapResult(
        partition_mode_used=mode_used,
        tile_size_um=tile_size_um,
        r_halo_um=engine.extractor.r_halo_um,
        partition_count=len(contributions),
        ground_capacitance_fF=total_cg,
        coupling_capacitance_fF=total_cc,
        total_capacitance_fF=total_ctot,
        bottleneck_partition_id=bottleneck_id,
        partitions=tuple(contributions),
        pair_c_cpl_fF=pair_totals,
        elapsed_ms=elapsed_ms,
    )
