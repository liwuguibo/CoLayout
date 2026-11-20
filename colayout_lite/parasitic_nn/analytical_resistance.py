"""3D analytical and nodal Laplacian resistance engine for interconnects and vias.

Supports planar traces and L-bends (0.55-square corner correction), multi-cut via
arrays, cross-layer jumpers, stacked vias, and parallel shunt buses via fast 3D
nodal Laplacian point-to-point resistance reduction.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .geometry_graph import RectangleElement, StackupConfig, synthetic_stackup


def _solve_linear_system(A: List[List[float]], b: List[float]) -> List[float]:
    """Solve a small dense nodal conductance system."""
    n = len(b)
    if n >= 4:
        mat = np.asarray(A, dtype=np.float64)
        rhs = np.asarray(b, dtype=np.float64)
        try:
            return np.linalg.solve(mat, rhs).tolist()
        except np.linalg.LinAlgError:
            return np.linalg.lstsq(mat, rhs, rcond=None)[0].tolist()
    matrix = [row[:] + [b[i]] for i, row in enumerate(A)]
    for k in range(n):
        piv = max(range(k, n), key=lambda r: abs(matrix[r][k]))
        if abs(matrix[piv][k]) < 1e-12:
            continue
        if piv != k:
            matrix[k], matrix[piv] = matrix[piv], matrix[k]
        diag = matrix[k][k]
        for j in range(k, n + 1):
            matrix[k][j] /= diag
        for i in range(n):
            if i != k and abs(matrix[i][k]) > 1e-15:
                factor = matrix[i][k]
                for j in range(k, n + 1):
                    matrix[i][j] -= factor * matrix[k][j]
    return [matrix[i][n] for i in range(n)]


class AnalyticalResistorEngine:
    """Fast 3D nodal & analytical resistance calculator for metals and vias."""

    VIA_ARRAY_EXPONENT: float = 1.00
    CROWDING_BETA: float = 0.00

    @staticmethod
    def calc_wire_r(
        layer: str,
        width_um: float,
        length_um: float,
        num_corners: int = 0,
        temp_c: float = 25.0,
        *,
        stackup: Optional[StackupConfig] = None,
    ) -> float:
        cfg = stackup or synthetic_stackup()
        lname = layer.upper()
        r_sh = cfg.rsh_ohm_sq.get(lname, 0.139)
        tc1 = 0.00086 if lname == 'PO' else 0.0035
        r_sh_t = r_sh * (1.0 + tc1 * (temp_c - 25.0))
        w = max(width_um, 0.025)
        length = max(length_um, 0.025)
        effective_l = max(0.0, length - num_corners * w)
        squares = (effective_l / w) + 0.55 * num_corners
        return float(r_sh_t * squares)

    @classmethod
    def calc_via_cluster_r(
        cls,
        via_layer: str,
        num_cuts: int = 1,
        bot_width_um: Optional[float] = None,
        top_width_um: Optional[float] = None,
        cluster_width_um: Optional[float] = None,
        temp_c: float = 25.0,
        *,
        stackup: Optional[StackupConfig] = None,
    ) -> float:
        """Calculate effective resistance of a single via or multi-cut via cluster."""
        cfg = stackup or synthetic_stackup()
        vl = via_layer.upper()
        vspec = cfg.via_stack_spec.get(vl)
        r_single = cfg.r_via_ohm.get(vl, vspec.r_single_ohm if vspec is not None else 1.50)
        tc1 = vspec.tc1 if vspec is not None else 7.8e-4
        r_single_t = r_single * (1.0 + tc1 * (temp_c - 25.0))

        n_eff = max(1, int(num_cuts)) ** cls.VIA_ARRAY_EXPONENT
        r_core = r_single_t / n_eff

        if vspec is None or cls.CROWDING_BETA == 0.0:
            return float(r_core)

        w_clust = max(cluster_width_um or vspec.cut_width, vspec.cut_width)
        w_bot = max(bot_width_um or w_clust, w_clust)
        w_top = max(top_width_um or w_clust, w_clust)
        rsh_bot = cfg.rsh_ohm_sq.get(vspec.bot_layer, 0.139)
        rsh_top = cfg.rsh_ohm_sq.get(vspec.top_layer, 0.139)

        r_spread = cls.CROWDING_BETA * (
            (rsh_bot / (2.0 * math.pi)) * math.log(1.0 + w_bot / w_clust)
            + (rsh_top / (2.0 * math.pi)) * math.log(1.0 + w_top / w_clust)
        )
        return float(r_core + r_spread)

    @classmethod
    def calc_net_elements_r(
        cls,
        elements: Sequence[RectangleElement],
        net_name: str,
        temp_c: float = 25.0,
        *,
        stackup: Optional[StackupConfig] = None,
    ) -> float:
        """Compute driving-point end-to-end resistance (Ohm) for ``net_name``."""
        net_elems = [e for e in elements if e.net_name == net_name]
        if not net_elems:
            return 0.0

        cfg = stackup or net_elems[0].stackup or synthetic_stackup()
        planar_elems = [e for e in net_elems if not e.is_via]
        via_elems = [e for e in net_elems if e.is_via]

        if not planar_elems:
            return sum(
                cls.calc_via_cluster_r(v.layer, v.num_cuts, temp_c=temp_c, stackup=cfg)
                for v in via_elems
            )

        if not via_elems:
            total_r = 0.0
            for idx, el in enumerate(planar_elems):
                n_corners = 0
                ex0, ey0, ex1, ey1 = el.bbox
                for jdx, other in enumerate(planar_elems):
                    if jdx <= idx or other.layer_idx != el.layer_idx:
                        continue
                    ox0, oy0, ox1, oy1 = other.bbox
                    if min(ex1, ox1) > max(ex0, ox0) and min(ey1, oy1) > max(ey0, oy0):
                        n_corners += 1
                total_r += cls.calc_wire_r(
                    el.layer,
                    width_um=el.width,
                    length_um=el.length,
                    num_corners=n_corners,
                    temp_c=temp_c,
                    stackup=cfg,
                )
            return total_r

        nodes_xyz: List[Tuple[float, float, float]] = []
        edges_gr: List[Tuple[int, int, float]] = []
        terminal_candidates: List[int] = []

        def add_node(x: float, y: float, z: float) -> int:
            idx_n = len(nodes_xyz)
            nodes_xyz.append((x, y, z))
            return idx_n

        elem_anchors: Dict[int, List[Tuple[float, int]]] = {
            i: [] for i in range(len(planar_elems))
        }

        def register_anchor_on_planar(p_idx: int, px: float, py: float) -> int:
            pel = planar_elems[p_idx]
            spec_p = cfg.layer_stack_spec[pel.layer]
            z_mid = 0.5 * (spec_p[0] + spec_p[1])
            horiz = (pel.x1 - pel.x0) >= (pel.y1 - pel.y0)
            coord_1d = min(max(px, pel.x0), pel.x1) if horiz else min(max(py, pel.y0), pel.y1)
            cx = coord_1d if horiz else 0.5 * (pel.x0 + pel.x1)
            cy = 0.5 * (pel.y0 + pel.y1) if horiz else coord_1d
            for existing_c, existing_nid in elem_anchors[p_idx]:
                if abs(existing_c - coord_1d) <= 0.020:
                    return existing_nid
            nid = add_node(cx, cy, z_mid)
            elem_anchors[p_idx].append((coord_1d, nid))
            return nid

        # 1. Connect same-layer touching/L-bend intersections
        for i in range(len(planar_elems)):
            ei = planar_elems[i]
            for j in range(i + 1, len(planar_elems)):
                ej = planar_elems[j]
                if ei.layer_idx != ej.layer_idx:
                    continue
                ix0, iy0 = max(ei.x0, ej.x0), max(ei.y0, ej.y0)
                ix1, iy1 = min(ei.x1, ej.x1), min(ei.y1, ej.y1)
                if ix1 > ix0 and iy1 > iy0:
                    mx, my = 0.5 * (ix0 + ix1), 0.5 * (iy0 + iy1)
                    ni = register_anchor_on_planar(i, mx, my)
                    nj = register_anchor_on_planar(j, mx, my)
                    if ni != nj:
                        r_bend = 0.55 * cfg.rsh_ohm_sq.get(ei.layer, 0.139)
                        edges_gr.append((ni, nj, 1.0 / max(r_bend, 1e-4)))

        # 2. Connect ViaClusters between bottom metal and top metal
        for vel in via_elems:
            vspec = cfg.via_stack_spec.get(vel.layer)
            if vspec is None:
                continue
            vx, vy = vel.center
            bot_p = None
            top_p = None
            for i, pel in enumerate(planar_elems):
                if pel.layer == vspec.bot_layer:
                    if min(vel.x1, pel.x1) > max(vel.x0, pel.x0) and min(vel.y1, pel.y1) > max(
                        vel.y0, pel.y0
                    ):
                        bot_p = i
                        break
            for i, pel in enumerate(planar_elems):
                if pel.layer == vspec.top_layer:
                    if min(vel.x1, pel.x1) > max(vel.x0, pel.x0) and min(vel.y1, pel.y1) > max(
                        vel.y0, pel.y0
                    ):
                        top_p = i
                        break

            r_v = cls.calc_via_cluster_r(
                vel.layer,
                num_cuts=vel.num_cuts,
                bot_width_um=planar_elems[bot_p].width if bot_p is not None else None,
                top_width_um=planar_elems[top_p].width if top_p is not None else None,
                cluster_width_um=vel.width,
                temp_c=temp_c,
                stackup=cfg,
            )
            if bot_p is not None and top_p is not None:
                nb = register_anchor_on_planar(bot_p, vx, vy)
                nt = register_anchor_on_planar(top_p, vx, vy)
                edges_gr.append((nb, nt, 1.0 / max(r_v, 1e-4)))
            elif bot_p is not None:
                nb = register_anchor_on_planar(bot_p, vx, vy)
                nt = add_node(vx, vy, vspec.z_max)
                edges_gr.append((nb, nt, 1.0 / max(r_v, 1e-4)))
                terminal_candidates.append(nt)
            elif top_p is not None:
                nt = register_anchor_on_planar(top_p, vx, vy)
                nb = add_node(vx, vy, vspec.z_min)
                edges_gr.append((nb, nt, 1.0 / max(r_v, 1e-4)))
                terminal_candidates.append(nb)

        # 3. Slice planar elements along anchor coordinates
        for i, pel in enumerate(planar_elems):
            horiz = (pel.x1 - pel.x0) >= (pel.y1 - pel.y0)
            if pel.length > 2.2 * pel.width or len(planar_elems) == 1:
                if horiz:
                    n_s = register_anchor_on_planar(i, pel.x0, 0.5 * (pel.y0 + pel.y1))
                    n_e = register_anchor_on_planar(i, pel.x1, 0.5 * (pel.y0 + pel.y1))
                else:
                    n_s = register_anchor_on_planar(i, 0.5 * (pel.x0 + pel.x1), pel.y0)
                    n_e = register_anchor_on_planar(i, 0.5 * (pel.x0 + pel.x1), pel.y1)
                terminal_candidates.extend([n_s, n_e])
            elif not elem_anchors[i]:
                n_c = register_anchor_on_planar(i, *pel.center)
                terminal_candidates.append(n_c)

            anchors_sorted = sorted(elem_anchors[i], key=lambda t: t[0])
            for (c0, n0), (c1, n1) in zip(anchors_sorted[:-1], anchors_sorted[1:]):
                seg_len = max(0.01, c1 - c0)
                r_seg = cls.calc_wire_r(
                    pel.layer,
                    width_um=pel.width,
                    length_um=seg_len,
                    temp_c=temp_c,
                    stackup=cfg,
                )
                edges_gr.append((n0, n1, 1.0 / max(r_seg, 1e-4)))

        n_nodes = len(nodes_xyz)
        if n_nodes <= 1 or not edges_gr:
            return sum(
                cls.calc_wire_r(p.layer, p.width, p.length, temp_c=temp_c, stackup=cfg)
                for p in planar_elems
            )

        adj: List[List[Tuple[int, float]]] = [[] for _ in range(n_nodes)]
        for u, v, g in edges_gr:
            adj[u].append((v, g))
            adj[v].append((u, g))

        visited = [False] * n_nodes
        total_equiv_r = 0.0
        cand_set = set(terminal_candidates) if terminal_candidates else set(range(n_nodes))

        for root in range(n_nodes):
            if visited[root]:
                continue
            comp = []
            queue = [root]
            visited[root] = True
            while queue:
                cur = queue.pop()
                comp.append(cur)
                for nxt, _ in adj[cur]:
                    if not visited[nxt]:
                        visited[nxt] = True
                        queue.append(nxt)
            if len(comp) <= 1:
                continue

            comp_cands = [u for u in comp if u in cand_set]
            if len(comp_cands) < 2:
                comp_cands = comp

            best_pair = (comp_cands[0], comp_cands[-1])
            best_dist2 = -1.0
            for ii in range(len(comp_cands)):
                u = comp_cands[ii]
                ux, uy, uz = nodes_xyz[u]
                for jj in range(ii + 1, len(comp_cands)):
                    v = comp_cands[jj]
                    vx, vy, vz = nodes_xyz[v]
                    d2 = (ux - vx) ** 2 + (uy - vy) ** 2 + (2.5 * (uz - vz)) ** 2
                    if d2 > best_dist2:
                        best_dist2 = d2
                        best_pair = (u, v)

            src_global, snk_global = best_pair
            if src_global == snk_global:
                continue

            active_nodes = [u for u in comp if u != snk_global]
            g2l = {u: idx_l for idx_l, u in enumerate(active_nodes)}
            m = len(active_nodes)
            conductance = [[0.0] * m for _ in range(m)]
            rhs = [0.0] * m
            rhs[g2l[src_global]] = 1.0

            for u in active_nodes:
                lu = g2l[u]
                for v, g_val in adj[u]:
                    conductance[lu][lu] += g_val
                    if v != snk_global:
                        lv = g2l[v]
                        conductance[lu][lv] -= g_val

            voltages = _solve_linear_system(conductance, rhs)
            r_comp = max(0.0, voltages[g2l[src_global]])
            total_equiv_r += r_comp

        return float(total_equiv_r)
