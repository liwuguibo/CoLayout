"""Scope-safe frozen geometry adapters for local RC estimates."""
from collections import defaultdict
from dataclasses import replace
from math import isfinite

from shapely.geometry import box

from .drc.models import Region
from .editor import projected
from .geometry import via_figures
from .hierarchy import is_hierarchical_instance
from .inspection_data import scope_pins
from .materials import Physical, extract, polygon_from_figure
from .model import LiteError, MAX_DBU
from .net_selection import index_contacts
from .parasitic_rc import (
    GeometryContribution,
    GeometrySet,
    RcBudget,
    RcEstimate,
    _to_um,
    analyze_geometry,
    compare_estimates,
    merge_geometry_sets,
    shape_geometry,
)
from .physical_connectivity import physical_graph
from .regions.query import object_bounds, owner_map
from .view_scope import view_scope


def analysis_graph(workspace, profile, scope, peek=False, exclude_uids=()):
    objects = projected(workspace)
    view_scope_data = view_scope(workspace, scope, peek)
    excluded = set(exclude_uids)
    visible = {uid for uid in objects if uid not in excluded and view_scope_data.visible(uid, workspace.baseline.text_owners)}
    native_devices = {uid for uid in visible if objects[uid].kind in ('inst', 'mosaic')}
    blackboxes = {uid for uid in native_devices if is_hierarchical_instance(workspace, objects[uid]) or
                  workspace.layout_connectivity and uid in workspace.layout_connectivity.instances and
                  workspace.layout_connectivity.instances[uid].black_box}
    leaf_devices = native_devices - blackboxes
    expandable = visible - native_devices
    view = workspace.model_copy(update={
        'baseline': workspace.baseline.model_copy(update={'objects': {uid: obj for uid, obj in workspace.baseline.objects.items() if uid in expandable}}),
        'operations': tuple(op for op in workspace.operations if op.uid in expandable),
    })
    pins, pin_warnings = scope_pins(workspace, profile, scope, peek)
    pins = [pin for pin in pins if pin.get('owner') not in excluded]
    region = Region(bbox=((-MAX_DBU, -MAX_DBU), (MAX_DBU, MAX_DBU)))
    physical, _, warnings, _, _ = extract(view, profile, region, 0, owner_filter=expandable)
    warnings += pin_warnings
    from .conduction_access import leaf_access_material
    access, access_warnings = leaf_access_material(workspace, profile, leaf_devices, pins)
    physical += access
    warnings += access_warnings
    for pin in pins:
        owner = pin.get('owner')
        if owner in blackboxes:
            for layer, geometry in pin['figures']:
                physical.append(Physical(owner or pin['id'], layer, geometry, True, (), None, None))
        elif not pin['local'] and not pin.get('existing_material'):
            for layer, geometry in pin['figures']:
                physical.append(Physical(owner or pin['id'], layer, geometry, True, (), None, None))
    graph = physical_graph(view, profile, {'schema_version': 'colayout-agent-intent-1', 'instance_map': {}, 'nets': {}}, {},
                           physical_override=(physical, warnings))
    graph['pins'] = pins
    graph['scope'] = scope
    graph['peek'] = peek
    graph['blackbox_ids'] = blackboxes
    graph['access_materials'] = access
    graph['analysis_expandable'] = expandable
    graph['analysis_leaf_devices'] = leaf_devices
    from .interface_access import scoped_figures
    for pin in pins:
        pin['contact_figures'] = scoped_figures(workspace, scope, pin)
    graph['warnings'] = [row for row in graph['report']['coverage']['warnings'] if row.get('kind') != 'empty_intent_net']
    index_contacts(graph)
    return graph


def _sets(graph, workspace, profile, target_uids=None, extra_target=GeometrySet()):
    pins = {pin['id']: pin for pin in graph['pins']}
    component_nets = defaultdict(set)
    for component, ids in graph['component_pins'].items():
        component_nets[component] = {pins[pid]['net'] for pid in ids if pins[pid]['net'] is not None}
    selected = set(target_uids) if target_uids is not None else {node['owner_uid'] for node in graph['nodes']}
    targets = []
    neighbors = []
    indices = defaultdict(int)
    owner_nets = defaultdict(set)
    for node in graph['nodes']:
        owner = node['owner_uid']
        key = f'{owner}:{node["layer"]}:{indices[owner, node["layer"]]}'
        indices[owner, node['layer']] += 1
        nets = component_nets.get(node['component_id'], set())
        owner_nets[owner].update(nets)
        row = GeometryContribution(
            key, owner, node['layer'], _to_um(node['geometry'], profile.dbu_per_micron),
            node['component_id'], next(iter(nets)) if len(nets) == 1 else None,
            figure_index=indices[owner, node['layer']] - 1,
        )
        (targets if owner in selected else neighbors).append(row)
    objects = projected(workspace)
    if profile.parasitics and profile.parasitics.surrogate:
        for uid in sorted(graph.get('analysis_expandable', ())):
            obj = objects.get(uid)
            if not obj or not obj.shape or obj.shape.kind != 'stdVia':
                continue
            vd = profile.vias.get(obj.shape.via_definition, {})
            cut_layer = vd.get('cut_layer')
            if not cut_layer:
                continue
            resolved_net = (
                next(iter(owner_nets[uid]))
                if len(owner_nets[uid]) == 1
                else (obj.shape.net_intent or None)
            )
            for idx_f, fig in enumerate(via_figures(obj.shape, profile)):
                if fig['lpp'][0] != cut_layer:
                    continue
                cut_geom = _to_um(polygon_from_figure(fig), profile.dbu_per_micron)
                row = GeometryContribution(
                    f'{uid}:{cut_layer}:cut:{idx_f}',
                    uid,
                    cut_layer,
                    cut_geom,
                    f'via:{uid}',
                    net=resolved_net,
                    figure_index=idx_f,
                )
                (targets if uid in selected else neighbors).append(row)
    direct = {uid for uid in selected if uid in objects and objects[uid].shape}
    if target_uids is None:
        simple = False
    elif extra_target.conductors:
        simple = not direct and extra_target.simple_series
    else:
        simple = len(direct) <= 1
    resistances = []
    for uid in sorted(direct):
        resistances += shape_geometry(objects[uid].shape, profile, uid).resistances
    target = GeometrySet(tuple(targets), tuple(resistances), simple)
    target = merge_geometry_sets(target, extra_target)
    if target_uids is None:
        target = GeometrySet(target.conductors, target.resistances, False)
    return target, GeometrySet(tuple(neighbors), simple_series=False)


def analyze_workspace(workspace, profile, scope, target_uids=None, *, peek=False,
                      extra_target=GeometrySet(), exclude_uids=(), budget=None, cancelled=None):
    graph = analysis_graph(workspace, profile, scope, peek, exclude_uids)
    target, neighbors = _sets(graph, workspace, profile, target_uids, extra_target)
    result = analyze_geometry(target, neighbors, profile, budget=budget, cancelled=cancelled)
    graph_warnings = tuple(str(row.get('kind', 'geometry_incomplete')) for row in graph['warnings'])
    if graph_warnings:
        result = replace(result,
            ground_status='partial' if result.ground_status == 'complete' else result.ground_status,
            coupling_status='partial' if result.coupling_status == 'complete' else result.coupling_status,
            warnings=result.warnings + graph_warnings)
    return result, graph


def _resolve_bounds_matcher(workspace, profile, scope_id, bounds_um):
    if bounds_um is None:
        return None
    if scope_id is not None and scope_id in workspace.local_data.regions:
        from .agent_tools.spatial_query import SpatialQuery

        query = SpatialQuery(workspace, profile, scope_id, bounds_um, None)
        return lambda obj: query.object_match(workspace, profile, obj) is not None
    if (
        len(bounds_um) != 4
        or not all(isfinite(v) for v in bounds_um)
        or bounds_um[0] >= bounds_um[2]
        or bounds_um[1] >= bounds_um[3]
    ):
        raise LiteError(
            'INVALID_QUERY_BOUNDS',
            '范围须为有限且有序的 [xmin,ymin,xmax,ymax]，宽高必须为正。',
            bounds_um=list(bounds_um),
        )
    scale = profile.dbu_per_micron
    roi = box(*(v * scale for v in bounds_um))

    def _matches(obj):
        bounds = object_bounds(workspace, obj, profile)
        return bounds is not None and box(*bounds[0], *bounds[1]).intersects(roi)

    return _matches


def resolve_parasitic_targets(
    workspace,
    profile,
    scope_id,
    graph,
    *,
    uids=None,
    net=None,
    target=None,
    bounds_um=None,
):
    """Resolve optional uids / net / target / bounds selectors into scoped UIDs."""
    if sum(selector is not None for selector in (uids, net, target)) > 1:
        raise LiteError('INVALID_ARGUMENTS', 'uids、net 与 target 只能选择一种目标方式。')

    objects = projected(workspace)
    owners = owner_map(workspace)
    if scope_id is not None and scope_id in workspace.local_data.regions:
        allowed = {
            uid
            for uid in objects
            if owners.get(uid) == scope_id and uid not in workspace.baseline.text_owners
        }
    else:
        allowed = {node['owner_uid'] for node in graph['nodes'] if node['owner_uid'] in objects} | {
            uid for uid in graph.get('analysis_expandable', ()) if uid in objects
        }

    bounds_matcher = _resolve_bounds_matcher(workspace, profile, scope_id, bounds_um)
    pins_by_id = {
        pin['id']: pin for pin in graph['pins'] + graph.get('directional_markers', [])
    }

    if uids is not None:
        ordered = tuple(dict.fromkeys(uids))
        if not ordered:
            raise LiteError('INVALID_ARGUMENTS', 'uids 不能为空。')
        outside = sorted(set(ordered) - allowed)
        if outside:
            raise LiteError('OBJECT_OUTSIDE_REGION', '所选对象不属于当前操作区。', uids=outside)
        selected = tuple(sorted(ordered))
        basis = 'uids'
    elif net is not None:
        comp_ids = {
            cid
            for cid, pids in graph['component_pins'].items()
            if any(pins_by_id[pid]['net'] == net for pid in pids if pid in pins_by_id)
        }
        matched = {
            node['owner_uid']
            for node in graph['nodes']
            if node['component_id'] in comp_ids and node['owner_uid'] in allowed
        }
        for uid in allowed:
            obj = objects.get(uid)
            if obj and obj.shape and obj.shape.net_intent == net:
                matched.add(uid)
        pin_has_net = any(pin.get('net') == net for pin in pins_by_id.values())
        if not matched and not pin_has_net:
            raise LiteError('NET_OUTSIDE_REGION', '网络不属于此操作区。')
        selected = tuple(sorted(matched))
        basis = 'net'
    elif target is not None:
        if scope_id is None or scope_id not in workspace.local_data.regions:
            raise LiteError('REGION_REQUIRED', 'target 选择需要在已接入的操作区内使用。')
        from .agent_tools import component_query

        comp_ids, _ = component_query.select(workspace, profile, scope_id, graph, target)
        matched = {
            node['owner_uid']
            for node in graph['nodes']
            if node['component_id'] in comp_ids and node['owner_uid'] in allowed
        }
        if target.kind == 'object' and target.id in allowed and not matched:
            obj = objects.get(target.id)
            if obj and (obj.shape or obj.mpp):
                matched.add(target.id)
        selected = tuple(sorted(matched))
        basis = f'target:{target.kind}'
    else:
        selected = None
        basis = 'bounds' if bounds_matcher is not None else 'region'

    if bounds_matcher is not None:
        pool = allowed if selected is None else selected
        selected = tuple(sorted(uid for uid in pool if uid in objects and bounds_matcher(objects[uid])))

    selected_set = allowed if selected is None else set(selected)
    resolved_nets = set()
    if net is not None:
        resolved_nets.add(net)
    else:
        for node in graph['nodes']:
            if node['owner_uid'] not in selected_set:
                continue
            for pid in graph['component_pins'].get(node['component_id'], ()):
                pin_net = pins_by_id.get(pid, {}).get('net')
                if pin_net is not None:
                    resolved_nets.add(pin_net)
        for uid in selected_set:
            obj = objects.get(uid)
            if obj and obj.shape and obj.shape.net_intent:
                resolved_nets.add(obj.shape.net_intent)

    return {
        'basis': basis,
        'target_uids': selected,
        'nets': sorted(resolved_nets),
    }


def evaluate_parasitics_outcome(
    workspace,
    profile,
    scope_id,
    *,
    uids=None,
    net=None,
    target=None,
    bounds_um=None,
    analysis_radius_um: float = 10.0,
    peek: bool = False,
    baseline_workspace=None,
    baseline_uids=None,
    budget=None,
    cancelled=None,
):
    """Shared on-demand RC evaluation for both Human UI and Agent API."""
    if profile.parasitics is None:
        raise LiteError('PARASITICS_NOT_CONFIGURED', '请先在工艺库配置并审核 RC 寄生参数。')
    budget = budget or RcBudget(analysis_radius_um=analysis_radius_um, time_seconds=2.0)
    graph = analysis_graph(workspace, profile, scope_id, peek)
    selection = resolve_parasitic_targets(
        workspace,
        profile,
        scope_id,
        graph,
        uids=uids,
        net=net,
        target=target,
        bounds_um=bounds_um,
    )
    selected_uids = selection['target_uids']
    if selected_uids == ():
        mode = profile.parasitics.surrogate.kind if profile.parasitics.surrogate else 'analytic'
        estimate = RcEstimate(
            None,
            0.0,
            0.0,
            'unavailable',
            'complete',
            'complete',
            mode=mode,
        )
    else:
        target_set, neighbors = _sets(graph, workspace, profile, selected_uids)
        estimate = analyze_geometry(target_set, neighbors, profile, budget=budget, cancelled=cancelled)
    graph_warnings = tuple(str(row.get('kind', 'geometry_incomplete')) for row in graph['warnings'])
    if graph_warnings:
        estimate = replace(
            estimate,
            ground_status='partial' if estimate.ground_status == 'complete' else estimate.ground_status,
            coupling_status='partial' if estimate.coupling_status == 'complete' else estimate.coupling_status,
            warnings=estimate.warnings + graph_warnings,
        )
    delta = None
    if baseline_workspace is not None:
        before, _ = analyze_workspace(
            baseline_workspace,
            profile,
            scope_id,
            baseline_uids,
            peek=peek,
            budget=budget,
            cancelled=cancelled,
        )
        delta = compare_estimates(before, estimate)
    return estimate, delta, baseline_workspace is not None, selection


def evaluate_parasitics(
    workspace,
    profile,
    scope_id,
    *,
    uids=None,
    net=None,
    target=None,
    bounds_um=None,
    analysis_radius_um: float = 10.0,
    peek: bool = False,
    baseline_workspace=None,
    baseline_uids=None,
    budget=None,
    cancelled=None,
) -> dict:
    """Return a JSON-serializable RC parasitic report shared across callers."""
    used_budget = budget or RcBudget(analysis_radius_um=analysis_radius_um, time_seconds=2.0)
    estimate, delta, _, selection = evaluate_parasitics_outcome(
        workspace,
        profile,
        scope_id,
        uids=uids,
        net=net,
        target=target,
        bounds_um=bounds_um,
        analysis_radius_um=analysis_radius_um,
        peek=peek,
        baseline_workspace=baseline_workspace,
        baseline_uids=baseline_uids,
        budget=used_budget,
        cancelled=cancelled,
    )
    return {
        'configured': True,
        'mode': estimate.mode,
        'rc_model_digest': profile.rc_model_digest,
        'document_id': workspace.id,
        'revision': workspace.revision,
        'region_id': scope_id,
        'analysis_radius_um': used_budget.analysis_radius_um,
        'selection': {
            'basis': selection['basis'],
            'target_uids': list(selection['target_uids'])
            if selection['target_uids'] is not None
            else None,
            'nets': selection['nets'],
        },
        'resistance_ohm': estimate.resistance_ohm,
        'ground_capacitance_fF': estimate.ground_capacitance_fF,
        'coupling_capacitance_fF': estimate.coupling_capacitance_fF,
        'total_capacitance_fF': estimate.ground_capacitance_fF + estimate.coupling_capacitance_fF,
        'resistance_status': estimate.resistance_status,
        'ground_status': estimate.ground_status,
        'coupling_status': estimate.coupling_status,
        'comparable': estimate.comparable,
        'delta': delta,
        'impacts': [
            {
                'key': impact.key,
                'relation': impact.relation,
                'layer': impact.layer,
                'capacitance_fF': impact.capacitance_fF,
                'spacing_um': impact.spacing_um,
                'target_sources': list(impact.target_sources),
                'neighbor_sources': list(impact.neighbor_sources),
                'status': impact.status,
                'target_internal': impact.target_internal,
            }
            for impact in estimate.impacts
        ],
        'warnings': list(estimate.warnings),
        'elapsed_ms': estimate.elapsed_ms,
        'signoff': False,
    }
