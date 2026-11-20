"""Pure-NumPy Geo-Graph Residual MLP inference engine for real-time RC prediction."""

from __future__ import annotations

import hashlib
from pathlib import Path
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np

from .analytical_resistance import AnalyticalResistorEngine
from .geometry_graph import LayoutGraphExtractor, RectangleElement, StackupConfig, synthetic_stackup
from .via_connectivity import resolve_layout_connectivity

_REQUIRED_WEIGHT_KEYS = (
    'v_mean', 'v_std', 'e_mean', 'e_std',
    'W_v0', 'b_v0', 'W_e0', 'b_e0',
    'W_em1', 'W_eh1', 'b_e1', 'W_nh1', 'W_na1', 'b_n1',
    'W_em2', 'W_eh2', 'b_e2', 'W_nh2', 'W_na2', 'b_n2',
    'W_er1', 'b_er1', 'W_er2', 'b_er2',
    'W_nr1', 'b_nr1', 'W_nr2', 'b_nr2',
    'bias_shift_e', 'bias_shift_n',
)

_WEIGHT_CACHE: Dict[Tuple[str, int, str], Dict[str, np.ndarray]] = {}


def _sigmoid(x: np.ndarray) -> np.ndarray:
    """Numerically stable Sigmoid activation."""
    return 1.0 / (1.0 + np.exp(-np.clip(x, -40.0, 40.0)))


def _silu(x: np.ndarray) -> np.ndarray:
    return x * _sigmoid(x)


def _softplus(x: np.ndarray) -> np.ndarray:
    return np.where(x > 20.0, x, np.log1p(np.exp(np.clip(x, -40.0, 20.0))))


def load_npz_weights(
    weights_path: Union[str, Path],
    expected_sha256: Optional[str] = None,
) -> Dict[str, np.ndarray]:
    """Load and validate an ``.npz`` Geo-Graph weight archive, with mtime caching."""
    path = Path(weights_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f'Surrogate weights file not found: {path}')
    stat = path.stat()
    cache_key = (str(path), stat.st_mtime_ns, expected_sha256 or '')
    cached = _WEIGHT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    raw = path.read_bytes()
    if expected_sha256:
        actual = hashlib.sha256(raw).hexdigest().lower()
        if actual != expected_sha256.lower():
            raise ValueError(f'Surrogate weights SHA-256 mismatch for {path.name}')

    with np.load(path, allow_pickle=False) as archive:
        missing = [k for k in _REQUIRED_WEIGHT_KEYS if k not in archive.files]
        if missing:
            raise ValueError(f'Surrogate weights archive missing keys: {missing}')
        weights = {k: np.asarray(archive[k], dtype=np.float32) for k in archive.files}

    if len(_WEIGHT_CACHE) >= 8:
        _WEIGHT_CACHE.clear()
    _WEIGHT_CACHE[cache_key] = weights
    return weights


class GeoGraphRCEngine:
    """Pure-NumPy Geo-Graph Residual MLP for via-aware capacitance and 3D resistance."""

    def __init__(
        self,
        weights_path: Optional[Union[str, Path]] = None,
        *,
        expected_sha256: Optional[str] = None,
        weights: Optional[Mapping[str, np.ndarray]] = None,
        r_halo_um: float = 12.0,
        layer_window: int = 2,
        stackup: Optional[StackupConfig] = None,
    ):
        self.stackup = stackup or synthetic_stackup()
        self.extractor = LayoutGraphExtractor(
            r_halo_um=r_halo_um,
            layer_window=layer_window,
            stackup=self.stackup,
        )
        self.weights: Optional[Dict[str, np.ndarray]] = None
        if weights is not None:
            self.load_weight_dict(weights)
        elif weights_path is not None:
            self.load_weights(weights_path, expected_sha256=expected_sha256)

    def load_weights(
        self,
        weights_path: Union[str, Path],
        *,
        expected_sha256: Optional[str] = None,
    ) -> None:
        """Load weight tensors from an ``.npz`` file on disk."""
        self.weights = load_npz_weights(weights_path, expected_sha256=expected_sha256)

    def load_weight_dict(self, weights: Mapping[str, Any]) -> None:
        """Load validated float32 weight arrays from an in-memory mapping."""
        missing = [k for k in _REQUIRED_WEIGHT_KEYS if k not in weights]
        if missing:
            raise ValueError(f'Surrogate weight dict missing keys: {missing}')
        self.weights = {k: np.asarray(v, dtype=np.float32) for k, v in weights.items()}

    def predict_window_rc(
        self,
        active_elements: Sequence[RectangleElement],
        bg_elements: Optional[Sequence[RectangleElement]] = None,
        *,
        resolve_vias: bool = True,
        compute_r_nets: Optional[Sequence[str]] = None,
        temp_c: float = 25.0,
    ) -> Dict[str, Any]:
        """Predict per-net/element ``C_gnd``, ``C_cpl``, and 3D ``R`` for a window."""
        t0 = time.perf_counter()
        if not active_elements:
            return {
                'net_c_tot': {},
                'net_c_gnd': {},
                'pair_c_cpl': {},
                'net_r_ohm': {},
                'merged_nets': {},
                'elements': [],
                'element_c_gnd': [],
                'element_pair_c_cpl': {},
                'elapsed_ms': 0.0,
            }

        cfg = self.stackup
        ground_nets = set(cfg.ground_nets)
        merged_nets: Dict[str, List[str]] = {}

        if resolve_vias and any(el.is_via for el in (bg_elements or active_elements)):
            if bg_elements is not None:
                active_ids = {el.id for el in active_elements}
                conn = resolve_layout_connectivity(
                    bg_elements,
                    via_specs=cfg.via_stack_spec,
                    ground_nets=cfg.ground_nets,
                )
                bg_use = conn.clustered_elements
                merged_nets = conn.merged_net_map
                bx0 = min(el.x0 for el in active_elements) - 0.5
                by0 = min(el.y0 for el in active_elements) - 0.5
                bx1 = max(el.x1 for el in active_elements) + 0.5
                by1 = max(el.y1 for el in active_elements) + 0.5
                lo_pos = min(el.layer_pos for el in active_elements) - 0.6
                hi_pos = max(el.layer_pos for el in active_elements) + 0.6
                act_use = [
                    el
                    for el in bg_use
                    if (el.id in active_ids)
                    or (
                        lo_pos <= el.layer_pos <= hi_pos
                        and el.x1 >= bx0
                        and el.x0 <= bx1
                        and el.y1 >= by0
                        and el.y0 <= by1
                    )
                ]
            else:
                conn = resolve_layout_connectivity(
                    active_elements,
                    via_specs=cfg.via_stack_spec,
                    ground_nets=cfg.ground_nets,
                )
                act_use = conn.clustered_elements
                bg_use = act_use
                merged_nets = conn.merged_net_map
        else:
            act_use = list(active_elements)
            bg_use = list(bg_elements) if bg_elements is not None else act_use

        graph = self.extractor.extract_graph(
            active_elements=act_use,
            all_elements_for_ground_shield=bg_use,
        )
        v_raw = graph['node_features']
        e_idx = graph['edge_index']
        e_raw = graph['edge_features']

        n_edges = e_raw.shape[0]
        c_gnd_base = v_raw[:, 16] + v_raw[:, 17]
        c_cpl_base = (
            (e_raw[:, 16] + e_raw[:, 17]) if n_edges > 0 else np.zeros((0,), dtype=np.float32)
        )

        if self.weights is not None:
            w = self.weights
            arch_ver = (
                int(round(float(np.asarray(w['arch_version']).reshape(-1)[0])))
                if 'arch_version' in w
                else 2
            )
            v_dim = int(w['v_mean'].shape[0])
            e_dim = int(w['e_mean'].shape[0])
            v_in = v_raw[:, :v_dim]
            v_norm = (v_in - w['v_mean']) / np.maximum(w['v_std'], 1e-5)
            h = _silu(v_norm @ w['W_v0'] + w['b_v0'])

            if n_edges > 0:
                src, dst = e_idx[0], e_idx[1]
                e_in = e_raw[:, :e_dim]
                e_norm = (e_in - w['e_mean']) / np.maximum(w['e_std'], 1e-5)
                m = _silu(e_norm @ w['W_e0'] + w['b_e0'])

                if arch_ver >= 3:
                    deg = np.bincount(dst, minlength=v_raw.shape[0]).astype(np.float32)
                    inv_deg = (1.0 / np.maximum(deg, 1.0))[:, None]
                else:
                    inv_deg = None

                for k in (1, 2):
                    u = _silu(
                        m @ w[f'W_em{k}']
                        + (h[src] + h[dst]) @ w[f'W_eh{k}']
                        + w[f'b_e{k}']
                    )
                    m = m + u
                    agg = np.zeros_like(h)
                    np.add.at(agg, dst, m)
                    if inv_deg is not None:
                        agg = agg * inv_deg
                    h = h + _silu(h @ w[f'W_nh{k}'] + agg @ w[f'W_na{k}'] + w[f'b_n{k}'])

                e_hid = _silu(m @ w['W_er1'] + w['b_er1'])
                e_out = e_hid @ w['W_er2'] + w['b_er2']
                if arch_ver >= 3:
                    alpha_e = 2.4 * _sigmoid(e_out[:, 0] + w['bias_shift_e'])
                    d_eff = e_raw[:, 7] + 0.5 * e_raw[:, 5]
                    screen_e = (
                        np.maximum(0.0, 1.0 - 0.95 * e_raw[:, 12])
                        * np.maximum(0.0, 1.0 - 0.85 * e_raw[:, 11])
                        * np.exp(-d_eff / 1.5)
                    )
                    e_scale = (
                        e_raw[:, 8] + (e_raw[:, 19] if e_raw.shape[1] > 19 else 0.0)
                    ) * screen_e
                    beta_e = 0.30 * _sigmoid(e_out[:, 1] - 2.5) * e_scale
                else:
                    alpha_e = _softplus(e_out[:, 0] + w['bias_shift_e'])
                    e_scale = e_raw[:, 8] + (e_raw[:, 19] if e_raw.shape[1] > 19 else 0.0)
                    beta_e = _softplus(e_out[:, 1] - 4.0) * e_scale
                c_edge_dir = c_cpl_base * alpha_e + beta_e
            else:
                for k in (1, 2):
                    h = h + _silu(h @ w[f'W_nh{k}'] + w[f'b_n{k}'])
                c_edge_dir = np.zeros((0,), dtype=np.float32)

            n_hid = _silu(h @ w['W_nr1'] + w['b_nr1'])
            n_out = n_hid @ w['W_nr2'] + w['b_nr2']
            if arch_ver >= 3:
                alpha_n = 2.4 * _sigmoid(n_out[:, 0] + w['bias_shift_n'])
                beta_n = 0.35 * _sigmoid(n_out[:, 1] - 2.5) * (v_raw[:, 3] + 0.12 * v_raw[:, 4])
            else:
                alpha_n = _softplus(n_out[:, 0] + w['bias_shift_n'])
                beta_n = _softplus(n_out[:, 1] - 4.0) * v_raw[:, 3]
            c_node_gnd = c_gnd_base * alpha_n + beta_n
        else:
            c_node_gnd = c_gnd_base
            c_edge_dir = c_cpl_base

        net_c_gnd: Dict[str, float] = {}
        net_c_tot: Dict[str, float] = {}
        pair_c_cpl: Dict[Tuple[str, str], float] = {}
        element_c_gnd = [
            0.0 if el.net_name in ground_nets else float(max(0.0, c_node_gnd[i]))
            for i, el in enumerate(act_use)
        ]
        element_pair_c_cpl: Dict[Tuple[int, int], float] = {}

        for i, el in enumerate(act_use):
            if el.net_name in ground_nets:
                continue
            cg = element_c_gnd[i]
            net_c_gnd[el.net_name] = net_c_gnd.get(el.net_name, 0.0) + cg
            net_c_tot[el.net_name] = net_c_tot.get(el.net_name, 0.0) + cg

        if n_edges > 0:
            edge_map: Dict[Tuple[int, int], float] = {}
            for e_i in range(n_edges):
                u_i, v_i = int(e_idx[0, e_i]), int(e_idx[1, e_i])
                net_u = act_use[u_i].net_name
                net_v = act_use[v_i].net_name
                if net_u == net_v:
                    continue
                key_uv = (min(u_i, v_i), max(u_i, v_i))
                edge_map[key_uv] = edge_map.get(key_uv, 0.0) + 0.5 * float(max(0.0, c_edge_dir[e_i]))

            for (u_i, v_i), c_val in edge_map.items():
                net_u = act_use[u_i].net_name
                net_v = act_use[v_i].net_name
                u_gnd = net_u in ground_nets
                v_gnd = net_v in ground_nets
                if u_gnd and not v_gnd:
                    element_c_gnd[v_i] += c_val
                    net_c_gnd[net_v] = net_c_gnd.get(net_v, 0.0) + c_val
                    net_c_tot[net_v] = net_c_tot.get(net_v, 0.0) + c_val
                elif v_gnd and not u_gnd:
                    element_c_gnd[u_i] += c_val
                    net_c_gnd[net_u] = net_c_gnd.get(net_u, 0.0) + c_val
                    net_c_tot[net_u] = net_c_tot.get(net_u, 0.0) + c_val
                elif not u_gnd and not v_gnd:
                    element_pair_c_cpl[(u_i, v_i)] = c_val
                    net_c_tot[net_u] = net_c_tot.get(net_u, 0.0) + c_val
                    net_c_tot[net_v] = net_c_tot.get(net_v, 0.0) + c_val
                    n_key = (net_u, net_v) if net_u < net_v else (net_v, net_u)
                    pair_c_cpl[n_key] = pair_c_cpl.get(n_key, 0.0) + c_val

        if compute_r_nets is None:
            r_targets = {el.net_name for el in act_use if el.net_name not in ground_nets}
        else:
            r_targets = {n for n in compute_r_nets if n not in ground_nets}
        net_r_ohm = {
            net: AnalyticalResistorEngine.calc_net_elements_r(
                act_use, net, temp_c=temp_c, stackup=cfg
            )
            for net in r_targets
        }

        elapsed_ms = (time.perf_counter() - t0) * 1e3
        return {
            'net_c_tot': net_c_tot,
            'net_c_gnd': net_c_gnd,
            'pair_c_cpl': pair_c_cpl,
            'net_r_ohm': net_r_ohm,
            'merged_nets': merged_nets,
            'elements': act_use,
            'element_c_gnd': element_c_gnd,
            'element_pair_c_cpl': element_pair_c_cpl,
            'elapsed_ms': elapsed_ms,
        }

    def predict_incremental_rc(
        self,
        existing_elements: Sequence[RectangleElement],
        new_element: Union[RectangleElement, Sequence[RectangleElement]],
        *,
        temp_c: float = 25.0,
    ) -> Dict[str, Any]:
        """Evaluate before/after RC deltas when placing planar traces or vias."""
        t0 = time.perf_counter()
        if isinstance(new_element, RectangleElement):
            new_elems = [new_element]
        else:
            new_elems = list(new_element)

        if not new_elems:
            return {'self_net': {}, 'affected_neighbors': [], 'elapsed_ms': 0.0}

        cfg = self.stackup
        ground_nets = set(cfg.ground_nets)
        rep_el = new_elems[0]
        union_bbox = (
            min(e.x0 for e in new_elems),
            min(e.y0 for e in new_elems),
            max(e.x1 for e in new_elems),
            max(e.y1 for e in new_elems),
        )

        conn_before = resolve_layout_connectivity(
            existing_elements,
            via_specs=cfg.via_stack_spec,
            ground_nets=cfg.ground_nets,
        )
        conn_after = resolve_layout_connectivity(
            list(existing_elements) + new_elems,
            via_specs=cfg.via_stack_spec,
            ground_nets=cfg.ground_nets,
        )

        win_before = self.extractor.filter_elements(
            conn_before.clustered_elements,
            target_layer=rep_el.layer,
            target_bbox=union_bbox,
        )
        win_after = self.extractor.filter_elements(
            conn_after.clustered_elements,
            target_layer=rep_el.layer,
            target_bbox=union_bbox,
        )

        target_net = conn_after.resolved_raw_elements[-1].net_name
        pre_nets_merged = sorted({
            conn_before.resolved_raw_elements[idx].net_name
            for idx in range(len(existing_elements))
            if conn_after.resolved_raw_elements[idx].net_name == target_net
        })
        topology_merged = len(pre_nets_merged) > 1

        res_before = self.predict_window_rc(
            win_before,
            bg_elements=conn_before.clustered_elements,
            resolve_vias=False,
            compute_r_nets=pre_nets_merged or [target_net],
            temp_c=temp_c,
        )
        res_after = self.predict_window_rc(
            win_after,
            bg_elements=conn_after.clustered_elements,
            resolve_vias=False,
            compute_r_nets=[target_net],
            temp_c=temp_c,
        )

        if topology_merged:
            c_before_branches = {m: res_before['net_c_tot'].get(m, 0.0) for m in pre_nets_merged}
            r_before_branches = {m: res_before['net_r_ohm'].get(m, 0.0) for m in pre_nets_merged}
            c_before_self = sum(c_before_branches.values())
            eliminated_cpl = 0.0
            for i_m in range(len(pre_nets_merged)):
                for j_m in range(i_m + 1, len(pre_nets_merged)):
                    na, nb = pre_nets_merged[i_m], pre_nets_merged[j_m]
                    k_ab = (na, nb) if na < nb else (nb, na)
                    eliminated_cpl += 2.0 * res_before['pair_c_cpl'].get(k_ab, 0.0)
            r_before_self = sum(r_before_branches.values())
        else:
            prev_name = pre_nets_merged[0] if pre_nets_merged else target_net
            c_before_branches = (
                {prev_name: res_before['net_c_tot'].get(prev_name, 0.0)} if pre_nets_merged else {}
            )
            r_before_branches = (
                {prev_name: res_before['net_r_ohm'].get(prev_name, 0.0)} if pre_nets_merged else {}
            )
            c_before_self = res_before['net_c_tot'].get(prev_name, 0.0)
            r_before_self = res_before['net_r_ohm'].get(prev_name, 0.0)
            eliminated_cpl = 0.0

        c_after_self = res_after['net_c_tot'].get(target_net, 0.0)
        r_after_self = res_after['net_r_ohm'].get(target_net, 0.0)

        excluded = set(pre_nets_merged) | {target_net} | ground_nets
        all_neighbor_nets = (
            set(res_before['net_c_tot'].keys()) | set(res_after['net_c_tot'].keys())
        ) - excluded

        neighbor_deltas = []
        for n_net in sorted(all_neighbor_nets):
            cb = res_before['net_c_tot'].get(n_net, 0.0)
            ca = res_after['net_c_tot'].get(n_net, 0.0)
            dc = ca - cb
            cgb = res_before['net_c_gnd'].get(n_net, 0.0)
            cga = res_after['net_c_gnd'].get(n_net, 0.0)
            dc_gnd = cga - cgb

            cpl_after = res_after['pair_c_cpl'].get(
                (target_net, n_net) if target_net < n_net else (n_net, target_net),
                0.0,
            )
            cpl_before = sum(
                res_before['pair_c_cpl'].get(
                    (pm, n_net) if pm < n_net else (n_net, pm),
                    0.0,
                )
                for pm in (pre_nets_merged or [target_net])
            )
            c_cpl_new = cpl_after - cpl_before

            neighbor_deltas.append({
                'net_name': n_net,
                'c_tot_before_ff': cb,
                'c_tot_after_ff': ca,
                'delta_c_tot_ff': dc,
                'delta_c_pct': (100.0 * dc / cb) if cb > 1e-6 else 0.0,
                'delta_c_gnd_shield_ff': dc_gnd,
                'delta_c_cpl_ff': c_cpl_new,
            })

        neighbor_deltas.sort(key=lambda d: abs(d['delta_c_tot_ff']), reverse=True)
        total_ms = (time.perf_counter() - t0) * 1e3

        return {
            'self_net': {
                'net_name': target_net,
                'layer': rep_el.layer,
                'is_via': rep_el.is_via,
                'topology_merged': topology_merged,
                'merged_from_nets': pre_nets_merged,
                'c_before_branches_ff': c_before_branches,
                'eliminated_interlayer_c_cpl_ff': eliminated_cpl,
                'c_tot_before_ff': c_before_self,
                'c_tot_after_ff': c_after_self,
                'delta_c_tot_ff': c_after_self - c_before_self,
                'r_before_branches_ohm': r_before_branches,
                'r_before_ohm': r_before_self,
                'r_after_ohm': r_after_self,
                'delta_r_ohm': r_after_self - r_before_self,
            },
            'affected_neighbors': neighbor_deltas,
            'elapsed_ms': total_ms,
        }
