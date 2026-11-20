"""Bridge between CoLayout frozen geometries and the pure-NumPy Geo-Graph RC engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from shapely.geometry import Polygon, box

from .parasitic_models import RcModel
from .parasitic_nn import GeoGraphRCEngine, RectangleElement
from .parasitic_rc import (
    CouplingImpact,
    GeometryContribution,
    GeometrySet,
    RcCancelled,
    RcEstimate,
    _Complexity,
    _components,
    _relation,
    _vertices,
)


@dataclass(frozen=True)
class _ElemOrigin:
    side: str  # 'target' or 'neighbor'
    component_key: str
    component_id: str
    layer: str
    mapped_layer: str
    is_via: bool
    sources: Tuple[str, ...]
    nets: Tuple[str, ...]
    geometry: object


def _is_axis_aligned_rect(geom) -> Optional[Tuple[float, float, float, float]]:
    if geom.geom_type != 'Polygon' or geom.interiors or geom.is_empty:
        return None
    coords = list(geom.exterior.coords)
    if len(coords) != 5:
        return None
    minx, miny, maxx, maxy = geom.bounds
    if maxx - minx <= 1e-9 or maxy - miny <= 1e-9:
        return None
    bbox_area = (maxx - minx) * (maxy - miny)
    if abs(geom.area - bbox_area) <= max(1e-9, 1e-5 * bbox_area):
        return (minx, miny, maxx, maxy)
    return None


def _polygon_to_boxes(
    geom,
    context,
    *,
    min_dim_um: float = 1e-4,
    max_boxes_per_geom: int = 128,
) -> Tuple[List[Tuple[float, float, float, float]], bool]:
    """Decompose a Shapely Polygon/MultiPolygon into axis-aligned ``(x0, y0, x1, y1)`` boxes."""
    if geom.is_empty:
        return [], False
    polygons = [geom] if geom.geom_type == 'Polygon' else [
        g for g in getattr(geom, 'geoms', ()) if g.geom_type == 'Polygon' and not g.is_empty
    ]
    boxes: List[Tuple[float, float, float, float]] = []
    approximated = False

    for poly in polygons:
        context.checkpoint()
        rect = _is_axis_aligned_rect(poly)
        if rect is not None:
            boxes.append(rect)
            continue

        coords = list(poly.exterior.coords)
        all_pts = list(coords)
        for ring in poly.interiors:
            all_pts.extend(ring.coords)
        is_ortho = all(
            abs(a[0] - b[0]) <= 1e-6 or abs(a[1] - b[1]) <= 1e-6
            for ring in (poly.exterior, *poly.interiors)
            for a, b in zip(ring.coords, list(ring.coords)[1:])
        )
        if not is_ortho:
            approximated = True
            minx, miny, maxx, maxy = poly.bounds
            if maxx - minx > min_dim_um and maxy - miny > min_dim_um:
                boxes.append((minx, miny, maxx, maxy))
            continue

        ys = sorted({round(pt[1], 6) for pt in all_pts})
        minx, _, maxx, _ = poly.bounds
        strips: List[Tuple[float, float, float, float]] = []
        for ya, yb in zip(ys[:-1], ys[1:]):
            if yb - ya <= min_dim_um:
                continue
            context.checkpoint()
            sliced = poly.intersection(box(minx - 1.0, ya, maxx + 1.0, yb))
            parts = [sliced] if sliced.geom_type == 'Polygon' else [
                g for g in getattr(sliced, 'geoms', ()) if g.geom_type == 'Polygon'
            ]
            for part in parts:
                px0, py0, px1, py1 = part.bounds
                if px1 - px0 > min_dim_um and py1 - py0 > min_dim_um:
                    strips.append((px0, py0, px1, py1))
            if len(strips) > max_boxes_per_geom:
                approximated = True
                b = poly.bounds
                strips = [(b[0], b[1], b[2], b[3])]
                break

        # Merge vertically adjacent strips with matching X spans
        strips.sort(key=lambda item: (round(item[0], 6), round(item[2], 6), item[1]))
        merged: List[Tuple[float, float, float, float]] = []
        for sx0, sy0, sx1, sy1 in strips:
            if (
                merged
                and abs(merged[-1][0] - sx0) <= 1e-6
                and abs(merged[-1][2] - sx1) <= 1e-6
                and abs(merged[-1][3] - sy0) <= 1e-6
            ):
                prev = merged[-1]
                merged[-1] = (prev[0], prev[1], prev[2], sy1)
            else:
                merged.append((sx0, sy0, sx1, sy1))
        boxes.extend(merged)

    return boxes, approximated


def _resolve_cut_to_surrogate_via(
    layer_name: str,
    surrogate,
    profile,
    stackup,
) -> Optional[str]:
    if layer_name in surrogate.via_map:
        mapped = surrogate.via_map[layer_name].upper()
        return mapped if mapped in stackup.via_stack_spec else None

    upper = layer_name.upper()
    if upper in stackup.via_stack_spec:
        return upper

    for def_name, vdef in getattr(profile, 'vias', {}).items():
        if vdef.get('cut_layer') != layer_name:
            continue
        if def_name in surrogate.via_map:
            mapped = surrogate.via_map[def_name].upper()
            if mapped in stackup.via_stack_spec:
                return mapped
        l1 = surrogate.layer_map.get(vdef.get('layer1', ''), vdef.get('layer1', '')).upper()
        l2 = surrogate.layer_map.get(vdef.get('layer2', ''), vdef.get('layer2', '')).upper()
        for vname, vspec in stackup.via_stack_spec.items():
            if {vspec.bot_layer, vspec.top_layer} == {l1, l2}:
                return vname
    return None


def _resolve_layer_name(
    raw_layer: str,
    surrogate,
    profile,
    stackup,
) -> Tuple[Optional[str], bool]:
    """Return ``(stackup_layer_name, is_via)`` or ``(None, False)`` if unmapped."""
    if raw_layer in surrogate.layer_map:
        mapped = surrogate.layer_map[raw_layer].upper()
        if mapped in stackup.layer_to_idx:
            return mapped, False
    via_mapped = _resolve_cut_to_surrogate_via(raw_layer, surrogate, profile, stackup)
    if via_mapped is not None:
        return via_mapped, True
    upper = raw_layer.upper()
    if upper in stackup.layer_to_idx:
        return upper, False
    return None, False


def _element_net_name(nets: Tuple[str, ...], component_id: str, is_via: bool, uid: str) -> str:
    if len(nets) == 1:
        return nets[0]
    if is_via and not nets:
        return f'__AUTO_VIA_{uid}'
    return f'__COMP_{component_id}'


def _relation_from_nets(nets_a: Tuple[str, ...], nets_b: Tuple[str, ...]) -> str:
    if len(nets_a) == len(nets_b) == 1:
        return 'same_net' if nets_a == nets_b else 'different_net'
    return 'unknown_net'


def _impact_from_elements(
    first_ori: _ElemOrigin,
    second_ori: _ElemOrigin,
    c_val: float,
    internal: bool,
) -> CouplingImpact:
    spacing = float(first_ori.geometry.distance(second_ori.geometry))
    relation = _relation_from_nets(first_ori.nets, second_ori.nets)
    key = '|'.join(sorted((first_ori.component_key, second_ori.component_key)))
    return CouplingImpact(
        key=key,
        target_component=first_ori.component_key,
        neighbor_component=second_ori.component_key,
        target_sources=first_ori.sources,
        neighbor_sources=second_ori.sources,
        layer=first_ori.layer,
        relation=relation,
        capacitance_fF=float(c_val),
        status='complete',
        spacing_um=spacing,
        geometry=second_ori.geometry,
        target_internal=internal,
    )


def analyze_geometry_surrogate(
    target: GeometrySet,
    neighbors: GeometrySet,
    profile,
    model: RcModel,
    context,
) -> RcEstimate:
    """Evaluate RC parasitics using the Geo-Graph neural surrogate + 3D nodal resistance."""
    surrogate = model.surrogate
    assert surrogate is not None
    stackup = surrogate.build_stackup()
    halo = (
        min(context.budget.analysis_radius_um, surrogate.r_halo_um)
        if context.budget.analysis_radius_um > 0
        else surrogate.r_halo_um
    )

    try:
        context.checkpoint()
        total_raw = len(target.conductors) + len(neighbors.conductors)
        if total_raw > context.budget.max_objects:
            raise _Complexity('Conductor count exceeds the real-time budget')
        if sum(_vertices(row.geometry) for row in (*target.conductors, *neighbors.conductors)) > context.budget.max_total_vertices:
            raise _Complexity('Total vertex count exceeds the real-time budget')

        # Build physical components per side while keeping individual via cuts un-unioned so
        # multi-cut via arrays preserve exact cut counts for ViaCluster resolution.
        def prepare_rows(rows: Sequence[GeometryContribution]):
            planar = [
                r for r in rows if not _resolve_layer_name(r.layer, surrogate, profile, stackup)[1]
            ]
            vias = [
                r for r in rows if _resolve_layer_name(r.layer, surrogate, profile, stackup)[1]
            ]
            comps = _components(planar, context)
            return comps, vias

        target_comps, target_vias = prepare_rows(target.conductors)
        neighbor_comps, neighbor_vias = prepare_rows(neighbors.conductors)

        elements: List[RectangleElement] = []
        origins: Dict[int, _ElemOrigin] = {}
        ground_coverage = 'complete'
        has_target_non_ortho = False
        target_ground_net_only = False
        next_el_id = 1

        def add_component_item(
            side: str,
            comp_key: str,
            comp_id: str,
            raw_layer: str,
            geom,
            sources: Tuple[str, ...],
            nets: Tuple[str, ...],
        ):
            nonlocal ground_coverage, has_target_non_ortho, target_ground_net_only, next_el_id
            mapped_layer, is_via = _resolve_layer_name(raw_layer, surrogate, profile, stackup)
            if mapped_layer is None:
                if side == 'target':
                    ground_coverage = 'partial'
                    context.warnings.append(f'{raw_layer}: unmapped in RC surrogate stackup')
                return

            net_name = _element_net_name(nets, comp_id, is_via, sources[0] if sources else comp_id)
            if side == 'target' and net_name in stackup.ground_nets:
                target_ground_net_only = True

            boxes, approx = _polygon_to_boxes(geom, context)
            if approx and side == 'target':
                has_target_non_ortho = True
            for bx0, by0, bx1, by1 in boxes:
                el = RectangleElement(
                    id=next_el_id,
                    net_name=net_name,
                    layer=mapped_layer,
                    x0=bx0,
                    y0=by0,
                    x1=bx1,
                    y1=by1,
                    num_cuts=1,
                    stackup=stackup,
                )
                origins[next_el_id] = _ElemOrigin(
                    side=side,
                    component_key=comp_key,
                    component_id=comp_id,
                    layer=raw_layer,
                    mapped_layer=mapped_layer,
                    is_via=el.is_via,
                    sources=sources,
                    nets=nets,
                    geometry=geom,
                )
                elements.append(el)
                next_el_id += 1

        for comp in target_comps:
            add_component_item(
                'target', comp.key, comp.identity, comp.layer,
                comp.geometry, comp.sources, comp.nets,
            )
        for vrow in target_vias:
            vnets = (vrow.net,) if vrow.net else ()
            add_component_item(
                'target', vrow.key, vrow.component_id, vrow.layer,
                vrow.geometry, (vrow.owner_uid,), vnets,
            )

        target_box_union = None
        target_els = [el for el in elements if origins[el.id].side == 'target']
        if target_els:
            tx0 = min(el.x0 for el in target_els) - halo - 0.5
            ty0 = min(el.y0 for el in target_els) - halo - 0.5
            tx1 = max(el.x1 for el in target_els) + halo + 0.5
            ty1 = max(el.y1 for el in target_els) + halo + 0.5
            target_box_union = (tx0, ty0, tx1, ty1)

        def in_halo(geom) -> bool:
            if target_box_union is None:
                return True
            gx0, gy0, gx1, gy1 = geom.bounds
            tx0, ty0, tx1, ty1 = target_box_union
            return not (gx1 < tx0 or gx0 > tx1 or gy1 < ty0 or gy0 > ty1)

        for comp in neighbor_comps:
            if not in_halo(comp.geometry):
                continue
            add_component_item(
                'neighbor', comp.key, comp.identity, comp.layer,
                comp.geometry, comp.sources, comp.nets,
            )
        for vrow in neighbor_vias:
            if not in_halo(vrow.geometry):
                continue
            vnets = (vrow.net,) if vrow.net else ()
            add_component_item(
                'neighbor', vrow.key, vrow.component_id, vrow.layer,
                vrow.geometry, (vrow.owner_uid,), vnets,
            )

        if has_target_non_ortho:
            context.warnings.append('non_orthogonal_approximated')

        if not any(origins[el.id].side == 'target' for el in elements):
            return RcEstimate(
                None, 0.0, 0.0,
                'unavailable', ground_coverage, 'complete',
                warnings=tuple(dict.fromkeys(context.warnings)),
                mode=surrogate.kind,
            )

        context.checkpoint()
        engine = GeoGraphRCEngine(
            weights_path=surrogate.weights_path,
            expected_sha256=surrogate.weights_sha256,
            r_halo_um=halo,
            layer_window=surrogate.layer_window,
            stackup=stackup,
        )
        if surrogate.weights_path is None:
            context.warnings.append('surrogate_weights_unconfigured_physics_baseline')

        target_elements_only = [el for el in elements if origins[el.id].side == 'target']
        # Run connectivity resolution globally first so ViaClusters track original origins
        from .parasitic_nn.via_connectivity import resolve_layout_connectivity

        conn = resolve_layout_connectivity(
            elements,
            via_specs=stackup.via_stack_spec,
            ground_nets=stackup.ground_nets,
        )
        clustered_els = conn.clustered_elements

        # Map clustered ViaCluster IDs (not present in `origins`) back to constituent raw via origin
        raw_vias_by_net_layer: Dict[Tuple[str, str], List[Tuple[object, _ElemOrigin]]] = {}
        for raw_el in conn.resolved_raw_elements:
            if raw_el.is_via and raw_el.id in origins:
                raw_vias_by_net_layer.setdefault(
                    (raw_el.net_name, raw_el.layer), []
                ).append((raw_el, origins[raw_el.id]))
        for cel in clustered_els:
            if cel.id not in origins:
                candidates = raw_vias_by_net_layer.get((cel.net_name, cel.layer), ())
                inside = [
                    ori
                    for raw_el, ori in candidates
                    if (
                        raw_el.x0 >= cel.x0 - 1e-4
                        and raw_el.x1 <= cel.x1 + 1e-4
                        and raw_el.y0 >= cel.y0 - 1e-4
                        and raw_el.y1 <= cel.y1 + 1e-4
                    )
                ]
                chosen = next((ori for ori in inside if ori.side == 'target'), None) or (
                    inside[0] if inside else (candidates[0][1] if candidates else None)
                )
                if chosen is not None:
                    origins[cel.id] = _ElemOrigin(
                        side=chosen.side,
                        component_key=chosen.component_key,
                        component_id=chosen.component_id,
                        layer=chosen.layer,
                        mapped_layer=chosen.mapped_layer,
                        is_via=chosen.is_via,
                        sources=tuple(sorted({s for ori in inside for s in ori.sources})) or chosen.sources,
                        nets=chosen.nets,
                        geometry=box(*cel.bbox),
                    )

        target_pos_vals = [el.layer_pos for el in target_elements_only]
        min_t_pos = min(target_pos_vals) - surrogate.layer_window - 0.55
        max_t_pos = max(target_pos_vals) + surrogate.layer_window + 0.55
        active_els = [
            el
            for el in clustered_els
            if (el.id in origins and origins[el.id].side == 'target')
            or (min_t_pos <= el.layer_pos <= max_t_pos)
        ]

        context.checkpoint()
        prediction = engine.predict_window_rc(
            active_els,
            bg_elements=clustered_els,
            resolve_vias=False,
            temp_c=model.temperature_c,
        )

        out_els = prediction['elements']
        elem_cgnd = prediction['element_c_gnd']
        elem_cpl = prediction['element_pair_c_cpl']

        ground_fF = 0.0
        target_signal_present = False
        target_nets_for_r: List[str] = []
        seen_r_nets = set()
        for idx_el, el in enumerate(out_els):
            ori = origins.get(el.id)
            if ori is None or ori.side != 'target':
                continue
            ground_fF += elem_cgnd[idx_el]
            if el.net_name not in stackup.ground_nets:
                target_signal_present = True
                if el.net_name not in seen_r_nets:
                    seen_r_nets.add(el.net_name)
                    target_nets_for_r.append(el.net_name)

        if target_ground_net_only and not target_signal_present:
            if ground_coverage == 'complete':
                ground_coverage = 'partial'
            context.warnings.append('ground_net_reference_excluded')

        # Group pairwise couplings per component/layer pair
        pair_totals: Dict[Tuple[str, str, bool], Tuple[_ElemOrigin, _ElemOrigin, float]] = {}
        for (u_i, v_i), c_val in elem_cpl.items():
            el_u, el_v = out_els[u_i], out_els[v_i]
            ori_u = origins.get(el_u.id)
            ori_v = origins.get(el_v.id)
            if ori_u is None or ori_v is None:
                continue
            if ori_u.side != 'target' and ori_v.side != 'target':
                continue
            if ori_u.component_id == ori_v.component_id and ori_u.layer == ori_v.layer:
                continue
            if ori_u.side == 'target' and ori_v.side == 'target':
                if ori_u.component_id == ori_v.component_id:
                    continue
                internal = True
                first_ori, second_ori = (
                    (ori_u, ori_v)
                    if ori_u.component_key <= ori_v.component_key
                    else (ori_v, ori_u)
                )
            else:
                internal = False
                first_ori, second_ori = (ori_u, ori_v) if ori_u.side == 'target' else (ori_v, ori_u)

            agg_key = (first_ori.component_key, second_ori.component_key, internal)
            if agg_key in pair_totals:
                prev_f, prev_s, prev_c = pair_totals[agg_key]
                pair_totals[agg_key] = (prev_f, prev_s, prev_c + c_val)
            else:
                pair_totals[agg_key] = (first_ori, second_ori, c_val)

        impacts = [
            _impact_from_elements(first_ori, second_ori, c_val, internal)
            for first_ori, second_ori, c_val in pair_totals.values()
            if c_val > 0.0
        ]
        impacts.sort(key=lambda imp: (imp.target_component, imp.neighbor_component, imp.key))
        coupling_fF = sum(imp.capacitance_fF for imp in impacts if imp.capacitance_fF is not None)

        # Compute driving-point 3D nodal R when target forms one connected net or simple series
        can_report_r = bool(target_elements_only) and (
            len(target_nets_for_r) == 1
            and (target.simple_series or any(origins[e.id].is_via for e in target_elements_only))
        )
        if can_report_r:
            resistance_ohm = float(prediction['net_r_ohm'].get(target_nets_for_r[0], 0.0))
            resistance_status = 'complete'
        elif target_nets_for_r:
            resistance_ohm = None
            resistance_status = 'partial'
        else:
            resistance_ohm = None
            resistance_status = 'unavailable'

        return RcEstimate(
            resistance_ohm=resistance_ohm,
            ground_capacitance_fF=float(ground_fF),
            coupling_capacitance_fF=float(coupling_fF),
            resistance_status=resistance_status,
            ground_status=ground_coverage,
            coupling_status='complete',
            impacts=tuple(impacts),
            warnings=tuple(dict.fromkeys(context.warnings)),
            mode=surrogate.kind,
            elapsed_ms=float(prediction['elapsed_ms']),
        )
    except _Complexity as exc:
        context.warnings.append(str(exc))
        return RcEstimate(
            None,
            0.0,
            0.0,
            'unavailable',
            'partial',
            'partial',
            warnings=tuple(dict.fromkeys(context.warnings)),
            mode=surrogate.kind,
        )
    except RcCancelled:
        raise
