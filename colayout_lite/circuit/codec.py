"""Native result decoding and transport checks; no circuit matching."""
import re
from ..model import LiteError
from .models import (CircuitContext, LayoutConnectivity, LayoutInstance, NativeBinding,
                     NativeCheck, NativePathSegment, NativeParameter, ReferenceInstance, ReferenceScope, Terminal, ViewRef)

_UNPLACED_SCHEMATIC_RE = re.compile(
    r"^INFO \(LX-1001\): Schematic instance '[^'\r\n]+' has no corresponding layout instance\.$"
)


def check_native_report(succeeded, report):
    """CAS returns command success, not equivalence. Preserve all diagnostics.

    IC618's verified clean report has an opening and a closing line; work-in-progress
    layouts may also emit LX-1001 INFO lines for schematic instances not yet placed in
    layout. Every existing layout instance still requires a verified native binding or
    confirmed tied lxDummy below; any other CAS diagnostic or unknown format rejects import.
    """
    lines = [line.strip() for line in report.splitlines() if line.strip()]
    complete = (succeeded and len(lines) >= 2 and
                lines[0].startswith('*** Checking layout cellview ') and
                lines[-1] == '*** Check Against Source complete. ***')
    middle = lines[1:-1] if complete else None
    if not complete or any(not _UNPLACED_SCHEMATIC_RE.match(line) for line in middle):
        raise LiteError('NATIVE_CORRESPONDENCE_REJECTED',
            'Virtuoso 未确认器件对应关系完整；请在 Virtuoso 修复后重新导入。',
            native_report=report, command_succeeded=bool(succeeded))
    return NativeCheck(report=report)


def terminals(rows):
    result = {}
    for row in rows or []:
        if len(row) not in (4, 5):
            raise ValueError('Invalid native terminal row')
        name, net, direction, members = row[:4]
        signals = dict(row[4] or []) if len(row) == 5 else {}
        if name in result:
            raise ValueError('Duplicate native terminal')
        result[name] = Terminal(name=name, net=net, direction=direction,
                               members=dict(members or []), member_signals=signals)
    return result


def reference_instances(rows, current=True):
    logical = {}
    for row in rows or []:
        name,master,terms = row[:3]
        if name in logical:raise ValueError('Duplicate source identity')
        parameters={};capture='not_captured';props=[]
        if current:
            capture,values=row[3];props=row[4] or []
            for key,kind,units,value,default,origin,override in values or []:
                if key in parameters:raise ValueError('Duplicate parameter')
                parameters[key]=NativeParameter(value_type=kind,units=units,value=value,default=default,
                    origin=origin,instance_value=override)
        logical[name] = ReferenceInstance(name=name, master=master, terminals=terminals(terms),
            parameters=parameters,parameter_capture=capture,instance_properties=props,
            member_count=row[5] if len(row)>5 else None)
    return logical


def native_path(parts):
    return tuple(NativePathSegment(**dict(zip(
        ['library','cell','view','instance','member','row','column'], part))) for part in parts)


def view_ref(row):
    return ViewRef(**dict(zip(['library','cell','view','session','counter'], row)))


def _is_tied_native_dummy(obj, interface, child_count):
    """Accept only native leaf dummies with all terminals on one native net.

    The fixed exporter retains OA property type and lbVal representation in
    native_row. Names, ignore flags and display signal aliases are not proof.
    A signal-driven dummy still needs its native schematic correspondence.
    """
    if child_count or obj.kind not in ('inst', 'mosaic') or len(obj.native_row) != 7:
        return False
    markers = [p for p in obj.native_row[5] or []
               if isinstance(p, list) and p and p[0] == 'lxDummy']
    if markers not in ([['lxDummy', 'boolean', ['string', 'TRUE']]],
                       [['lxDummy', 'boolean', ['opaque', 't']]]):
        return False
    nets = {term.net for term in interface.values()}
    nets.update(net for term in interface.values() for net in term.members.values())
    return bool(interface) and len(nets) == 1 and None not in nets and '' not in nets


def decode_context(raw, snapshot):
    """No name/master matching, parameter rules or inferred pins."""
    try:
        hierarchical = raw[0] == 'colayout-context-native-5'
        current = hierarchical or raw[0] in ('colayout-context-native-3', 'colayout-context-native-4')
        if (not current and raw[0] != 'colayout-context-native-2') or len(raw) != (9 if hierarchical else 8 if current else 7):
            raise ValueError('Unsupported native context version')
        source, schematic_rows, source_ports, layout_rows, layout_ports, check = raw[1:7]
        pair_id = raw[7] if current else None
        if current and (not isinstance(pair_id,str) or not pair_id):
            raise ValueError('Missing paired export identity')
        report = check_native_report(check[0], check[1])
        if len(check) != 3 or not check[2]:
            raise LiteError('SOURCE_CHANGED_DURING_READ', '原生查询期间源数据变化，请重新读取。')
        source = view_ref(source)
        logical = reference_instances(schematic_rows, current)
        physical, bindings = {}, []
        for uid, name, master, child_count, terms, paths in layout_rows or []:
            if uid in physical or uid not in snapshot.objects:
                raise ValueError('Missing or duplicate physical identity')
            interface = terminals(terms)
            children = int(child_count)
            unbound_dummy = (paths is None or paths == []) and _is_tied_native_dummy(
                snapshot.objects[uid], interface, children)
            if (not paths and not unbound_dummy) or any(not path for path in paths or []):
                raise LiteError('NATIVE_BINDING_MISSING', '没有原生绑定，不进行本地补配。', layout_uid=uid, name=name)
            physical[uid] = LayoutInstance(uid=uid, name=name, master=master,
                black_box=children>0, terminals=interface)
            native_paths = tuple(native_path(path) for path in paths or [])
            for path in native_paths:
                if (path[0].library,path[0].cell,path[0].view) != (source.library,source.cell,source.view):
                    raise ValueError('Native binding belongs to another source view')
            if native_paths:
                bindings.append(NativeBinding(layout_uid=uid, schematic_paths=native_paths))
        if set(physical) != {uid for uid,obj in snapshot.objects.items() if obj.kind in ('inst','mosaic')}:
            raise ValueError('Incomplete Layout connectivity export')
        if len({inst.name for inst in physical.values()}) != len(physical):
            raise ValueError('Ambiguous Layout instance name')
        if source.session != snapshot.session:raise ValueError('Paired views belong to different sessions')
        hierarchy = tuple(ReferenceScope(path=native_path(path),source=view_ref(ref),
            instances=reference_instances(rows),ports=terminals(ports)) for path,ref,rows,ports in (raw[8] or [])) if hierarchical else ()
        circuit = CircuitContext(schema_version='colayout-circuit-4' if hierarchical else 'colayout-circuit-3' if current else 'colayout-circuit-2',
                                 pair_id=pair_id,source=source, instances=logical, ports=terminals(source_ports),
                                 bindings=tuple(bindings), native_check=report,hierarchy=hierarchy)
        layout = LayoutConnectivity(pair_id=pair_id,source=ViewRef(library=snapshot.library,cell=snapshot.cell,view='layout',
            session=snapshot.session,counter=snapshot.counter),instances=physical,ports=terminals(layout_ports))
        return circuit, layout
    except LiteError:
        raise
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise LiteError('NATIVE_CONTEXT_INVALID', str(exc)) from exc
