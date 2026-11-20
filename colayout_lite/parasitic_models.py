"""Reviewed, bounded RC coefficients for local interactive estimates."""
from typing import Literal

from pydantic import Field, model_validator

from .model import Record, digest


def overlap_key(first: str, second: str) -> str:
    if first == second:
        raise ValueError('Overlap coupling needs two different layers')
    return '|'.join(sorted((first, second)))


class RcMetal(Record):
    sheet_resistance_ohm_per_sq: float = Field(gt=0)
    area_cap_fF_per_um2: float = Field(ge=0)
    perimeter_cap_fF_per_um: float = Field(ge=0)
    min_width_um: float = Field(gt=0)
    max_width_um: float = Field(gt=0)

    @model_validator(mode='after')
    def valid(self):
        if self.max_width_um < self.min_width_um:
            raise ValueError('RC maximum width is below minimum width')
        return self


class RcVia(Record):
    resistance_ohm_per_cut: float = Field(gt=0)


class RcLateral(Record):
    coefficient_fF_per_um: float = Field(gt=0)
    min_spacing_um: float = Field(gt=0)
    max_spacing_um: float = Field(gt=0)

    @model_validator(mode='after')
    def valid(self):
        if self.max_spacing_um < self.min_spacing_um:
            raise ValueError('RC maximum spacing is below minimum spacing')
        return self


class RcOverlap(Record):
    coefficient_fF_per_um2: float = Field(gt=0)


class RcSurrogateStackupLayer(Record):
    z_min_um: float = Field(ge=0)
    z_max_um: float = Field(gt=0)
    rsh_ohm_per_sq: float = Field(gt=0)

    @model_validator(mode='after')
    def valid(self):
        if self.z_max_um <= self.z_min_um:
            raise ValueError('Surrogate layer z_max_um must exceed z_min_um')
        return self


class RcSurrogateViaSpec(Record):
    bot_layer: str = Field(min_length=1, max_length=64)
    top_layer: str = Field(min_length=1, max_length=64)
    z_min_um: float = Field(ge=0)
    z_max_um: float = Field(gt=0)
    cut_width_um: float = Field(gt=0)
    min_space_um: float = Field(gt=0)
    enc_bot_um: float = Field(ge=0, default=0.01)
    enc_top_um: float = Field(ge=0, default=0.01)
    r_single_ohm: float = Field(gt=0)
    tc1: float = 1.000e-3

    @model_validator(mode='after')
    def valid(self):
        if self.z_max_um <= self.z_min_um:
            raise ValueError('Surrogate via z_max_um must exceed z_min_um')
        return self


class RcSurrogateDielectricZone(Record):
    z_min_um: float = Field(ge=0)
    z_max_um: float = Field(gt=0)
    eps_r: float = Field(gt=0)

    @model_validator(mode='after')
    def valid(self):
        if self.z_max_um <= self.z_min_um:
            raise ValueError('Dielectric zone z_max_um must exceed z_min_um')
        return self


class RcSurrogateModel(Record):
    kind: Literal['geograph_rc_v3', 'geograph_rc_v2', 'geograph_rc_v1'] = 'geograph_rc_v3'
    preset: Literal['synthetic_demo', 'custom'] = 'custom'
    weights_path: str | None = Field(
        default=None, max_length=4000, exclude_if=lambda value: value is None
    )
    weights_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$', exclude_if=lambda value: value is None
    )
    r_halo_um: float = Field(default=12.0, gt=0, le=100.0)
    layer_window: int = Field(default=2, ge=0, le=10)
    grid_snap_um: float = Field(default=0.001, gt=0, le=1.0)
    ground_nets: tuple[str, ...] = ('VSS', 'GND')
    layer_order: tuple[str, ...] = ()
    layer_map: dict[str, str] = Field(default_factory=dict)
    via_map: dict[str, str] = Field(default_factory=dict)
    stackup_layers: dict[str, RcSurrogateStackupLayer] = Field(default_factory=dict)
    stackup_vias: dict[str, RcSurrogateViaSpec] = Field(default_factory=dict)
    dielectric_zones: tuple[RcSurrogateDielectricZone, ...] = ()

    @model_validator(mode='after')
    def valid(self):
        if self.preset == 'custom' and not self.stackup_layers:
            raise ValueError('Custom RC surrogate requires stackup_layers')
        from .parasitic_nn.geometry_graph import SYNTHETIC_LAYER_ORDER

        base_layers = (
            set(SYNTHETIC_LAYER_ORDER) if self.preset == 'synthetic_demo' else set()
        ) | {name.upper() for name in self.stackup_layers}
        for name in self.layer_order:
            if name.upper() not in base_layers:
                raise ValueError(f'Unknown surrogate layer in layer_order: {name}')
        for vname, via in self.stackup_vias.items():
            if via.bot_layer.upper() not in base_layers or via.top_layer.upper() not in base_layers:
                raise ValueError(f'Surrogate via {vname} references unknown stackup layer')
        return self

    def build_stackup(self):
        from .parasitic_nn.geometry_graph import (
            StackupConfig,
            SYNTHETIC_DIELECTRIC_ZONES,
            SYNTHETIC_LAYER_ORDER,
            SYNTHETIC_LAYER_STACK_SPEC,
            SYNTHETIC_RSH_OHM_SQ,
            SYNTHETIC_R_VIA_OHM,
            synthetic_stackup,
        )
        from .parasitic_nn.via_connectivity import SYNTHETIC_VIA_STACK_SPEC, ViaSpec

        if (
            self.preset == 'synthetic_demo'
            and not self.stackup_layers
            and not self.stackup_vias
            and not self.dielectric_zones
            and not self.layer_order
        ):
            return synthetic_stackup(
                grid_snap_um=self.grid_snap_um,
                ground_nets=self.ground_nets,
            )

        layer_specs = dict(SYNTHETIC_LAYER_STACK_SPEC) if self.preset == 'synthetic_demo' else {}
        rsh_map = dict(SYNTHETIC_RSH_OHM_SQ) if self.preset == 'synthetic_demo' else {}
        for name, row in self.stackup_layers.items():
            key = name.upper()
            layer_specs[key] = (
                row.z_min_um,
                row.z_max_um,
                row.z_max_um - row.z_min_um,
                row.rsh_ohm_per_sq,
            )
            rsh_map[key] = row.rsh_ohm_per_sq

        if self.layer_order:
            order = tuple(name.upper() for name in self.layer_order)
        elif self.preset == 'synthetic_demo' and not self.stackup_layers:
            order = SYNTHETIC_LAYER_ORDER
        else:
            order = tuple(
                k for k, _ in sorted(layer_specs.items(), key=lambda item: item[1][0])
            )
        layer_idx = {name: idx for idx, name in enumerate(order)}

        via_specs = dict(SYNTHETIC_VIA_STACK_SPEC) if self.preset == 'synthetic_demo' else {}
        r_via_map = dict(SYNTHETIC_R_VIA_OHM) if self.preset == 'synthetic_demo' else {}
        for vname, via in self.stackup_vias.items():
            key = vname.upper()
            bot_key = via.bot_layer.upper()
            top_key = via.top_layer.upper()
            bot_idx = layer_idx[bot_key]
            top_idx = layer_idx[top_key]
            via_specs[key] = ViaSpec(
                name=key,
                bot_layer=bot_key,
                top_layer=top_key,
                bot_idx=bot_idx,
                top_idx=top_idx,
                layer_pos=0.5 * (bot_idx + top_idx),
                gds_layer=0,
                gds_datatype=0,
                z_min=via.z_min_um,
                z_max=via.z_max_um,
                cut_width=via.cut_width_um,
                min_space=via.min_space_um,
                enc_bot=via.enc_bot_um,
                enc_top=via.enc_top_um,
                r_single_ohm=via.r_single_ohm,
                tc1=via.tc1,
            )
            r_via_map[key] = via.r_single_ohm

        if self.dielectric_zones:
            zones = tuple(
                (z.z_min_um, z.z_max_um, z.eps_r) for z in self.dielectric_zones
            )
        elif self.preset == 'synthetic_demo':
            zones = SYNTHETIC_DIELECTRIC_ZONES
        else:
            zones = ((0.0, max(v[1] for v in layer_specs.values()) + 2.0, 3.5),)

        m1_name = 'M1' if 'M1' in layer_specs else order[min(1, len(order) - 1)]
        m1_z = layer_specs[m1_name][1] if m1_name in layer_specs else 0.450
        return StackupConfig(
            layer_order=order,
            layer_stack_spec=layer_specs,
            dielectric_zones=zones,
            via_stack_spec=via_specs,
            rsh_ohm_sq=rsh_map,
            r_via_ohm=r_via_map,
            ground_nets=self.ground_nets,
            reference_m1_layer=m1_name,
            reference_z_um=m1_z,
            grid_snap_um=self.grid_snap_um,
        )


class RcModel(Record):
    schema_version: Literal['colayout-lite-rc-1'] = 'colayout-lite-rc-1'
    source: str = Field(min_length=1, max_length=4000)
    corner: str = Field(min_length=1, max_length=128)
    temperature_c: float
    reference_environment: str = Field(min_length=1, max_length=2000)
    ground_capacitance_scope: Literal['reference_ground_only'] = 'reference_ground_only'
    metals: dict[str, RcMetal] = Field(default_factory=dict)
    vias: dict[str, RcVia] = Field(default_factory=dict)
    lateral: dict[str, RcLateral] = Field(default_factory=dict)
    overlap: dict[str, RcOverlap] = Field(default_factory=dict)
    capacitance_status: Literal['configured', 'partial', 'unconfigured'] = 'configured'
    coverage_notes: tuple[str, ...] = ()
    surrogate: RcSurrogateModel | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    validation: dict = Field(default_factory=dict, exclude_if=lambda value: not value)

    def fingerprint(self):
        return digest(self.model_dump(mode='json', exclude={'validation'}))


class RcExtractionEvidence(Record):
    schema_version: Literal['colayout-lite-rc-evidence-1'] = 'colayout-lite-rc-evidence-1'
    rc_model_digest: str
    input_digest: str
    extraction_settings: dict
    status: Literal['documented', 'xrc_checked']
    summary: str = Field(default='', max_length=4000)

    def current(self, model: RcModel):
        return self.rc_model_digest == model.fingerprint()
