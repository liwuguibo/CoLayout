"""Scope-safe frozen geometry adapters for local RC estimates."""
from collections import defaultdict
from dataclasses import replace

from .drc.models import Region
from .editor import projected
from .geometry import via_figures
from .hierarchy import is_hierarchical_instance
from .inspection_data import scope_pins
from .materials import Physical, extract, polygon_from_figure
from .model import MAX_DBU
from .net_selection import index_contacts
from .parasitic_rc import GeometryContribution, GeometrySet, _to_um, analyze_geometry, merge_geometry_sets, shape_geometry
from .physical_connectivity import physical_graph
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
