"""Topological connectivity and Via-Cluster abstraction for inter-metal vias.

Supports Union-Find electrical connectivity resolution across planar traces and
inter-metal vias (detecting net merging when via(s) bridge bottom and top
metal traces) and clusters multi-cut via arrays into single ``ViaCluster`` nodes
to preserve 3D barrel perimeter/area without graph node explosion.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple


@dataclass(frozen=True)
class ViaSpec:
    """Physical stackup and design-rule parameters for a single via layer."""

    name: str
    bot_layer: str
    top_layer: str
    bot_idx: int
    top_idx: int
    layer_pos: float
    gds_layer: int
    gds_datatype: int
    z_min: float
    z_max: float
    cut_width: float
    min_space: float
    enc_bot: float
    enc_top: float
    r_single_ohm: float
    tc1: float

    @property
    def height(self) -> float:
        return round(self.z_max - self.z_min, 4)


SYNTHETIC_VIA_ORDER: Tuple[str, ...] = (
    'VIA1', 'VIA2', 'VIA3', 'VIA4', 'VIA5', 'VIA6', 'VIA7', 'VIA8',
)

# Synthetic demonstration via stackup for Geo-Graph tests; not a foundry process.
SYNTHETIC_VIA_STACK_SPEC: Dict[str, ViaSpec] = {
    'VIA1': ViaSpec('VIA1', 'M1', 'M2', 1, 2, 1.5, 101, 0, 0.500, 0.700, 0.120, 0.080, 0.020, 0.020, 1.00, 1.000e-3),
    'VIA2': ViaSpec('VIA2', 'M2', 'M3', 2, 3, 2.5, 102, 0, 0.900, 1.100, 0.120, 0.080, 0.020, 0.020, 1.00, 1.000e-3),
    'VIA3': ViaSpec('VIA3', 'M3', 'M4', 3, 4, 3.5, 103, 0, 1.300, 1.500, 0.120, 0.080, 0.020, 0.020, 1.00, 1.000e-3),
    'VIA4': ViaSpec('VIA4', 'M4', 'M5', 4, 5, 4.5, 104, 0, 1.700, 1.900, 0.120, 0.080, 0.020, 0.020, 1.00, 1.000e-3),
    'VIA5': ViaSpec('VIA5', 'M5', 'M6', 5, 6, 5.5, 105, 0, 1.900, 2.100, 0.120, 0.080, 0.020, 0.020, 1.00, 1.000e-3),
    'VIA6': ViaSpec('VIA6', 'M6', 'M7', 6, 7, 6.5, 106, 0, 2.500, 2.700, 0.120, 0.080, 0.020, 0.020, 1.00, 1.000e-3),
    'VIA7': ViaSpec('VIA7', 'M7', 'M8', 7, 8, 7.5, 107, 0, 2.900, 3.300, 0.250, 0.250, 0.030, 0.030, 1.00, 1.000e-3),
    'VIA8': ViaSpec('VIA8', 'M8', 'M9', 8, 9, 8.5, 108, 0, 4.300, 4.700, 0.250, 0.250, 0.030, 0.030, 1.00, 1.000e-3),
}

SYNTHETIC_GDS_VIA_MAP: Dict[str, Tuple[int, int]] = {
    vname: (spec.gds_layer, spec.gds_datatype) for vname, spec in SYNTHETIC_VIA_STACK_SPEC.items()
}


def _boxes_overlap(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
    eps: float = 1e-6,
) -> bool:
    """Return True if two axis-aligned bounding boxes have positive 2D intersection area."""
    return (min(a[2], b[2]) - max(a[0], b[0]) > eps) and (
        min(a[3], b[3]) - max(a[1], b[1]) > eps
    )


def _boxes_touch_same_layer(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
    eps: float = 1e-5,
) -> bool:
    """Return True if two same-layer rectangles overlap in 2D or abut along a shared edge > eps."""
    ox = min(a[2], b[2]) - max(a[0], b[0])
    oy = min(a[3], b[3]) - max(a[1], b[1])
    return (ox >= -eps and oy > eps) or (ox > eps and oy >= -eps)


def _boxes_distance(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


@dataclass
class ViaBridgeInfo:
    """Describes a clustered via array bridging a bottom metal segment and a top metal segment."""

    cluster_id: int
    via_layer: str
    net_name: str
    bot_elem_id: int
    top_elem_id: int
    num_cuts: int
    bbox: Tuple[float, float, float, float]

    @property
    def center(self) -> Tuple[float, float]:
        return (0.5 * (self.bbox[0] + self.bbox[2]), 0.5 * (self.bbox[1] + self.bbox[3]))


@dataclass
class ConnectivityResult:
    """Result of physical 3D connectivity resolution and via clustering."""

    clustered_elements: List[object]
    resolved_raw_elements: List[object]
    merged_net_map: Dict[str, List[str]]
    orig_to_canonical_net: Dict[str, str]
    via_bridges: List[ViaBridgeInfo]


def _is_placeholder_net(name: str) -> bool:
    return not name or name.startswith('__AUTO') or name in ('AUTO', 'UNASSIGNED', 'VIA_AUTO')


def resolve_layout_connectivity(
    elements: Sequence[object],
    *,
    via_specs: Optional[Mapping[str, ViaSpec]] = None,
    ground_nets: Tuple[str, ...] = ('VSS', 'GND'),
) -> ConnectivityResult:
    """Resolve physical 3D connectivity across metal rectangles and via cuts.

    Adjacent co-net via cuts are merged into ``ViaCluster`` elements.
    """
    specs = via_specs if via_specs is not None else SYNTHETIC_VIA_STACK_SPEC
    n = len(elements)
    if n == 0:
        return ConnectivityResult([], [], {}, {}, [])

    has_any_via = any(getattr(el, 'is_via', False) for el in elements)
    if not has_any_via:
        els_list = list(elements)
        id_map = {el.net_name: el.net_name for el in els_list if el.net_name}
        return ConnectivityResult(els_list, els_list, {}, id_map, [])

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    metals_by_layer: Dict[str, List[int]] = {}
    via_indices: List[int] = []
    for idx, el in enumerate(elements):
        if getattr(el, 'is_via', False):
            via_indices.append(idx)
        else:
            metals_by_layer.setdefault(el.layer, []).append(idx)

    # 1. Same-layer touching/overlapping or explicitly co-net metal segments
    for _, m_idxs in metals_by_layer.items():
        k_len = len(m_idxs)
        for ii in range(k_len):
            i = m_idxs[ii]
            bi = elements[i].bbox
            ni = elements[i].net_name
            for jj in range(ii + 1, k_len):
                j = m_idxs[jj]
                if ni == elements[j].net_name and not _is_placeholder_net(ni):
                    union(i, j)
                elif _boxes_touch_same_layer(bi, elements[j].bbox):
                    union(i, j)

    explicit_net_first_idx: Dict[str, int] = {}
    for idx, el in enumerate(elements):
        nn = el.net_name
        if not _is_placeholder_net(nn):
            if nn in explicit_net_first_idx:
                union(idx, explicit_net_first_idx[nn])
            else:
                explicit_net_first_idx[nn] = idx

    # 2. Via-to-bottom-metal and via-to-top-metal physical overlap resolution
    via_bot_touch: Dict[int, int] = {}
    via_top_touch: Dict[int, int] = {}

    for v_idx in via_indices:
        vel = elements[v_idx]
        vspec = specs.get(vel.layer)
        if vspec is None:
            continue
        vbbox = vel.bbox

        bot_cands = metals_by_layer.get(vspec.bot_layer, [])
        top_cands = metals_by_layer.get(vspec.top_layer, [])

        matched_bots = [m_i for m_i in bot_cands if _boxes_overlap(vbbox, elements[m_i].bbox)]
        matched_tops = [m_i for m_i in top_cands if _boxes_overlap(vbbox, elements[m_i].bbox)]

        for m_i in matched_bots:
            union(v_idx, m_i)
        for m_i in matched_tops:
            union(v_idx, m_i)

        if matched_bots:
            via_bot_touch[v_idx] = elements[matched_bots[0]].id
        if matched_tops:
            via_top_touch[v_idx] = elements[matched_tops[0]].id

    # 3. Assign canonical net names per connected component
    comp_orig_nets: Dict[int, Set[str]] = {}
    for idx, el in enumerate(elements):
        root = find(idx)
        nn = el.net_name
        if not _is_placeholder_net(nn):
            comp_orig_nets.setdefault(root, set()).add(nn)

    comp_canonical_net: Dict[int, str] = {}
    merged_net_map: Dict[str, List[str]] = {}
    orig_to_canonical: Dict[str, str] = {}

    for idx in range(n):
        root = find(idx)
        if root in comp_canonical_net:
            continue
        names = comp_orig_nets.get(root, set())
        if not names:
            canon = f'NET_AUTO_{root}'
        else:
            ground_match = next((g for g in ground_nets if g in names), None)
            canon = ground_match if ground_match is not None else sorted(names)[0]
        comp_canonical_net[root] = canon
        if len(names) > 1:
            merged_net_map[canon] = sorted(names)
        for orig_n in names:
            orig_to_canonical[orig_n] = canon

    resolved_raw = []
    for idx, el in enumerate(elements):
        canon = comp_canonical_net[find(idx)]
        bot_id = via_bot_touch.get(idx, getattr(el, 'bot_elem_id', -1))
        top_id = via_top_touch.get(idx, getattr(el, 'top_elem_id', -1))
        resolved_raw.append(
            replace(el, net_name=canon, bot_elem_id=bot_id, top_elem_id=top_id)
        )

    # 4. Cluster VIA cuts into ViaCluster elements
    clustered: List[object] = [el for el in resolved_raw if not getattr(el, 'is_via', False)]
    via_resolved = [el for el in resolved_raw if getattr(el, 'is_via', False)]

    m_v = len(via_resolved)
    v_parent = list(range(m_v))

    def v_find(x: int) -> int:
        while v_parent[x] != x:
            v_parent[x] = v_parent[v_parent[x]]
            x = v_parent[x]
        return x

    for a in range(m_v):
        va = via_resolved[a]
        spec_a = specs.get(va.layer)
        if spec_a is None:
            continue
        array_thresh = 2.2 * (spec_a.cut_width + spec_a.min_space)
        fence_thresh = 3.0 * (spec_a.cut_width + spec_a.min_space)
        for b in range(a + 1, m_v):
            vb = via_resolved[b]
            if va.layer != vb.layer or va.net_name != vb.net_name:
                continue
            same_bridge_pair = (
                va.bot_elem_id >= 0
                and va.bot_elem_id == vb.bot_elem_id
                and va.top_elem_id >= 0
                and va.top_elem_id == vb.top_elem_id
            )
            dist_ab = _boxes_distance(va.bbox, vb.bbox)
            if (same_bridge_pair and dist_ab <= array_thresh) or (
                not same_bridge_pair and dist_ab <= fence_thresh
            ):
                ra, rb = v_find(a), v_find(b)
                if ra != rb:
                    v_parent[rb] = ra

    raw_groups: Dict[int, List[object]] = {}
    for idx_v, vel in enumerate(via_resolved):
        raw_groups.setdefault(v_find(idx_v), []).append(vel)

    def _split_noncompact_group(grp: List[object]) -> List[List[object]]:
        """Split hollow L/U/ring via walls into compact 1D bars or dense 2D arrays."""
        if len(grp) <= 1:
            return [grp]
        spec = specs.get(grp[0].layer)
        if spec is None:
            return [grp]
        unit_cell = (spec.cut_width + 2.2 * spec.min_space) ** 2
        total_cuts = sum(max(1, getattr(g, 'num_cuts', 1)) for g in grp)
        cx0 = min(g.x0 for g in grp)
        cy0 = min(g.y0 for g in grp)
        cx1 = max(g.x1 for g in grp)
        cy1 = max(g.y1 for g in grp)
        bw = max(cx1 - cx0, spec.cut_width)
        bh = max(cy1 - cy0, spec.cut_width)
        if min(bw, bh) <= 2.2 * spec.cut_width or (total_cuts * unit_cell) / (bw * bh) >= 0.28:
            return [grp]

        xs = sorted(grp, key=lambda g: 0.5 * (g.x0 + g.x1))
        ys = sorted(grp, key=lambda g: 0.5 * (g.y0 + g.y1))
        best_gap_x, split_ix = -1.0, len(xs) // 2
        for idx_s in range(len(xs) - 1):
            gx = xs[idx_s + 1].x0 - xs[idx_s].x1
            if gx > best_gap_x:
                best_gap_x, split_ix = gx, idx_s + 1
        best_gap_y, split_iy = -1.0, len(ys) // 2
        for idx_s in range(len(ys) - 1):
            gy = ys[idx_s + 1].y0 - ys[idx_s].y1
            if gy > best_gap_y:
                best_gap_y, split_iy = gy, idx_s + 1

        if best_gap_x > best_gap_y + 1e-6 or (abs(best_gap_x - best_gap_y) <= 1e-6 and bw >= bh):
            left, right = xs[:split_ix], xs[split_ix:]
        else:
            left, right = ys[:split_iy], ys[split_iy:]
        return _split_noncompact_group(left) + _split_noncompact_group(right)

    groups_list: List[List[object]] = []
    for rgrp in raw_groups.values():
        groups_list.extend(_split_noncompact_group(rgrp))

    next_id = max((el.id for el in clustered), default=0) + 1
    via_bridges: List[ViaBridgeInfo] = []

    for grp in groups_list:
        first = grp[0]
        total_cuts = sum(max(1, getattr(g, 'num_cuts', 1)) for g in grp)
        cx0 = min(g.x0 for g in grp)
        cy0 = min(g.y0 for g in grp)
        cx1 = max(g.x1 for g in grp)
        cy1 = max(g.y1 for g in grp)
        bot_id = next((g.bot_elem_id for g in grp if g.bot_elem_id >= 0), -1)
        top_id = next((g.top_elem_id for g in grp if g.top_elem_id >= 0), -1)

        cluster_el = replace(
            first,
            id=next_id,
            x0=cx0,
            y0=cy0,
            x1=cx1,
            y1=cy1,
            num_cuts=total_cuts,
            bot_elem_id=bot_id,
            top_elem_id=top_id,
        )
        clustered.append(cluster_el)
        via_bridges.append(
            ViaBridgeInfo(
                cluster_id=next_id,
                via_layer=first.layer,
                net_name=first.net_name,
                bot_elem_id=bot_id,
                top_elem_id=top_id,
                num_cuts=total_cuts,
                bbox=(cluster_el.x0, cluster_el.y0, cluster_el.x1, cluster_el.y1),
            )
        )
        next_id += 1

    return ConnectivityResult(
        clustered_elements=clustered,
        resolved_raw_elements=resolved_raw,
        merged_net_map=merged_net_map,
        orig_to_canonical_net=orig_to_canonical,
        via_bridges=via_bridges,
    )
