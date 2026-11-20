"""Local, bounded RC estimates over frozen geometry in micrometres."""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from math import isfinite
from threading import Event
from time import monotonic

from shapely import affinity
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .geometry import shape_figure, via_figures
from .materials import polygon_from_figure
from .model import Shape
from .parasitic_models import RcModel, overlap_key


@dataclass(frozen=True)
class GeometryContribution:
    key: str
    owner_uid: str
    layer: str
    geometry: object
    component_id: str
    net: str | None = None
    occurrence_path: tuple = ()
    figure_index: int = 0
    width_um: float | None = None


@dataclass(frozen=True)
class ResistanceContribution:
    key: str
    ohms: float | None


@dataclass(frozen=True)
class GeometrySet:
    conductors: tuple[GeometryContribution, ...] = ()
    resistances: tuple[ResistanceContribution, ...] = ()
    simple_series: bool = True


@dataclass(frozen=True)
class RcBudget:
    analysis_radius_um: float = 10.0
    time_seconds: float = 0.15
    max_objects: int = 2500
    max_total_vertices: int = 100000
    max_polygon_vertices: int = 12000
    max_operation_vertices: int = 30000
    max_pairs: int = 100000


@dataclass(frozen=True)
class CouplingImpact:
    key: str
    target_component: str
    neighbor_component: str
    target_sources: tuple[str, ...]
    neighbor_sources: tuple[str, ...]
    layer: str
    relation: str
    capacitance_fF: float | None
    status: str
    spacing_um: float
    geometry: object
    target_internal: bool = False


@dataclass(frozen=True)
class RcEstimate:
    resistance_ohm: float | None
    ground_capacitance_fF: float
    coupling_capacitance_fF: float
    resistance_status: str
    ground_status: str
    coupling_status: str
    impacts: tuple[CouplingImpact, ...] = ()
    warnings: tuple[str, ...] = ()
    mode: str = 'analytic'
    elapsed_ms: float | None = None

    @property
    def comparable(self):
        return self.ground_status == self.coupling_status == 'complete'


class RcCancelled(RuntimeError):
    pass


class _Complexity(RuntimeError):
    pass


@dataclass
class _Component:
    identity: str
    key: str
    layer: str
    geometry: object
    sources: tuple[str, ...]
    owners: tuple[str, ...]
    nets: tuple[str, ...]


@dataclass
class _Context:
    model: RcModel
    budget: RcBudget
    cancelled: Event | None
    deadline: float
    warnings: list[str] = field(default_factory=list)

    def checkpoint(self):
        if self.cancelled is not None and self.cancelled.is_set():
            raise RcCancelled('RC estimate cancelled')
        if monotonic() > self.deadline:
            raise RcCancelled('RC estimate exceeded its time budget')


def _vertices(geometry):
    polygons = [geometry] if geometry.geom_type == 'Polygon' else getattr(geometry, 'geoms', ())
    return sum(len(ring.coords) for polygon in polygons if polygon.geom_type == 'Polygon'
               for ring in (polygon.exterior, *polygon.interiors))


def _checked_geometry(geometry, context, *, operation=False):
    count = _vertices(geometry)
    limit = context.budget.max_operation_vertices if operation else context.budget.max_polygon_vertices
    if count > limit:
        raise _Complexity(f'Geometry has {count} vertices, above the real-time limit {limit}')
    return geometry


def _union(geometries, context):
    rows = list(geometries)
    if not rows:
        return Polygon()
    for row in rows:
        _checked_geometry(row, context)
    if sum(_vertices(row) for row in rows) > context.budget.max_operation_vertices:
        raise _Complexity('Union input exceeds the real-time vertex budget')
    context.checkpoint()
    result = unary_union(rows)
    context.checkpoint()
    return _checked_geometry(result, context, operation=True)


def _to_um(geometry, scale):
    return affinity.scale(geometry, xfact=1 / scale, yfact=1 / scale, origin=(0, 0))


def shape_geometry(shape: Shape, profile, uid: str, *, component_id=None, net=None,
                   occurrence_path=(), simple_series=None) -> GeometrySet:
    scale = profile.dbu_per_micron
    component_id = component_id or uid
    conductors = []
    resistances = []
    series = shape.kind in ('path', 'pathSeg', 'stdVia') if simple_series is None else simple_series
    if shape.kind == 'stdVia':
        via = profile.parasitics.vias.get(shape.via_definition) if profile.parasitics else None
        params = profile.effective_via(shape)
        cuts = params['cutRows'] * params['cutColumns']
        r_cut = via.resistance_ohm_per_cut if via else None
        if r_cut is None and profile.parasitics and profile.parasitics.surrogate:
            surr = profile.parasitics.surrogate
            stack = surr.build_stackup()
            vd = profile.vias.get(shape.via_definition, {})
            vname = (
                surr.via_map.get(shape.via_definition)
                or surr.via_map.get(vd.get('cut_layer', ''))
                or vd.get('cut_layer', '')
            ).upper()
            r_cut = stack.r_via_ohm.get(vname)
        resistances.append(ResistanceContribution(uid, r_cut / cuts if r_cut is not None else None))
        figures = via_figures(shape, profile)
    else:
        figures = [shape_figure(shape)]
    for index, figure in enumerate(figures):
        layer = figure['lpp'][0]
        geometry = _to_um(polygon_from_figure(figure), scale)
        width = None
        if shape.kind in ('path', 'pathSeg'):
            width = shape.width / scale
            if shape.width_basis == 'oa_diagonal':
                from math import sqrt
                width /= sqrt(2)
        conductors.append(GeometryContribution(
            f'{uid}:{layer}:{index}', uid, layer, geometry, component_id, net,
            tuple(occurrence_path), index, width,
        ))
    if shape.kind in ('path', 'pathSeg'):
        length = LineString(shape.points).length / scale
        extension = (shape.begin_extension + shape.end_extension) / scale
        width = conductors[0].width_um
        if shape.kind == 'pathSeg' and shape.width_basis == 'oa_diagonal':
            from math import sqrt
            extension /= sqrt(2)
        if shape.kind == 'path' and shape.style in ('extendExtend', 'roundRound'):
            extension += width
        length += extension
        metal = profile.parasitics.metals.get(shape.lpp[0]) if profile.parasitics else None
        r_sh = metal.sheet_resistance_ohm_per_sq if metal else None
        if r_sh is None and profile.parasitics and profile.parasitics.surrogate:
            surr = profile.parasitics.surrogate
            stack = surr.build_stackup()
            mname = surr.layer_map.get(shape.lpp[0], shape.lpp[0]).upper()
            r_sh = stack.rsh_ohm_sq.get(mname)
        resistances.append(ResistanceContribution(
            uid, r_sh * length / width if r_sh is not None else None,
        ))
    return GeometrySet(tuple(conductors), tuple(resistances), bool(series))


def merge_geometry_sets(*rows: GeometrySet) -> GeometrySet:
    return GeometrySet(
        tuple(item for row in rows for item in row.conductors),
        tuple(item for row in rows for item in row.resistances),
        all(row.simple_series for row in rows),
    )


def _components(rows, context):
    grouped = {}
    for row in rows:
        grouped.setdefault((row.component_id, row.layer), []).append(row)
    result = []
    for (component, layer), items in grouped.items():
        geometry = _union((row.geometry for row in items), context)
        nets = tuple(sorted({row.net for row in items if row.net is not None}))
        result.append(_Component(
            component, f'{component}:{layer}', layer, geometry,
            tuple(sorted(row.key for row in items)),
            tuple(sorted({row.owner_uid for row in items})), nets,
        ))
    return result


def _edges(geometry, context):
    _checked_geometry(geometry, context)
    polygons = [geometry] if geometry.geom_type == 'Polygon' else getattr(geometry, 'geoms', ())
    result = []
    for polygon in polygons:
        if polygon.geom_type != 'Polygon':
            continue
        for ring in (polygon.exterior, *polygon.interiors):
            points = list(ring.coords)
            result.extend((a, b) for a, b in zip(points, points[1:]))
    return result


def _facing_length(first, second, gap, context):
    total = 0.0
    tolerance = max(1e-9, gap * 1e-6)
    second_edges = _edges(second, context)
    for a, b in _edges(first, context):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = (dx * dx + dy * dy) ** 0.5
        if length <= tolerance:
            continue
        ux, uy = dx / length, dy / length
        for c, d in second_edges:
            context.checkpoint()
            ex, ey = d[0] - c[0], d[1] - c[1]
            other = (ex * ex + ey * ey) ** 0.5
            if other <= tolerance or abs(dx * ey - dy * ex) > tolerance * length * other:
                continue
            separation = abs((c[0] - a[0]) * uy - (c[1] - a[1]) * ux)
            if abs(separation - gap) > tolerance:
                continue
            ac = (c[0] - a[0]) * ux + (c[1] - a[1]) * uy
            ad = (d[0] - a[0]) * ux + (d[1] - a[1]) * uy
            lo = max(0.0, min(ac, ad))
            hi = min(length, max(ac, ad))
            if hi - lo > tolerance:
                total += hi - lo
    return total


def _relation(first, second):
    if len(first.nets) == len(second.nets) == 1:
        return 'same_net' if first.nets == second.nets else 'different_net'
    return 'unknown_net'


def _coupling(first, second, context, internal):
    model = context.model
    spacing = first.geometry.distance(second.geometry)
    relation = _relation(first, second)
    key = '|'.join(sorted((first.key, second.key)))
    base = dict(key=key, target_component=first.key, neighbor_component=second.key,
                target_sources=first.sources, neighbor_sources=second.sources,
                layer=first.layer, relation=relation, spacing_um=spacing,
                geometry=second.geometry, target_internal=internal)
    if first.layer == second.layer:
        if spacing <= 1e-12 or spacing > context.budget.analysis_radius_um:
            return None
        rule = model.lateral.get(first.layer)
        if rule is None:
            context.warnings.append(f'{first.layer}: missing lateral coupling model')
            return CouplingImpact(**base, capacitance_fF=None, status='missing_model')
        facing = _facing_length(first.geometry, second.geometry, spacing, context)
        if facing <= 1e-12:
            return None
        if not rule.min_spacing_um <= spacing <= rule.max_spacing_um:
            return CouplingImpact(**base, capacitance_fF=None, status='out_of_model_range')
        return CouplingImpact(**base, capacitance_fF=rule.coefficient_fF_per_um * facing / spacing,
                              status='complete')
    if first.geometry.bounds[2] < second.geometry.bounds[0] or second.geometry.bounds[2] < first.geometry.bounds[0] \
            or first.geometry.bounds[3] < second.geometry.bounds[1] or second.geometry.bounds[3] < first.geometry.bounds[1]:
        return None
    context.checkpoint()
    area = first.geometry.intersection(second.geometry).area
    context.checkpoint()
    if area <= 1e-18:
        return None
    pair = overlap_key(first.layer, second.layer)
    rule = model.overlap.get(pair)
    if rule is None:
        context.warnings.append(f'{pair}: missing overlap coupling model')
        return CouplingImpact(**base, capacitance_fF=None, status='missing_model')
    return CouplingImpact(**base, capacitance_fF=rule.coefficient_fF_per_um2 * area, status='complete')


def analyze_geometry(target: GeometrySet, neighbors: GeometrySet, profile, *, budget=None,
                     cancelled=None) -> RcEstimate:
    model = profile.parasitics
    if model is None:
        return RcEstimate(None, 0.0, 0.0, 'unconfigured', 'unconfigured', 'unconfigured')
    budget = budget or RcBudget()
    context = _Context(model, budget, cancelled, monotonic() + budget.time_seconds)
    if model.surrogate is not None:
        from .parasitic_surrogate import analyze_geometry_surrogate
        return analyze_geometry_surrogate(target, neighbors, profile, model, context)
    resistances = [row.ohms for row in target.resistances]
    out_of_width_range = set()
    for row in target.conductors:
        metal = model.metals.get(row.layer)
        if row.width_um is not None and metal and not metal.min_width_um <= row.width_um <= metal.max_width_um:
            out_of_width_range.add(row.layer)
            context.warnings.append(f'{row.layer}: width {row.width_um:g} µm is outside the model range')
    resistance_complete = target.simple_series and not out_of_width_range and all(value is not None for value in resistances)
    resistance = sum(resistances) if resistance_complete else None
    resistance_status = ('out_of_model_range' if out_of_width_range else
                         'complete' if resistance_complete else 'partial' if resistances else 'unavailable')
    if model.capacitance_status == 'unconfigured':
        return RcEstimate(resistance, 0.0, 0.0, resistance_status, 'unconfigured', 'unconfigured',
                          warnings=tuple(model.coverage_notes))
    ground = 0.0
    ground_status = 'out_of_model_range' if out_of_width_range else 'complete'
    coupling = 0.0
    coupling_complete = True
    impacts = []
    try:
        context.checkpoint()
        if len(target.conductors) + len(neighbors.conductors) > budget.max_objects:
            raise _Complexity('Conductor count exceeds the real-time budget')
        if sum(_vertices(row.geometry) for row in (*target.conductors, *neighbors.conductors)) > budget.max_total_vertices:
            raise _Complexity('Total vertex count exceeds the real-time budget')
        targets = _components(target.conductors, context)
        for component in targets:
            metal = model.metals.get(component.layer)
            if metal is None:
                if ground_status == 'complete':ground_status = 'partial'
                context.warnings.append(f'{component.layer}: missing reference-ground model')
                continue
            ground += metal.area_cap_fF_per_um2 * component.geometry.area
            ground += metal.perimeter_cap_fF_per_um * component.geometry.length
        neighbors_components = _components(neighbors.conductors, context)
        pairs = []
        if len(targets) * max(1, len(neighbors_components)) + len(targets) * len(targets) > budget.max_pairs:
            raise _Complexity('Coupling pair count exceeds the real-time budget')
        for first in targets:
            for second in targets:
                if first.key < second.key and first.identity != second.identity:
                    pairs.append((first, second, True))
            for second in neighbors_components:
                if first.identity != second.identity:
                    pairs.append((first, second, False))
        for first, second, internal in pairs:
            context.checkpoint()
            impact = _coupling(first, second, context, internal)
            if impact is None:
                continue
            impacts.append(impact)
            if impact.status == 'complete':
                coupling += impact.capacitance_fF
            else:
                coupling_complete = False
    except _Complexity as exc:
        if ground_status == 'complete':ground_status = 'partial'
        coupling_complete = False
        context.warnings.append(str(exc))
    except RcCancelled:
        raise
    return RcEstimate(resistance, ground, coupling, resistance_status, ground_status,
                      'complete' if coupling_complete else 'partial', tuple(impacts),
                      tuple(dict.fromkeys(context.warnings)))


def _source_groups(impacts):
    parent = {}
    def find(value):
        parent.setdefault(value, value)
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value
    def join(first, second):
        a, b = find(first), find(second)
        if a != b:
            parent[b] = a
    for impact in impacts:
        sources = (*impact.target_sources, *impact.neighbor_sources)
        for source in sources[1:]:
            join(sources[0], source)
    totals = {}
    for impact in impacts:
        root = find(impact.target_sources[0])
        totals[root] = totals.get(root, 0.0) + impact.capacitance_fF
    return {root: (set(source for source in parent if find(source) == root), value)
            for root, value in totals.items()}


def _impact_deltas(before, after):
    groups = []
    for rows, field in ((_source_groups(before.impacts), 'before_fF'),
                        (_source_groups(after.impacts), 'after_fF')):
        for sources, value in rows.values():
            groups.append({'sources': sources, field: value})
    merged = True
    while merged:
        merged = False
        for index, group in enumerate(groups):
            for other in groups[index + 1:]:
                if group['sources'] & other['sources']:
                    group['sources'] |= other['sources']
                    for field in ('before_fF', 'after_fF'):
                        if field in other:
                            group[field] = group.get(field, 0.0) + other[field]
                    groups.remove(other)
                    merged = True
                    break
            if merged:
                break
    return tuple({'sources': tuple(sorted(group['sources'])),
                  'before_fF': group.get('before_fF', 0.0), 'after_fF': group.get('after_fF', 0.0),
                  'delta_fF': group.get('after_fF', 0.0) - group.get('before_fF', 0.0)}
                 for group in groups)


def compare_estimates(before: RcEstimate, after: RcEstimate):
    if not before.comparable or not after.comparable:
        return None
    def delta(left, right):
        value = right - left
        return 0.0 if isfinite(value) and abs(value) < 1e-15 else value
    return {'delta_r_ohm': delta(before.resistance_ohm or 0.0, after.resistance_ohm or 0.0)
            if before.resistance_status == after.resistance_status == 'complete' else None,
            'delta_cg_fF': delta(before.ground_capacitance_fF, after.ground_capacitance_fF),
            'delta_cc_fF': delta(before.coupling_capacitance_fF, after.coupling_capacitance_fF),
            'impact_deltas': _impact_deltas(before, after)}
