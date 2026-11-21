"""Geometry and E(2)-invariant Geo-Graph feature extraction for neural RC surrogates.

Extracts 22-dimensional node and edge features (first 18 dims compatible with v1
planar models; dims 18..21 encode Via-Cluster geometry, same-net vertical
self-shielding, 3D via-barrel sidewall coupling, and Via-Fence lateral shielding)
within a vertical sliding window and horizontal Halo cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import numpy as np

from .via_connectivity import SYNTHETIC_VIA_STACK_SPEC, ViaSpec

EPS0_FF_PER_UM = 8.854187817e-3  # Vacuum permittivity in fF / um
DEFAULT_GRID_SNAP_UM = 0.001

SYNTHETIC_LAYER_ORDER: Tuple[str, ...] = (
    'PO', 'M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'M7', 'M8', 'M9', 'AP',
)

# (z_min, z_max, thickness in um, Rsh in Ohm/sq)
SYNTHETIC_LAYER_STACK_SPEC: Dict[str, Tuple[float, float, float, float]] = {
    'PO': (0.000, 0.200, 0.200, 10.0000),
    'M1': (0.300, 0.500, 0.200, 0.1000),
    'M2': (0.700, 0.900, 0.200, 0.1000),
    'M3': (1.100, 1.300, 0.200, 0.1000),
    'M4': (1.500, 1.700, 0.200, 0.1000),
    'M5': (1.900, 2.100, 0.200, 0.1000),
    'M6': (2.300, 2.500, 0.200, 0.1000),
    'M7': (2.700, 2.900, 0.200, 0.1000),
    'M8': (3.300, 4.300, 1.000, 0.0200),
    'M9': (4.700, 7.700, 3.000, 0.0040),
    'AP': (8.500, 10.000, 1.500, 0.0200),
}

SYNTHETIC_RSH_OHM_SQ: Dict[str, float] = {
    'PO': 10.0000,
    'M1': 0.1000,
    'M2': 0.1000,
    'M3': 0.1000,
    'M4': 0.1000,
    'M5': 0.1000,
    'M6': 0.1000,
    'M7': 0.1000,
    'M8': 0.0200,
    'M9': 0.0040,
    'AP': 0.0200,
}

SYNTHETIC_R_VIA_OHM: Dict[str, float] = {
    'VIA1': 1.0000,
    'VIA2': 1.0000,
    'VIA3': 1.0000,
    'VIA4': 1.0000,
    'VIA5': 1.0000,
    'VIA6': 1.0000,
    'VIA7': 1.0000,
    'VIA8': 1.0000,
}

SYNTHETIC_DIELECTRIC_ZONES: Tuple[Tuple[float, float, float], ...] = (
    (0.000, 10.500, 4.0000),
)


@dataclass(frozen=True)
class StackupConfig:
    """Process vertical stackup descriptor used for feature extraction and 3D R."""

    layer_order: Tuple[str, ...]
    layer_stack_spec: Dict[str, Tuple[float, float, float, float]]
    dielectric_zones: Tuple[Tuple[float, float, float], ...]
    via_stack_spec: Dict[str, ViaSpec]
    rsh_ohm_sq: Dict[str, float]
    r_via_ohm: Dict[str, float]
    ground_nets: Tuple[str, ...] = ('VSS', 'GND')
    reference_m1_layer: str = 'M1'
    reference_z_um: float = 0.450
    grid_snap_um: float = DEFAULT_GRID_SNAP_UM
    layer_to_idx: Dict[str, int] = field(init=False)

    def __post_init__(self):
        object.__setattr__(
            self,
            'layer_to_idx',
            {name.upper(): idx for idx, name in enumerate(self.layer_order)},
        )


@lru_cache(maxsize=4)
def synthetic_stackup(
    *,
    grid_snap_um: float = DEFAULT_GRID_SNAP_UM,
    ground_nets: Tuple[str, ...] = ('VSS', 'GND'),
) -> StackupConfig:
    """Return a synthetic demonstration stackup, not a foundry or production process."""
    return StackupConfig(
        layer_order=SYNTHETIC_LAYER_ORDER,
        layer_stack_spec=dict(SYNTHETIC_LAYER_STACK_SPEC),
        dielectric_zones=SYNTHETIC_DIELECTRIC_ZONES,
        via_stack_spec=dict(SYNTHETIC_VIA_STACK_SPEC),
        rsh_ohm_sq=dict(SYNTHETIC_RSH_OHM_SQ),
        r_via_ohm=dict(SYNTHETIC_R_VIA_OHM),
        ground_nets=ground_nets,
        reference_m1_layer='M1',
        reference_z_um=0.450,
        grid_snap_um=grid_snap_um,
    )


def snap_grid(val: float, grid_um: float = DEFAULT_GRID_SNAP_UM) -> float:
    """Snap a coordinate or dimension (in um) to ``grid_um``."""
    return round(round(val / grid_um) * grid_um, 6)


def decompose_rectilinear_polygon(
    points: Sequence[Tuple[float, float]],
) -> List[Tuple[float, float, float, float]]:
    """Decompose an orthogonal polygon into disjoint axis-aligned rectangles ``(x0, y0, x1, y1)``."""
    pts = [(round(float(x), 6), round(float(y), 6)) for x, y in points]
    if len(pts) < 3:
        return []
    xs_all = sorted({p[0] for p in pts})
    ys_all = [p[1] for p in pts]
    if len(pts) <= 4 and len(xs_all) == 2 and len(set(ys_all)) == 2:
        return [(xs_all[0], min(ys_all), xs_all[1], max(ys_all))]

    m = len(pts)
    active_rects: Dict[Tuple[float, float], Tuple[float, float]] = {}
    finished: List[Tuple[float, float, float, float]] = []

    for xa, xb in zip(xs_all[:-1], xs_all[1:]):
        if xb - xa <= 1e-6:
            continue
        xm = 0.5 * (xa + xb)
        h_ys = []
        for k in range(m):
            x1, y1 = pts[k]
            x2, y2 = pts[(k + 1) % m]
            if abs(y1 - y2) <= 1e-6 and min(x1, x2) <= xm <= max(x1, x2) and abs(x1 - x2) > 1e-6:
                h_ys.append(0.5 * (y1 + y2))
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
            if y_iv in active_rects and abs(active_rects[y_iv][1] - xa) <= 1e-6:
                next_active[y_iv] = (active_rects[y_iv][0], xb)
            else:
                next_active[y_iv] = (xa, xb)
        for y_iv, (rx0, rx1) in active_rects.items():
            if y_iv not in next_active or abs(next_active[y_iv][0] - rx0) > 1e-6:
                finished.append((rx0, y_iv[0], rx1, y_iv[1]))
        active_rects = next_active

    for y_iv, (rx0, rx1) in active_rects.items():
        finished.append((rx0, y_iv[0], rx1, y_iv[1]))
    return finished


@lru_cache(maxsize=1024)
def calc_effective_eps_r(
    z_a: float,
    z_b: float,
    dielectric_zones: Tuple[Tuple[float, float, float], ...] = SYNTHETIC_DIELECTRIC_ZONES,
) -> float:
    """Compute series harmonic-mean relative permittivity between ``z_a`` and ``z_b`` (in um)."""
    z0, z1 = min(z_a, z_b), max(z_a, z_b)
    if z1 - z0 < 1e-6:
        for za, zb, eps in dielectric_zones:
            if za <= z0 <= zb:
                return eps
        return 3.5

    inv_eps_sum = 0.0
    total_dz = 0.0
    for za, zb, eps in dielectric_zones:
        ovlp = min(z1, zb) - max(z0, za)
        if ovlp > 0.0:
            inv_eps_sum += ovlp / eps
            total_dz += ovlp
    if total_dz <= 1e-9:
        return 3.5
    return total_dz / inv_eps_sum


def get_element_z_span(
    el: 'RectangleElement',
    stackup: Optional[StackupConfig] = None,
) -> Tuple[float, float, float]:
    """Return ``(z_min, z_max, thickness_or_height)`` in um for a metal or via element."""
    cfg = stackup or el.stackup or synthetic_stackup()
    if el.is_via:
        vspec = cfg.via_stack_spec[el.layer]
        return vspec.z_min, vspec.z_max, vspec.height
    z_min, z_max, thickness, _ = cfg.layer_stack_spec[el.layer]
    return z_min, z_max, thickness


@dataclass
class RectangleElement:
    """Axis-aligned conductor or via cut/cluster rectangle in micrometers."""

    id: int
    net_name: str
    layer: str
    layer_idx: int = 0
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    is_via: bool = False
    num_cuts: int = 1
    bot_elem_id: int = -1
    top_elem_id: int = -1
    layer_pos: float = 0.0
    stackup: Optional[StackupConfig] = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        cfg = self.stackup or synthetic_stackup()
        self.layer = self.layer.upper()
        if self.layer in cfg.via_stack_spec:
            self.is_via = True
            vspec = cfg.via_stack_spec[self.layer]
            self.layer_idx = vspec.bot_idx
            self.layer_pos = vspec.layer_pos
            self.num_cuts = max(1, int(self.num_cuts))
        else:
            self.is_via = False
            self.layer_idx = cfg.layer_to_idx[self.layer]
            self.layer_pos = float(self.layer_idx)
            self.num_cuts = 1

        grid = cfg.grid_snap_um
        bx0, bx1 = snap_grid(min(self.x0, self.x1), grid), snap_grid(max(self.x0, self.x1), grid)
        by0, by1 = snap_grid(min(self.y0, self.y1), grid), snap_grid(max(self.y0, self.y1), grid)
        self.x0, self.y0, self.x1, self.y1 = bx0, by0, bx1, by1

    @property
    def width(self) -> float:
        return min(self.x1 - self.x0, self.y1 - self.y0)

    @property
    def length(self) -> float:
        return max(self.x1 - self.x0, self.y1 - self.y0)

    @property
    def area(self) -> float:
        return (self.x1 - self.x0) * (self.y1 - self.y0)

    @property
    def perimeter(self) -> float:
        return 2.0 * ((self.x1 - self.x0) + (self.y1 - self.y0))

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def center(self) -> Tuple[float, float]:
        return (0.5 * (self.x0 + self.x1), 0.5 * (self.y0 + self.y1))


def _union_overlap_area_ratio(
    target_bbox: Tuple[float, float, float, float],
    other_bboxes: Sequence[Tuple[float, float, float, float]],
) -> float:
    """Compute fraction in ``[0, 1]`` of ``target_bbox`` covered by union of ``other_bboxes``."""
    tx0, ty0, tx1, ty1 = target_bbox
    t_area = (tx1 - tx0) * (ty1 - ty0)
    if t_area <= 1e-9 or not other_bboxes:
        return 0.0

    clipped = []
    for ox0, oy0, ox1, oy1 in other_bboxes:
        ix0, iy0 = max(tx0, ox0), max(ty0, oy0)
        ix1, iy1 = min(tx1, ox1), min(ty1, oy1)
        if ix1 > ix0 and iy1 > iy0:
            clipped.append((ix0, iy0, ix1, iy1))
    if not clipped:
        return 0.0
    if len(clipped) == 1:
        b0 = clipped[0]
        return min(1.0, ((b0[2] - b0[0]) * (b0[3] - b0[1])) / t_area)
    if len(clipped) == 2:
        a0, a1 = clipped[0], clipped[1]
        area_sum = (a0[2] - a0[0]) * (a0[3] - a0[1]) + (a1[2] - a1[0]) * (a1[3] - a1[1])
        ox = min(a0[2], a1[2]) - max(a0[0], a1[0])
        oy = min(a0[3], a1[3]) - max(a0[1], a1[1])
        if ox > 0.0 and oy > 0.0:
            area_sum -= ox * oy
        return min(1.0, area_sum / t_area)

    x_coords = sorted({b[0] for b in clipped} | {b[2] for b in clipped})
    union_area = 0.0
    for xa, xb in zip(x_coords[:-1], x_coords[1:]):
        dx = xb - xa
        if dx <= 1e-9:
            continue
        xm = 0.5 * (xa + xb)
        y_intervals = sorted((b[1], b[3]) for b in clipped if b[0] <= xm <= b[2])
        if not y_intervals:
            continue
        cur_y0, cur_y1 = y_intervals[0]
        y_len = 0.0
        for ny0, ny1 in y_intervals[1:]:
            if ny0 <= cur_y1:
                cur_y1 = max(cur_y1, ny1)
            else:
                y_len += cur_y1 - cur_y0
                cur_y0, cur_y1 = ny0, ny1
        y_len += cur_y1 - cur_y0
        union_area += dx * y_len

    return min(1.0, union_area / t_area)


def _line_of_sight_block_ratio(
    gap_box: Tuple[float, float, float, float],
    blocker_bboxes: Sequence[Tuple[float, float, float, float]],
    across_x: bool,
) -> float:
    """Compute 1D line-of-sight blocking fraction ``[0, 1]`` along the facing axis."""
    gx0, gy0, gx1, gy1 = gap_box
    span = (gy1 - gy0) if across_x else (gx1 - gx0)
    if span <= 1e-9 or not blocker_bboxes:
        return 0.0
    ivs = []
    for bx0, by0, bx1, by1 in blocker_bboxes:
        if bx1 <= gx0 or bx0 >= gx1 or by1 <= gy0 or by0 >= gy1:
            continue
        a = max(gy0, by0) if across_x else max(gx0, bx0)
        b = min(gy1, by1) if across_x else min(gx1, bx1)
        if b > a:
            ivs.append((a, b))
    if not ivs:
        return 0.0
    if len(ivs) == 1:
        return min(1.0, (ivs[0][1] - ivs[0][0]) / span)
    ivs.sort()
    cov = 0.0
    ca, cb = ivs[0]
    for na, nb in ivs[1:]:
        if na <= cb:
            cb = max(cb, nb)
        else:
            cov += cb - ca
            ca, cb = na, nb
    cov += cb - ca
    return min(1.0, cov / span)


def _los_block_sym(
    gap_box: Tuple[float, float, float, float],
    blocker_bboxes: Sequence[Tuple[float, float, float, float]],
    dx: float,
    dy: float,
) -> float:
    """E(2)-invariant 1D line-of-sight blocking fraction across a gap box."""
    if abs(dx - dy) > 1e-9:
        return _line_of_sight_block_ratio(gap_box, blocker_bboxes, across_x=(dx > dy))
    return 0.5 * (
        _line_of_sight_block_ratio(gap_box, blocker_bboxes, across_x=True)
        + _line_of_sight_block_ratio(gap_box, blocker_bboxes, across_x=False)
    )


def _is_artificial_seam_edge(
    ei: RectangleElement,
    ej: RectangleElement,
    dist_xy: float,
    partition_owner_by_id: Mapping[int, Optional[int]],
) -> bool:
    """Return True when two abutting same-net planar pieces meet across an artificial cut."""
    if dist_xy >= 1e-4 or ei.is_via or ej.is_via:
        return False
    if ei.layer_idx != ej.layer_idx or ei.net_name != ej.net_name:
        return False
    ou = partition_owner_by_id.get(ei.id)
    ov = partition_owner_by_id.get(ej.id)
    if ou is not None and ov is not None and ou != ov:
        return True
    return (
        (abs(ei.y0 - ej.y0) <= 1e-4 and abs(ei.y1 - ej.y1) <= 1e-4)
        or (abs(ei.x0 - ej.x0) <= 1e-4 and abs(ei.x1 - ej.x1) <= 1e-4)
    )


class LayoutGraphExtractor:
    """Extract E(2)-invariant Geo-Graph node and edge features within vertical/halo bounds."""

    NODE_DIM = 22
    EDGE_DIM = 22
    V1_DIM = 18

    def __init__(
        self,
        r_halo_um: float = 12.0,
        layer_window: int = 2,
        stackup: Optional[StackupConfig] = None,
    ):
        self.r_halo_um = r_halo_um
        self.layer_window = layer_window
        self.stackup = stackup or synthetic_stackup()

    def get_target_layer_pos(self, target_layer: str) -> float:
        tl = target_layer.upper()
        if tl in self.stackup.via_stack_spec:
            return self.stackup.via_stack_spec[tl].layer_pos
        return float(self.stackup.layer_to_idx[tl])

    def get_layer_range(self, target_layer: str) -> Set[int]:
        pos = self.get_target_layer_pos(target_layer)
        lo = max(0, int(math.floor(pos - self.layer_window)))
        hi = min(len(self.stackup.layer_order) - 1, int(math.ceil(pos + self.layer_window)))
        return set(range(lo, hi + 1))

    def filter_elements(
        self,
        elements: Sequence[RectangleElement],
        target_layer: str,
        target_bbox: Tuple[float, float, float, float],
    ) -> List[RectangleElement]:
        t_pos = self.get_target_layer_pos(target_layer)
        max_span = self.layer_window + (
            0.55 if target_layer.upper() in self.stackup.via_stack_spec else 0.05
        )
        bx0, by0, bx1, by1 = target_bbox
        hx0, hy0 = bx0 - self.r_halo_um, by0 - self.r_halo_um
        hx1, hy1 = bx1 + self.r_halo_um, by1 + self.r_halo_um

        out = []
        for el in elements:
            if abs(el.layer_pos - t_pos) > max_span:
                continue
            ex0, ey0, ex1, ey1 = el.bbox
            if ex1 < hx0 or ex0 > hx1 or ey1 < hy0 or ey0 > hy1:
                continue
            out.append(el)
        return out

    def extract_graph(
        self,
        active_elements: Sequence[RectangleElement],
        all_elements_for_ground_shield: Optional[Sequence[RectangleElement]] = None,
        *,
        partition_owner_by_id: Optional[Mapping[int, Optional[int]]] = None,
    ) -> Dict[str, Any]:
        bg_elements = (
            all_elements_for_ground_shield
            if all_elements_for_ground_shield is not None
            else active_elements
        )
        v_feat = self.build_node_features(active_elements, bg_elements)
        e_idx, e_feat = self.build_edge_features(
            active_elements,
            partition_owner_by_id=partition_owner_by_id,
        )
        return {
            'node_features': v_feat,
            'edge_index': e_idx,
            'edge_features': e_feat,
        }

    def build_node_features(
        self,
        elements: Sequence[RectangleElement],
        bg_elements: Sequence[RectangleElement],
    ) -> np.ndarray:
        """Build the 22-D E(2)-invariant node feature matrix."""
        if not elements:
            return np.zeros((0, self.NODE_DIM), dtype=np.float32)

        cfg = self.stackup
        grid = cfg.grid_snap_um
        ground_nets = set(cfg.ground_nets)
        ref_idx = cfg.layer_to_idx.get(cfg.reference_m1_layer, 1)
        planar_bg = [b for b in bg_elements if not b.is_via]
        via_bg = [b for b in bg_elements if b.is_via]
        bg_by_layer: Dict[int, List[RectangleElement]] = {}
        for b in planar_bg:
            bg_by_layer.setdefault(b.layer_idx, []).append(b)
        m1_gnd_boxes = [
            b.bbox for b in bg_by_layer.get(ref_idx, ()) if b.net_name in ground_nets
        ]

        el_by_pos: Dict[float, List[RectangleElement]] = {}
        for e in elements:
            el_by_pos.setdefault(e.layer_pos, []).append(e)

        rows = []
        for el in elements:
            z_min, z_max, thickness = get_element_z_span(el, cfg)
            w = max(el.width, grid)
            length = max(el.length, grid)
            area = max(el.area, 1e-6)
            ex0, ey0, ex1, ey1 = el.bbox
            log_ar = math.log1p(length / w)

            if el.is_via:
                vspec = cfg.via_stack_spec[el.layer]
                l1_bot_boxes = [
                    b.bbox for b in bg_by_layer.get(vspec.bot_idx, ())
                    if b.x1 > ex0 and b.x0 < ex1 and b.y1 > ey0 and b.y0 < ey1
                ]
                l2_bot_boxes = [
                    b.bbox for b in bg_by_layer.get(vspec.bot_idx - 1, ())
                    if b.x1 > ex0 and b.x0 < ex1 and b.y1 > ey0 and b.y0 < ey1
                ]
                all_bot_boxes = [
                    b.bbox
                    for lk in range(0, vspec.bot_idx + 1)
                    for b in bg_by_layer.get(lk, ())
                    if b.x1 > ex0 and b.x0 < ex1 and b.y1 > ey0 and b.y0 < ey1
                ]
                l1_top_boxes = [
                    b.bbox for b in bg_by_layer.get(vspec.top_idx, ())
                    if b.x1 > ex0 and b.x0 < ex1 and b.y1 > ey0 and b.y0 < ey1
                ]
                l2_top_boxes = [
                    b.bbox for b in bg_by_layer.get(vspec.top_idx + 1, ())
                    if b.x1 > ex0 and b.x0 < ex1 and b.y1 > ey0 and b.y0 < ey1
                ]
            else:
                li = el.layer_idx
                l1_bot_boxes = [
                    b.bbox for b in bg_by_layer.get(li - 1, ())
                    if b.id != el.id and b.x1 > ex0 and b.x0 < ex1 and b.y1 > ey0 and b.y0 < ey1
                ]
                l2_bot_boxes = [
                    b.bbox for b in bg_by_layer.get(li - 2, ())
                    if b.id != el.id and b.x1 > ex0 and b.x0 < ex1 and b.y1 > ey0 and b.y0 < ey1
                ]
                all_bot_boxes = [
                    b.bbox
                    for lk in range(0, li)
                    for b in bg_by_layer.get(lk, ())
                    if b.id != el.id and b.x1 > ex0 and b.x0 < ex1 and b.y1 > ey0 and b.y0 < ey1
                ]
                l1_top_boxes = [
                    b.bbox for b in bg_by_layer.get(li + 1, ())
                    if b.id != el.id and b.x1 > ex0 and b.x0 < ex1 and b.y1 > ey0 and b.y0 < ey1
                ]
                l2_top_boxes = [
                    b.bbox for b in bg_by_layer.get(li + 2, ())
                    if b.id != el.id and b.x1 > ex0 and b.x0 < ex1 and b.y1 > ey0 and b.y0 < ey1
                ]

            sh_b1 = _union_overlap_area_ratio(el.bbox, l1_bot_boxes)
            sh_b2 = _union_overlap_area_ratio(el.bbox, l2_bot_boxes)
            sh_b_all = _union_overlap_area_ratio(el.bbox, all_bot_boxes)
            sh_t1 = _union_overlap_area_ratio(el.bbox, l1_top_boxes)
            sh_t2 = _union_overlap_area_ratio(el.bbox, l2_top_boxes)

            m1_cov = (
                _union_overlap_area_ratio(el.bbox, m1_gnd_boxes)
                if el.layer_pos > ref_idx + 0.05
                else 0.0
            )
            z_ref = cfg.reference_z_um * m1_cov
            h_eff = max(0.15, z_min - z_ref)
            eps_gnd = calc_effective_eps_r(z_ref, z_min, cfg.dielectric_zones)

            min_gap = self.r_halo_um
            near_area = 0.0
            shared_abut_len = 0.0
            win_area = (ex1 - ex0 + 10.0) * (ey1 - ey0 + 10.0)
            for other in el_by_pos.get(el.layer_pos, ()):
                if other.id == el.id:
                    continue
                ox0, oy0, ox1, oy1 = other.bbox
                ox_raw = min(ex1, ox1) - max(ex0, ox0)
                oy_raw = min(ey1, oy1) - max(ey0, oy0)
                dx = max(0.0, -ox_raw)
                dy = max(0.0, -oy_raw)
                if not el.is_via and other.net_name == el.net_name:
                    if abs(ox_raw) <= 1e-4 and oy_raw > 1e-4:
                        shared_abut_len += oy_raw
                    elif abs(oy_raw) <= 1e-4 and ox_raw > 1e-4:
                        shared_abut_len += ox_raw
                gap = math.hypot(dx, dy)
                if gap > 1e-4 and gap < min_gap:
                    min_gap = gap
                ix0, iy0 = max(ex0 - 5.0, ox0), max(ey0 - 5.0, oy0)
                ix1, iy1 = min(ex1 + 5.0, ox1), min(ey1 + 5.0, oy1)
                if ix1 > ix0 and iy1 > iy0:
                    near_area += (ix1 - ix0) * (iy1 - iy0)
            same_density = min(1.0, near_area / max(win_area, 1.0))
            peri = max(0.0, el.perimeter - shared_abut_len)

            same_net_adj_boxes = [
                b.bbox
                for b in planar_bg
                if b.id != el.id
                and b.net_name == el.net_name
                and 0 < abs(b.layer_pos - el.layer_pos) <= 2.05
                and b.x1 > ex0
                and b.x0 < ex1
                and b.y1 > ey0
                and b.y0 < ey1
            ]
            same_net_vert_shield = _union_overlap_area_ratio(el.bbox, same_net_adj_boxes)

            if el.is_via:
                vspec = cfg.via_stack_spec[el.layer]
                is_via_flag = 1.0
                log_cuts = math.log1p(el.num_cuts)
                via_fill = min(1.0, (el.num_cuts * (vspec.cut_width ** 2)) / area)
                c_pp_gnd = 0.0
                unscreened_via = max(0.01, 1.0 - 0.96 * sh_b_all)
                c_fringe_gnd = (
                    0.12
                    * EPS0_FF_PER_UM
                    * eps_gnd
                    * peri
                    * (thickness / max(h_eff, 0.2))
                    * unscreened_via
                )
            else:
                is_via_flag = 0.0
                log_cuts = 0.0
                touching_via_boxes = [
                    vb.bbox
                    for vb in via_bg
                    if vb.net_name == el.net_name
                    and abs(vb.layer_pos - el.layer_pos) < 0.6
                    and vb.x1 > ex0
                    and vb.x0 < ex1
                    and vb.y1 > ey0
                    and vb.y0 < ey1
                ]
                via_fill = _union_overlap_area_ratio(el.bbox, touching_via_boxes)

                non_gnd_bot_boxes = [
                    b.bbox
                    for lk in range(0, el.layer_idx)
                    for b in bg_by_layer.get(lk, ())
                    if b.id != el.id
                    and b.net_name not in ground_nets
                    and b.x1 > ex0
                    and b.x0 < ex1
                    and b.y1 > ey0
                    and b.y0 < ey1
                ]
                screened_by_lower = _union_overlap_area_ratio(el.bbox, non_gnd_bot_boxes)
                unscreened_gnd_frac = max(0.05, 1.0 - 0.85 * screened_by_lower)

                c_pp_gnd = EPS0_FF_PER_UM * eps_gnd * (el.area / h_eff) * unscreened_gnd_frac
                lat_ground_factor = min(1.0, min_gap / (min_gap + 0.8 * h_eff))
                top_self_factor = max(0.50, 1.0 - 0.35 * same_net_vert_shield)
                c_fringe_gnd = (
                    EPS0_FF_PER_UM
                    * eps_gnd
                    * peri
                    * (
                        0.28 * ((thickness / h_eff) ** 0.222)
                        + 0.65 / math.log(1.0 + 2.0 * h_eff / max(thickness, 0.05))
                    )
                    * unscreened_gnd_frac
                    * (0.28 + 0.72 * lat_ground_factor)
                    * top_self_factor
                )

            rows.append([
                w, length, log_ar, el.area, peri, thickness, z_min, z_max, eps_gnd,
                sh_b1, sh_b2, sh_b_all, sh_t1, sh_t2, min_gap, same_density,
                c_pp_gnd, c_fringe_gnd, is_via_flag, log_cuts, via_fill, same_net_vert_shield,
            ])

        return np.asarray(rows, dtype=np.float32)

    def build_edge_features(
        self,
        elements: Sequence[RectangleElement],
        *,
        partition_owner_by_id: Optional[Mapping[int, Optional[int]]] = None,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Build bidirectional edge indices and 22-D E(2)-invariant edge feature matrix."""
        cfg = self.stackup
        edge_index: List[List[int]] = []
        edge_features: List[List[float]] = []
        n = len(elements)
        if n <= 1:
            return np.zeros((2, 0), dtype=np.int64), np.zeros((0, self.EDGE_DIM), dtype=np.float32)

        via_elems = [(idx_v, ev) for idx_v, ev in enumerate(elements) if ev.is_via]
        planar_by_layer: Dict[int, List[Tuple[int, RectangleElement]]] = {}
        for idx_p, ep in enumerate(elements):
            if not ep.is_via:
                planar_by_layer.setdefault(ep.layer_idx, []).append((idx_p, ep))

        # Precompute nearest same-layer non-touching neighbor gap for dense-pitch crowding factor.
        min_gaps = [self.r_halo_um] * n
        for l_items in planar_by_layer.values():
            m_l = len(l_items)
            for a_i in range(m_l):
                idx_a, ea = l_items[a_i]
                ax0, ay0, ax1, ay1 = ea.bbox
                mg = self.r_halo_um
                for b_i in range(m_l):
                    if a_i == b_i:
                        continue
                    _, eb = l_items[b_i]
                    dx_ab = max(0.0, max(ax0, eb.x0) - min(ax1, eb.x1))
                    dy_ab = max(0.0, max(ay0, eb.y0) - min(ay1, eb.y1))
                    g_ab = math.hypot(dx_ab, dy_ab)
                    if g_ab > 1e-4 and g_ab < mg:
                        mg = g_ab
                min_gaps[idx_a] = mg

        for i in range(n):
            ei = elements[i]
            ix0, iy0, ix1, iy1 = ei.bbox
            zi_min, zi_max, ti = get_element_z_span(ei, cfg)
            ei_area = max(ei.area, 1e-6)

            for j in range(i + 1, n):
                ej = elements[j]
                layer_diff = ej.layer_pos - ei.layer_pos
                abs_ld = abs(layer_diff)
                if abs_ld > self.layer_window + 0.55:
                    continue

                jx0, jy0, jx1, jy1 = ej.bbox
                dx = max(0.0, max(ix0, jx0) - min(ix1, jx1))
                dy = max(0.0, max(iy0, jy0) - min(iy1, jy1))
                dist_xy = math.hypot(dx, dy)
                if dist_xy > self.r_halo_um:
                    continue

                same_net = 1.0 if ei.net_name == ej.net_name else 0.0
                if same_net > 0.5 and (dist_xy > 0.40 or abs_ld > 1.1):
                    continue
                if (
                    same_net > 0.5
                    and partition_owner_by_id is not None
                    and _is_artificial_seam_edge(ei, ej, dist_xy, partition_owner_by_id)
                ):
                    continue

                zj_min, zj_max, tj = get_element_z_span(ej, cfg)
                if zj_min >= zi_max:
                    dz = max(0.02, zj_min - zi_max)
                    eps_ij = calc_effective_eps_r(zi_max, zj_min, cfg.dielectric_zones)
                elif zi_min >= zj_max:
                    dz = max(0.02, zi_min - zj_max)
                    eps_ij = calc_effective_eps_r(zj_max, zi_min, cfg.dielectric_zones)
                else:
                    dz = 0.0
                    eps_ij = calc_effective_eps_r(
                        min(zi_min, zj_min), max(zi_max, zj_max), cfg.dielectric_zones
                    )

                is_via_edge = 1.0 if (ei.is_via or ej.is_via) else 0.0
                ovlp_x = max(0.0, min(ix1, jx1) - max(ix0, jx0))
                ovlp_y = max(0.0, min(iy1, jy1) - max(iy0, jy0))
                overlap_area = ovlp_x * ovlp_y
                overlap_peri = 2.0 * (ovlp_x + ovlp_y) if overlap_area > 0.0 else 0.0

                if dx > 0.0 and dy == 0.0:
                    parallel_len = ovlp_y
                elif dy > 0.0 and dx == 0.0:
                    parallel_len = ovlp_x
                elif dx == 0.0 and dy == 0.0:
                    parallel_len = max(ovlp_x, ovlp_y)
                else:
                    parallel_len = 0.0

                # Electrostatic screening distance cutoffs
                if is_via_edge > 0.5:
                    if same_net < 0.5 and (dz > 0.25 or parallel_len <= 1e-4 or dist_xy > 1.5):
                        continue
                elif abs_ld < 0.1:
                    if same_net < 0.5 and (dist_xy > 5.5 or (parallel_len <= 1e-4 and dist_xy > 0.8)):
                        continue
                else:
                    if overlap_area <= 0.0 and dist_xy > max(2.2, 3.5 * dz):
                        continue

                intervening_shield = 0.0
                lo_l = min(ei.layer_idx, ej.layer_idx)
                hi_l = max(ei.layer_idx, ej.layer_idx)
                if abs_ld >= 1.9 and overlap_area > 0.0:
                    ox_lo, oy_lo = max(ix0, jx0), max(iy0, jy0)
                    ox_hi, oy_hi = min(ix1, jx1), min(iy1, jy1)
                    mid_boxes = [
                        ek.bbox
                        for lk in range(lo_l + 1, hi_l)
                        for _, ek in planar_by_layer.get(lk, ())
                        if ek.x1 > ox_lo and ek.x0 < ox_hi and ek.y1 > oy_lo and ek.y0 < oy_hi
                    ]
                    if mid_boxes:
                        intervening_shield = _union_overlap_area_ratio(
                            (ox_lo, oy_lo, ox_hi, oy_hi), mid_boxes
                        )

                coplanar_shield = 0.0
                via_fence_shield = 0.0
                if dist_xy > 0.02:
                    gx0 = min(ix1, jx1) if dx > 0 else max(ix0, jx0)
                    gy0 = min(iy1, jy1) if dy > 0 else max(iy0, jy0)
                    gx1 = max(ix0, jx0) if dx > 0 else min(ix1, jx1)
                    gy1 = max(iy0, jy0) if dy > 0 else min(iy1, jy1)
                    if gx1 > gx0 and gy1 > gy0:
                        gap_box = (gx0, gy0, gx1, gy1)
                        if abs_ld < 0.1 and not ei.is_via and not ej.is_via:
                            blockers = [
                                ek.bbox
                                for idx_k, ek in planar_by_layer.get(ei.layer_idx, ())
                                if idx_k not in (i, j)
                                and ek.x1 > gx0
                                and ek.x0 < gx1
                                and ek.y1 > gy0
                                and ek.y0 < gy1
                            ]
                            if blockers:
                                coplanar_shield = _los_block_sym(gap_box, blockers, dx, dy)
                            if same_net < 0.5 and coplanar_shield > 0.94:
                                continue
                        elif abs_ld >= 0.8 and overlap_area <= 0.0 and not ei.is_via and not ej.is_via:
                            lat_blockers = [
                                ek.bbox
                                for lk in range(lo_l, hi_l + 1)
                                for idx_k, ek in planar_by_layer.get(lk, ())
                                if idx_k not in (i, j)
                                and ek.x1 > gx0
                                and ek.x0 < gx1
                                and ek.y1 > gy0
                                and ek.y0 < gy1
                            ]
                            if lat_blockers:
                                coplanar_shield = _los_block_sym(gap_box, lat_blockers, dx, dy)
                            if same_net < 0.5 and coplanar_shield > 0.92:
                                continue

                        if via_elems:
                            z_band_lo = min(zi_min, zj_min) - 0.25
                            z_band_hi = max(zi_max, zj_max) + 0.25
                            fence_boxes = [
                                ev.bbox
                                for idx_v, ev in via_elems
                                if idx_v not in (i, j)
                                and ev.x1 > gx0
                                and ev.x0 < gx1
                                and ev.y1 > gy0
                                and ev.y0 < gy1
                                and get_element_z_span(ev, cfg)[1] >= z_band_lo
                                and get_element_z_span(ev, cfg)[0] <= z_band_hi
                            ]
                            if fence_boxes:
                                via_fence_shield = _los_block_sym(gap_box, fence_boxes, dx, dy)

                ej_area = max(ej.area, 1e-6)
                t_avg = 0.5 * (ti + tj)
                sidewall_ar = t_avg / max(dist_xy, 0.05)
                ovlp_frac_i = overlap_area / ei_area
                ovlp_frac_j = overlap_area / ej_area

                via_barrel_area = 0.0
                c_via_barrel = 0.0

                if is_via_edge > 0.5:
                    c_pp_cpl = 0.0
                    c_fr_cpl = 0.0
                    vel = ei if ei.is_via else ej
                    _, _, h_via = get_element_z_span(vel, cfg)
                    facing_l = max(parallel_len, 0.0)
                    via_barrel_area = h_via * facing_l
                    if same_net < 0.5 and facing_l > 0.0:
                        d_3d = math.hypot(max(dist_xy, 0.04), dz)
                        c_via_barrel = (
                            EPS0_FF_PER_UM
                            * eps_ij
                            * h_via
                            * (
                                0.95
                                * facing_l
                                / math.log(1.0 + math.pi * d_3d / max(vel.width, 0.08))
                            )
                            * math.exp(-dist_xy / 0.8)
                            * max(0.05, 1.0 - 0.85 * via_fence_shield)
                        )
                else:
                    fence_atten = max(0.10, 1.0 - 0.65 * via_fence_shield)
                    if abs_ld < 0.1:
                        c_pp_cpl = 0.0
                        s_eff = max(dist_xy, 0.05)
                        h_gnd = max(0.2, zi_min)
                        c_fr_cpl = (
                            EPS0_FF_PER_UM
                            * eps_ij
                            * parallel_len
                            * (
                                1.06 * (t_avg / s_eff)
                                + 0.42
                                * ((min(ei.width, ej.width) / h_gnd) ** 0.2)
                                * (h_gnd / (h_gnd + 2.0 * s_eff))
                            )
                            * max(0.0, 1.0 - 0.96 * coplanar_shield)
                            * fence_atten
                        )
                    else:
                        unblocked_pp = max(0.03, 1.0 - 0.92 * intervening_shield) * fence_atten
                        c_pp_cpl = (
                            EPS0_FF_PER_UM * eps_ij * (overlap_area / max(dz, 0.05)) * unblocked_pp
                        )
                        crowd_i = min_gaps[i] / (min_gaps[i] + 1.1 * dz)
                        crowd_j = min_gaps[j] / (min_gaps[j] + 1.1 * dz)
                        crowd_ij = crowd_i * crowd_j
                        unblocked_fr = unblocked_pp * max(0.03, 1.0 - 0.92 * coplanar_shield)
                        d_3d = math.hypot(dist_xy, dz)
                        c_fr_cpl = (
                            EPS0_FF_PER_UM
                            * eps_ij
                            * (overlap_peri * crowd_ij + 0.42 * parallel_len * min(crowd_i, crowd_j))
                            * (0.36 * dz / max(d_3d, 0.08))
                            * math.exp(-dist_xy / max(1.8 * dz, 0.30))
                            * unblocked_fr
                        )

                f_same = 1.0 if abs_ld < 0.1 else 0.0
                f_adj = 1.0 if 0.8 <= abs_ld <= 1.2 else 0.0
                f_far = 1.0 if abs_ld >= 1.8 else 0.0
                ld_half = layer_diff * 0.5
                abs_ld_half = abs_ld * 0.5
                c_fr_tot = c_fr_cpl + c_via_barrel

                edge_index.append([i, j])
                edge_features.append([
                    ld_half, abs_ld_half, f_same, f_adj, f_far,
                    dz, eps_ij, dist_xy, parallel_len, overlap_area, overlap_peri,
                    intervening_shield, coplanar_shield, same_net, sidewall_ar,
                    ovlp_frac_i, c_pp_cpl, c_fr_tot,
                    is_via_edge, via_barrel_area, via_fence_shield, c_via_barrel,
                ])
                edge_index.append([j, i])
                edge_features.append([
                    -ld_half, abs_ld_half, f_same, f_adj, f_far,
                    dz, eps_ij, dist_xy, parallel_len, overlap_area, overlap_peri,
                    intervening_shield, coplanar_shield, same_net, sidewall_ar,
                    ovlp_frac_j, c_pp_cpl, c_fr_tot,
                    is_via_edge, via_barrel_area, via_fence_shield, c_via_barrel,
                ])

        if not edge_index:
            return np.zeros((2, 0), dtype=np.int64), np.zeros((0, self.EDGE_DIM), dtype=np.float32)

        return (
            np.asarray(edge_index, dtype=np.int64).T,
            np.asarray(edge_features, dtype=np.float32),
        )
