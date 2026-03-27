"""
roof_structural.py
──────────────────
Roof Structural Design Module — Engineer of Record (EOR) Perspective
Western Canada (BC / AB), per NBCC 2020 / BCBC 2024 / ABC 2019

PURPOSE
  Determine: load calculations, truss layout, girder placement, valley drift
  zones, deflection requirements, and the complete vertical load path from
  every bearing point to the foundation.

NOT responsible for:
  - Truss member sizing          (truss manufacturer's scope)
  - Interior post / wall sizing  (frame analyzer determines this)
  - Shear wall / lateral design  (frame analyzer's scope)
  - Connection hardware selection (post-frame / post-roof step)

KEY OUTPUTS consumed by downstream modules
  for_foundation:      column loads + wall line loads
  for_frame_analyzer:  DL/SL-split node and distributed wall loads (PyNite-ready)
  for_drawing:         3-D geometry + annotations + notes table
  Exported via to_json() and to_csv_tables()

IMPORTANT NOTE ON INTERIOR BEARING
  Exterior walls are always assumed bearing.
  Interior bearing walls must be confirmed by the frame analyzer — the roof
  module flags "support required here at (x, y)" and the frame module decides
  whether that becomes a post-in-wall, a new wall, or a transfer beam.
"""

from __future__ import annotations

import csv
import io
import json
import math
from dataclasses import dataclass, field
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# NBCC 2020 Reference Data
# ──────────────────────────────────────────────────────────────────────────────

NBCC_CLIMATE: dict[str, dict] = {
    # BC
    "Vancouver":      {"Ss": 1.8, "Sr": 0.3},
    "Surrey":         {"Ss": 1.8, "Sr": 0.3},
    "Burnaby":        {"Ss": 1.8, "Sr": 0.3},
    "Richmond":       {"Ss": 1.8, "Sr": 0.3},
    "Victoria":       {"Ss": 1.1, "Sr": 0.3},
    "Kelowna":        {"Ss": 1.3, "Sr": 0.2},
    "Kamloops":       {"Ss": 1.2, "Sr": 0.2},
    "Prince George":  {"Ss": 2.4, "Sr": 0.2},
    "Revelstoke":     {"Ss": 6.0, "Sr": 0.5},
    "Whistler":       {"Ss": 5.5, "Sr": 0.4},
    "Cranbrook":      {"Ss": 1.5, "Sr": 0.2},
    "Abbotsford":     {"Ss": 1.8, "Sr": 0.3},
    "Nanaimo":        {"Ss": 1.2, "Sr": 0.3},
    # AB
    "Calgary":        {"Ss": 1.1, "Sr": 0.1},
    "Edmonton":       {"Ss": 1.6, "Sr": 0.1},
    "Red Deer":       {"Ss": 1.4, "Sr": 0.1},
    "Fort McMurray":  {"Ss": 2.3, "Sr": 0.1},
    "Lethbridge":     {"Ss": 0.7, "Sr": 0.1},
    "Medicine Hat":   {"Ss": 0.9, "Sr": 0.1},
    "Grande Prairie": {"Ss": 1.8, "Sr": 0.1},
}

SNOW_IMPORTANCE: dict[str, float] = {
    "Low":           0.8,
    "Normal":        1.0,
    "High":          1.15,
    "Post-Disaster": 1.25,
}

DEAD_LOAD_COMPONENTS: dict[str, dict] = {
    "asphalt_shingle": {"roofing": 3.5,  "sheathing": 2.3, "framing": 4.0,
                        "ceiling_drywall": 2.5, "insulation": 1.5},
    "metal":           {"roofing": 2.0,  "sheathing": 2.3, "framing": 4.0,
                        "ceiling_drywall": 2.5, "insulation": 1.5},
    "clay_tile":       {"roofing": 10.0, "sheathing": 2.3, "framing": 4.0,
                        "ceiling_drywall": 2.5, "insulation": 1.5},
    "concrete_tile":   {"roofing": 12.0, "sheathing": 2.3, "framing": 4.0,
                        "ceiling_drywall": 2.5, "insulation": 1.5},
}

KPA_TO_PSF      = 20.885   # 1 kPa = 20.885 psf
FT_TO_M         = 0.3048
RHO_SNOW_KN_M3  = 3.0      # Compacted snow density (NBCC) in kN/m³

# Deflection limits — NBCC Table 4.1.5.3
DEF_LIVE  = 180    # L/180  snow (live) load only
DEF_TOTAL = 240    # L/240  total load (standard roof)
DEF_BRITTLE = 360  # L/360  plaster / tile ceiling

# SPF #2 modulus of elasticity (conservative baseline for rule-of-thumb flag)
E_SPF_PSI = 1_600_000


# ──────────────────────────────────────────────────────────────────────────────
# Input Data Models
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Pt2:
    x: float
    y: float

    def dist_to(self, other: "Pt2") -> float:
        return math.hypot(other.x - self.x, other.y - self.y)


@dataclass
class Pt3:
    x: float
    y: float
    z: float

    def to_dict(self) -> dict:
        return {"x_ft": self.x, "y_ft": self.y, "z_ft": self.z}


@dataclass
class RoofPlane:
    id: str
    pitch_str: str           # "6/12"
    perimeter: list[Pt2]     # plan corners at eave level (ft)
    eave_height_ft: float
    overhang_ft: float = 2.0

    @property
    def pitch_decimal(self) -> float:
        r, ru = (float(v) for v in self.pitch_str.split("/"))
        return r / ru

    @property
    def pitch_degrees(self) -> float:
        return math.degrees(math.atan(self.pitch_decimal))

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        xs = [p.x for p in self.perimeter]
        ys = [p.y for p in self.perimeter]
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def width_ft(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def depth_ft(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def short_span_ft(self) -> float:
        return min(self.width_ft, self.depth_ft)

    @property
    def long_span_ft(self) -> float:
        return max(self.width_ft, self.depth_ft)

    @property
    def area_sqft(self) -> float:
        return self.width_ft * self.depth_ft

    @property
    def ridge_height_ft(self) -> float:
        return self.eave_height_ft + (self.short_span_ft / 2.0) * self.pitch_decimal


@dataclass
class BearingWall:
    """
    A bearing wall in plan.

    Exterior walls are always bearing — pass them all in.
    Interior walls: include only those confirmed on architectural drawings.
    continuous_to_foundation=True means the wall runs all the way down without
    any transfer beams; False triggers a warning requiring a transfer beam.
    """
    id: str
    start: Pt2
    end: Pt2
    is_exterior: bool
    continuous_to_foundation: bool
    floor: str = "all"

    @property
    def length_ft(self) -> float:
        return self.start.dist_to(self.end)

    @property
    def direction(self) -> str:
        """'x' = wall runs E-W (roughly constant y); 'y' = wall runs N-S."""
        return "x" if abs(self.end.y - self.start.y) < abs(self.end.x - self.start.x) else "y"


@dataclass
class RoofOpening:
    id: str
    kind: str         # "skylight" | "chimney" | "hatch" | "RTU_curb"
    center: Pt2
    width_ft: float
    depth_ft: float


@dataclass
class PointLoadItem:
    """Heavy item on roof — RTU, solar rack, water tank."""
    id: str
    kind: str
    weight_lbs: float
    center: Pt2
    footprint_ft: tuple[float, float]


@dataclass
class ArchitecturalInput:
    city: str
    province: str
    importance_category: str    # "Normal" | "High" | "Post-Disaster" | "Low"
    roof_planes: list[RoofPlane]
    bearing_walls: list[BearingWall]
    num_stories: int = 2
    roofing_material: str = "asphalt_shingle"
    wind_exposure: str = "sheltered"   # "sheltered" | "exposed"
    has_cathedral_ceiling: bool = False
    roof_type: str = "gable"    # "gable" | "shed" | "flat" — "hip" raises NotImplementedError
    roof_openings: list[RoofOpening] = field(default_factory=list)
    point_load_items: list[PointLoadItem] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Output Data Models
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class DesignLoads:
    dead_psf: float
    ground_snow_kPa: float
    rain_kPa: float
    design_snow_psf: float
    service_combo_psf: float       # D + S
    strength_combo_psf: float      # 1.25D + 1.5S
    truss_spacing_in: int
    slope_factor_Cs: float
    exposure_factor_Cw: float
    Is: float
    drift_note: str = ""


@dataclass
class TrussInfo:
    id: str
    kind: str         # "common" | "gable_end"
    position_ft: float
    span_ft: float
    spacing_in: int
    bearing_wall_ids: list[str]
    reaction_dead_lbs: float
    reaction_snow_lbs: float
    reaction_factored_lbs: float   # 1.25D + 1.5S
    plane_id: str
    in_drift_zone: bool = False


@dataclass
class BearingPoint:
    """Critical concentrated bearing — girder end, ridge beam end, heavy RTU."""
    id: str
    source_id: str
    source_kind: str     # "girder" | "point_load_item"
    location: Pt3
    load_direction: Optional[str]   # "x" | "y" — spans direction of source girder
    load_kind: str       # "point"
    dead_lbs: float
    snow_lbs: float
    unfactored_lbs: float
    factored_lbs: float
    post_requirement: str
    critical: bool = True


@dataclass
class GirderInfo:
    id: str
    kind: str          # "valley_girder" | "master_girder"
    span_ft: float
    start_pt: Pt3
    end_pt: Pt3
    span_direction: str
    supported_truss_ids: list[str]
    total_dead_load_lbs: float
    total_snow_load_lbs: float
    total_factored_load_lbs: float
    bearing_points: list[BearingPoint]
    note: str = ""


@dataclass
class DriftLoad:
    """
    Valley snow accumulation estimate per NBCC 4.1.6.5.
    Uses empirical formula: hd = 0.8 × sqrt(lu_m), capped at 1.5× base snow depth.
    Drift is triangular: peak at valley line, zero at drift_width.
    EOR must confirm with full NBCC 4.1.6.5 calculation before stamping.
    """
    id: str
    kind: str           # "valley"
    valley_x: float
    y_start: float
    y_end: float
    drift_width_ft: float        # each side of valley
    peak_additional_psf: float   # at valley line
    avg_additional_psf: float    # = 0.5 × peak (triangular average)
    affected_truss_ids: list[str]
    source_planes: list[str]
    note: str


@dataclass
class DeflectionCheck:
    """
    L/180 (live) and L/240 (total) deflection limits — NBCC Table 4.1.5.3.
    Outputs minimum required EI so truss manufacturer can verify compliance.

    flag:
      OK_typical         span in normal range; standard truss expected adequate
      VERIFY_DEFLECTION  long span; manufacturer must provide EI confirmation
      CRITICAL_DEFLECTION  deflection very likely governs over strength
    """
    element_id: str
    element_type: str    # "truss" | "girder"
    span_ft: float
    service_load_plf: float
    limit_live_in: float
    limit_total_in: float
    req_EI_live_lb_in2: float
    req_EI_total_lb_in2: float
    governing_EI_lb_in2: float
    min_depth_rec_in: float
    flag: str


@dataclass
class LoadPathStage:
    level: str
    element_kind: str
    element_id: Optional[str]
    status: str          # "OK" | "DISCONTINUITY"


@dataclass
class LoadPath:
    bearing_point_id: str
    stages: list[LoadPathStage]
    complete: bool
    warning: str = ""


@dataclass
class RoofDesignOutput:
    loads: DesignLoads
    trusses: list[TrussInfo]
    girders: list[GirderInfo]
    bearing_points: list[BearingPoint]
    drift_loads: list[DriftLoad]
    deflection_checks: list[DeflectionCheck]
    load_paths: list[LoadPath]
    point_loads_to_floors: list[dict]
    bearing_wall_loads: list[dict]
    roof_planes_geometry: list[dict]   # serialised plane geometry for the Drawer
    warnings: list[str]
    summary: dict


# ──────────────────────────────────────────────────────────────────────────────
# Core Designer
# ──────────────────────────────────────────────────────────────────────────────

class RoofStructuralDesigner:
    """
    EOR Roof Structural Design Tool.

    Usage:
        arch   = ArchitecturalInput(...)
        output = RoofStructuralDesigner(arch).design()
        print_summary(output)
        files  = export_to_files(output, "./outputs")
    """

    def __init__(self, arch: ArchitecturalInput) -> None:
        self.arch = arch
        self._loads: Optional[DesignLoads] = None
        self._trusses: list[TrussInfo] = []
        self._girders: list[GirderInfo] = []

    def design(self) -> RoofDesignOutput:
        # ── Guard: hip roofs are not yet supported ─────────────────────────────
        if self.arch.roof_type.lower() in {"hip", "dutch_gable", "dutch hip"}:
            raise NotImplementedError(
                f"Roof type '{self.arch.roof_type}' is not yet supported. "
                "Hip girder and hip jack truss geometry requires a separate "
                "implementation branch. Set roof_type='gable' for rectangular plans."
            )

        self._loads   = self._calculate_loads()
        self._trusses = self._layout_trusses()
        self._girders = self._identify_girders()

        drift_loads       = self._calculate_drift_loads()
        deflection_checks = self._check_deflection_limits()
        bearing_points    = self._collect_bearing_points()
        load_paths        = self._trace_load_paths(bearing_points)
        point_loads       = self._package_point_loads(bearing_points)
        wall_loads        = self._package_wall_loads()
        warnings          = self._generate_warnings(bearing_points, load_paths, drift_loads)

        # ── Serialise roof plane geometry for the Drawer ───────────────────────
        planes_geo = [
            {
                "id": p.id,
                "pitch_str": p.pitch_str,
                "pitch_degrees": round(p.pitch_degrees, 2),
                "perimeter": [{"x": pt.x, "y": pt.y} for pt in p.perimeter],
                "eave_height_ft": p.eave_height_ft,
                "ridge_height_ft": round(p.ridge_height_ft, 2),
                "width_ft": round(p.width_ft, 1),
                "depth_ft": round(p.depth_ft, 1),
                "area_sqft": round(p.area_sqft, 1),
                "overhang_ft": p.overhang_ft,
            }
            for p in self.arch.roof_planes
        ]

        df = {
            "OK_typical":          sum(1 for d in deflection_checks if d.flag == "OK_typical"),
            "VERIFY_DEFLECTION":   sum(1 for d in deflection_checks if d.flag == "VERIFY_DEFLECTION"),
            "CRITICAL_DEFLECTION": sum(1 for d in deflection_checks if d.flag == "CRITICAL_DEFLECTION"),
        }

        return RoofDesignOutput(
            loads=self._loads,
            trusses=self._trusses,
            girders=self._girders,
            bearing_points=bearing_points,
            drift_loads=drift_loads,
            deflection_checks=deflection_checks,
            load_paths=load_paths,
            point_loads_to_floors=point_loads,
            bearing_wall_loads=wall_loads,
            roof_planes_geometry=planes_geo,
            warnings=warnings,
            summary={
                "city": self.arch.city,
                "province": self.arch.province,
                "roof_type": self.arch.roof_type,
                "dead_psf": self._loads.dead_psf,
                "design_snow_psf": self._loads.design_snow_psf,
                "strength_combo_psf": self._loads.strength_combo_psf,
                "truss_spacing_in": self._loads.truss_spacing_in,
                "truss_count": len(self._trusses),
                "girder_count": len(self._girders),
                "drift_zones": len(drift_loads),
                "critical_bearing_points": sum(1 for bp in bearing_points if bp.critical),
                "load_paths_complete": sum(1 for lp in load_paths if lp.complete),
                "load_paths_incomplete": sum(1 for lp in load_paths if not lp.complete),
                "deflection_flags": df,
                "warning_count": len(warnings),
            },
        )

    # ── Step 1: Loads (NBCC 2020 §4.1.6) ─────────────────────────────────────

    def _calculate_loads(self) -> DesignLoads:
        city = self.arch.city
        if city not in NBCC_CLIMATE:
            raise ValueError(
                f"City '{city}' not in NBCC climate table. "
                "Add it to NBCC_CLIMATE or pass explicit snow loads."
            )
        cl = NBCC_CLIMATE[city]
        Ss, Sr = cl["Ss"], cl["Sr"]
        Is = SNOW_IMPORTANCE[self.arch.importance_category]

        alpha = max(p.pitch_degrees for p in self.arch.roof_planes)

        if alpha <= 15.0:
            Cs = 1.0
        elif alpha <= 60.0:
            Cs = round(1.0 - (alpha - 15.0) / 45.0, 3)
        else:
            Cs = 0.0

        Cw = 0.75 if self.arch.wind_exposure == "exposed" else 1.0
        Cb, Ca = 0.8, 1.0

        S_kPa = Is * (Ss * Cb * Cw * Cs * Ca + Sr)
        S_psf = round(S_kPa * KPA_TO_PSF, 1)

        comps  = DEAD_LOAD_COMPONENTS.get(self.arch.roofing_material,
                                           DEAD_LOAD_COMPONENTS["asphalt_shingle"])
        DL_psf = round(sum(comps.values()), 1)
        if self.arch.point_load_items:
            total_area = sum(p.area_sqft for p in self.arch.roof_planes)
            if total_area > 0:
                DL_psf += round(
                    sum(i.weight_lbs for i in self.arch.point_load_items) / total_area, 1
                )

        service  = round(DL_psf + S_psf, 1)
        strength = round(1.25 * DL_psf + 1.5 * S_psf, 1)

        spacing_in = 12 if S_psf >= 60 else (16 if S_psf >= 35 else 24)

        drift_note = ""
        if S_psf >= 25 and len(self.arch.roof_planes) > 1:
            drift_note = (
                "DRIFT REQUIRED: Multiple roof planes detected. "
                "Valley accumulation calculated below per NBCC 4.1.6.5."
            )
        elif S_psf >= 35:
            drift_note = (
                "DRIFT NOTE: High snow region. "
                "Verify drift at parapets and obstructions."
            )

        return DesignLoads(
            dead_psf=DL_psf, ground_snow_kPa=Ss, rain_kPa=Sr,
            design_snow_psf=S_psf, service_combo_psf=service,
            strength_combo_psf=strength, truss_spacing_in=spacing_in,
            slope_factor_Cs=Cs, exposure_factor_Cw=Cw, Is=Is,
            drift_note=drift_note,
        )

    # ── Step 2: Truss Layout ──────────────────────────────────────────────────

    def _layout_trusses(self) -> list[TrussInfo]:
        trusses: list[TrussInfo] = []
        sp_ft = self._loads.truss_spacing_in / 12.0

        for plane in self.arch.roof_planes:
            x_min, y_min, x_max, y_max = plane.bbox
            spans_ew = plane.width_ft <= plane.depth_ft

            if spans_ew:
                span_ft = plane.width_ft
                len_ft  = plane.depth_ft
                walls   = self._find_walls_at_x(x_min, x_max)
            else:
                span_ft = plane.depth_ft
                len_ft  = plane.width_ft
                walls   = self._find_walls_at_y(y_min, y_max)

            if not walls:
                walls = [w.id for w in self.arch.bearing_walls[:2]]

            n = max(2, round(len_ft / sp_ft) + 1)
            positions = [i * (len_ft / (n - 1)) for i in range(n)]

            for i, pos in enumerate(positions):
                is_end  = (i == 0 or i == n - 1)
                trib_ft = sp_ft / 2.0 if is_end else sp_ft
                rd, rs, rf = self._truss_reactions(span_ft, trib_ft)
                trusses.append(TrussInfo(
                    id=f"{plane.id}_T{i+1:02d}",
                    kind="gable_end" if is_end else "common",
                    position_ft=round(pos, 2),
                    span_ft=round(span_ft, 1),
                    spacing_in=self._loads.truss_spacing_in,
                    bearing_wall_ids=walls[:2],
                    reaction_dead_lbs=round(rd, 0),
                    reaction_snow_lbs=round(rs, 0),
                    reaction_factored_lbs=round(rf, 0),
                    plane_id=plane.id,
                ))
        return trusses

    def _truss_reactions(
        self, span_ft: float, trib_ft: float
    ) -> tuple[float, float, float]:
        rd = self._loads.dead_psf        * trib_ft * span_ft / 2.0
        rs = self._loads.design_snow_psf * trib_ft * span_ft / 2.0
        rf = (1.25 * self._loads.dead_psf + 1.5 * self._loads.design_snow_psf) \
             * trib_ft * span_ft / 2.0
        return rd, rs, rf

    def _find_walls_at_x(self, x_min: float, x_max: float) -> list[str]:
        TOL, found = 1.5, []
        for w in self.arch.bearing_walls:
            if abs(w.start.x - w.end.x) < 0.5:
                if (abs(w.start.x - x_min) < TOL or abs(w.start.x - x_max) < TOL):
                    if w.id not in found:
                        found.append(w.id)
        return found

    def _find_walls_at_y(self, y_min: float, y_max: float) -> list[str]:
        TOL, found = 1.5, []
        for w in self.arch.bearing_walls:
            if abs(w.start.y - w.end.y) < 0.5:
                if (abs(w.start.y - y_min) < TOL or abs(w.start.y - y_max) < TOL):
                    if w.id not in found:
                        found.append(w.id)
        return found

    # ── Step 3: Girder Identification ─────────────────────────────────────────

    def _identify_girders(self) -> list[GirderInfo]:
        girders: list[GirderInfo] = []
        planes = self.arch.roof_planes

        for i in range(len(planes)):
            for j in range(i + 1, len(planes)):
                vx, vy = _bbox_shared_edge(planes[i], planes[j])
                if vx is not None:
                    g = self._make_valley_girder(planes[i], planes[j], vx, vy,
                                                  len(girders) + 1)
                    if g:
                        girders.append(g)

        for plane in planes:
            if plane.short_span_ft > 40.0:
                g = self._make_midspan_girder(plane, len(girders) + 1)
                if g:
                    girders.append(g)

        return girders

    def _make_valley_girder(
        self, plane_a: RoofPlane, plane_b: RoofPlane,
        valley_x: float, _: float, gid: int,
    ) -> Optional[GirderInfo]:
        bx_min, b_y_min, bx_max, b_y_max = plane_b.bbox
        ax_min, a_y_min, ax_max, a_y_max = plane_a.bbox

        if abs(bx_min - valley_x) < 1.5:
            # Side wing B frames in from the east. Jack trusses in B span E-W,
            # from valley_x (bx_min) to bx_max. Tributary = full E-W width / 2.
            gy_min, gy_max = b_y_min, b_y_max
            trib = (bx_max - bx_min) / 2.0
        elif abs(ax_min - valley_x) < 1.5:
            # Side wing A frames in from the east.
            gy_min, gy_max = a_y_min, a_y_max
            trib = (ax_max - ax_min) / 2.0
        else:
            # T-shape or Y-junction: valley runs E-W; jack trusses span N-S.
            if (a_y_max - a_y_min) <= (b_y_max - b_y_min):
                gy_min, gy_max = a_y_min, a_y_max
                trib = (a_y_max - a_y_min) / 2.0
            else:
                gy_min, gy_max = b_y_min, b_y_max
                trib = (b_y_max - b_y_min) / 2.0

        span_ft   = gy_max - gy_min
        eave_z    = max(plane_a.eave_height_ft, plane_b.eave_height_ft)
        direction = "y"

        td = self._loads.dead_psf        * trib * span_ft
        ts = self._loads.design_snow_psf * trib * span_ft
        tf = (1.25 * self._loads.dead_psf + 1.5 * self._loads.design_snow_psf) * trib * span_ft

        bp1 = self._make_girder_bp(f"G{gid}_BP1", f"G{gid}",
                                    valley_x, gy_min, eave_z, direction, td/2, ts/2)
        bp2 = self._make_girder_bp(f"G{gid}_BP2", f"G{gid}",
                                    valley_x, gy_max, eave_z, direction, td/2, ts/2)
        sp_ft = self._loads.truss_spacing_in / 12.0
        n_j   = max(1, int(span_ft / sp_ft))

        return GirderInfo(
            id=f"G{gid}", kind="valley_girder",
            span_ft=round(span_ft, 1),
            start_pt=Pt3(valley_x, gy_min, eave_z),
            end_pt=Pt3(valley_x, gy_max, eave_z),
            span_direction=direction,
            supported_truss_ids=[f"JT_{gid}_{k+1:02d}" for k in range(n_j)],
            total_dead_load_lbs=round(td, 0),
            total_snow_load_lbs=round(ts, 0),
            total_factored_load_lbs=round(tf, 0),
            bearing_points=[bp1, bp2],
            note=f"Valley at x={valley_x:.1f} between {plane_a.id} and {plane_b.id}",
        )

    def _make_midspan_girder(self, plane: RoofPlane, gid: int) -> Optional[GirderInfo]:
        x_min, y_min, x_max, y_max = plane.bbox

        if plane.width_ft >= plane.depth_ft:
            mid_y = (y_min + y_max) / 2.0
            span_ft, trib = plane.width_ft, plane.depth_ft / 4.0
            direction = "x"
            start_pt, end_pt = (Pt3(x_min, mid_y, plane.eave_height_ft),
                                 Pt3(x_max, mid_y, plane.eave_height_ft))
        else:
            mid_x = (x_min + x_max) / 2.0
            span_ft, trib = plane.depth_ft, plane.width_ft / 4.0
            direction = "y"
            start_pt, end_pt = (Pt3(mid_x, y_min, plane.eave_height_ft),
                                 Pt3(mid_x, y_max, plane.eave_height_ft))

        td = self._loads.dead_psf        * trib * span_ft
        ts = self._loads.design_snow_psf * trib * span_ft
        tf = (1.25 * self._loads.dead_psf + 1.5 * self._loads.design_snow_psf) * trib * span_ft

        bp1 = self._make_girder_bp(f"G{gid}_BP1", f"G{gid}",
                                    start_pt.x, start_pt.y, start_pt.z, direction, td/2, ts/2)
        bp2 = self._make_girder_bp(f"G{gid}_BP2", f"G{gid}",
                                    end_pt.x, end_pt.y, end_pt.z, direction, td/2, ts/2)
        return GirderInfo(
            id=f"G{gid}", kind="master_girder",
            span_ft=round(span_ft, 1),
            start_pt=start_pt, end_pt=end_pt,
            span_direction=direction, supported_truss_ids=[],
            total_dead_load_lbs=round(td, 0),
            total_snow_load_lbs=round(ts, 0),
            total_factored_load_lbs=round(tf, 0),
            bearing_points=[bp1, bp2],
            note=(f"Midspan support for {plane.id} "
                  f"({plane.short_span_ft:.0f} ft truss span > 40 ft)"),
        )

    def _make_girder_bp(
        self, bp_id: str, source_id: str,
        x: float, y: float, z: float,
        load_direction: str,
        dead: float, snow: float,
    ) -> BearingPoint:
        uf = round(dead + snow, 0)
        fa = round(1.25 * dead + 1.5 * snow, 0)
        return BearingPoint(
            id=bp_id, source_id=source_id, source_kind="girder",
            location=Pt3(x, y, z),
            load_direction=load_direction,
            load_kind="point",
            dead_lbs=round(dead, 0), snow_lbs=round(snow, 0),
            unfactored_lbs=uf, factored_lbs=fa,
            post_requirement=_post_spec(fa), critical=True,
        )

    # ── Step 4: Drift Loads (NBCC 4.1.6.5) ────────────────────────────────────

    def _calculate_drift_loads(self) -> list[DriftLoad]:
        """
        Valley drift per NBCC 4.1.6.5 — empirical estimate.

        hd = min( 0.8 × sqrt(lu_m),  1.5 × hs_m )
          lu = upwind fetch (average short span of both planes, in metres)
          hs = base snow depth on roof = S_kPa / ρ_snow

        Drift is triangular:  peak at valley line → zero at xd each side.
        xd = min(4 × hd,  lu / 2)    (in metres, converted to ft)

        Peak additional load = ρ_snow × hd  [kPa → psf]
        """
        drifts: list[DriftLoad] = []
        Ss = NBCC_CLIMATE[self.arch.city]["Ss"]
        Is = self._loads.Is
        S_kPa = Is * Ss * 0.8   # base roof snow (Cb=0.8, Cw=1.0)

        for girder in self._girders:
            if girder.kind != "valley_girder":
                continue

            planes = self.arch.roof_planes
            lu_ft  = sum(p.short_span_ft for p in planes) / max(len(planes), 1)
            lu_m   = lu_ft * FT_TO_M

            hs_m   = S_kPa / RHO_SNOW_KN_M3   # base snow depth
            hd_m   = max(0.0, min(0.8 * math.sqrt(lu_m), 1.5 * hs_m))
            xd_m   = min(4.0 * hd_m, lu_m / 2.0)
            xd_ft  = round(xd_m / FT_TO_M, 1)

            peak_kPa = RHO_SNOW_KN_M3 * hd_m
            peak_psf = round(peak_kPa * KPA_TO_PSF, 1)
            avg_psf  = round(peak_psf * 0.5, 1)

            if peak_psf < 1.0:
                continue   # negligible — skip

            valley_x = girder.start_pt.x
            sp_ft    = self._loads.truss_spacing_in / 12.0
            affected  = []
            for t in self._trusses:
                plane = next(
                    (p for p in self.arch.roof_planes if p.id == t.plane_id), None
                )
                if plane is None:
                    continue
                _, py_min, _, _ = plane.bbox
                t_pos_global = py_min + t.position_ft
                dist_to_valley = abs(t_pos_global - valley_x)
                if dist_to_valley <= xd_ft + sp_ft:
                    affected.append(t.id)
                    t.in_drift_zone = True

            drifts.append(DriftLoad(
                id=f"DRIFT_{girder.id}", kind="valley",
                valley_x=valley_x,
                y_start=girder.start_pt.y, y_end=girder.end_pt.y,
                drift_width_ft=xd_ft,
                peak_additional_psf=peak_psf,
                avg_additional_psf=avg_psf,
                affected_truss_ids=affected,
                source_planes=[p.id for p in planes],
                note=(
                    f"NBCC 4.1.6.5 estimate. "
                    f"hd={hd_m*100:.0f} cm, drift ±{xd_ft:.1f} ft. "
                    f"EOR to confirm full calculation."
                ),
            ))
        return drifts

    # ── Step 5: Deflection Checks (NBCC Table 4.1.5.3) ───────────────────────

    def _check_deflection_limits(self) -> list[DeflectionCheck]:
        """
        L/180 under live (snow) load only.
        L/240 under total (D+S) — or L/360 for brittle finishes.

        EI required = 5wL⁴ / (384 × δ_limit)   [lb·in²]
          w   = uniform service load (lb/in)
          L   = span (in)
          δ   = L / limit_divisor
        """
        brittle = self.arch.has_cathedral_ceiling or \
                  self.arch.roofing_material in {"clay_tile", "concrete_tile"}
        tot_div = DEF_BRITTLE if brittle else DEF_TOTAL

        def req_ei(w_plf: float, span_ft: float, divisor: int) -> float:
            L_in = span_ft * 12.0
            dlt  = L_in / divisor
            w_pli = w_plf / 12.0
            return (5.0 * w_pli * L_in**4) / (384.0 * dlt)

        def flag(span_ft: float, req_ei: float, depth_div: float) -> str:
            """
            Compare required EI against a typical 2x4 SPF #2 parallel-chord truss
            (or girder) built to the recommended minimum depth.

            Typical EI model — two 2x4 chords (A=5.25 in², I_own=5.36 in⁴, E=1.6M psi):
              d_rec   = span_in / depth_div   (span/20 trusses, span/15 girders)
              I_eff   = 2×I_own + 2×A×(d_rec/2)²   [parallel-axis theorem]
              EI_typ  = E × I_eff

            Thresholds (ratio = req_ei / EI_typ):
              < 0.65  → OK_typical         standard truss at rec. depth can handle this
              0.65–1.0→ VERIFY_DEFLECTION   near capacity; manufacturer must confirm EI
              ≥ 1.0   → CRITICAL_DEFLECTION exceeds typical; deeper truss / LVL chords needed
            """
            L_in   = span_ft * 12.0
            d_rec  = L_in / depth_div
            I_eff  = 2 * 5.36 + 2 * 5.25 * (d_rec / 2) ** 2
            EI_typ = 1_600_000 * I_eff
            ratio  = req_ei / EI_typ
            if ratio < 0.65:  return "OK_typical"
            if ratio < 1.00:  return "VERIFY_DEFLECTION"
            return "CRITICAL_DEFLECTION"

        checks: list[DeflectionCheck] = []
        sp_ft = self._loads.truss_spacing_in / 12.0

        for t in self._trusses:
            w_tot  = (self._loads.dead_psf + self._loads.design_snow_psf) * sp_ft
            w_snow = self._loads.design_snow_psf * sp_ft
            L_in   = t.span_ft * 12.0
            ei_l   = req_ei(w_snow, t.span_ft, DEF_LIVE)
            ei_t   = req_ei(w_tot,  t.span_ft, tot_div)
            gov    = max(ei_l, ei_t)
            checks.append(DeflectionCheck(
                element_id=t.id, element_type="truss",
                span_ft=t.span_ft,
                service_load_plf=round(w_tot, 1),
                limit_live_in=round(L_in / DEF_LIVE, 3),
                limit_total_in=round(L_in / tot_div, 3),
                req_EI_live_lb_in2=round(ei_l, 0),
                req_EI_total_lb_in2=round(ei_t, 0),
                governing_EI_lb_in2=round(gov, 0),
                min_depth_rec_in=round(t.span_ft * 12.0 / 20.0, 1),
                flag=flag(t.span_ft, gov, 20.0),
            ))

        for g in self._girders:
            trib   = max(g.span_ft / max(len(g.supported_truss_ids), 1), sp_ft)
            w_tot  = (self._loads.dead_psf + self._loads.design_snow_psf) * trib
            w_snow = self._loads.design_snow_psf * trib
            L_in   = g.span_ft * 12.0
            ei_l   = req_ei(w_snow, g.span_ft, DEF_LIVE)
            ei_t   = req_ei(w_tot,  g.span_ft, tot_div)
            gov    = max(ei_l, ei_t)
            checks.append(DeflectionCheck(
                element_id=g.id, element_type="girder",
                span_ft=g.span_ft,
                service_load_plf=round(w_tot, 1),
                limit_live_in=round(L_in / DEF_LIVE, 3),
                limit_total_in=round(L_in / tot_div, 3),
                req_EI_live_lb_in2=round(ei_l, 0),
                req_EI_total_lb_in2=round(ei_t, 0),
                governing_EI_lb_in2=round(gov, 0),
                min_depth_rec_in=round(g.span_ft * 12.0 / 15.0, 1),
                flag=flag(g.span_ft, gov, 15.0),
            ))

        return checks

    # ── Step 6: Bearing Points ────────────────────────────────────────────────

    def _collect_bearing_points(self) -> list[BearingPoint]:
        bps: list[BearingPoint] = []
        for g in self._girders:
            bps.extend(g.bearing_points)
        for item in self.arch.point_load_items:
            dead_e = item.weight_lbs * 0.9
            snow_e = item.weight_lbs * 0.1
            fa     = round(1.25 * dead_e + 1.5 * snow_e, 0)
            bps.append(BearingPoint(
                id=f"PTL_{item.id}", source_id=item.id, source_kind="point_load_item",
                location=Pt3(item.center.x, item.center.y,
                              self.arch.roof_planes[0].eave_height_ft),
                load_direction=None, load_kind="point",
                dead_lbs=round(dead_e, 0), snow_lbs=round(snow_e, 0),
                unfactored_lbs=round(dead_e + snow_e, 0), factored_lbs=fa,
                post_requirement=_post_spec(fa),
                critical=item.weight_lbs > 2_000,
            ))
        return bps

    # ── Step 7: Load Path Tracing ─────────────────────────────────────────────

    def _trace_load_paths(self, bearing_points: list[BearingPoint]) -> list[LoadPath]:
        levels = (
            ["2nd_floor_wall", "1st_floor_wall", "foundation"]
            if self.arch.num_stories >= 2
            else ["1st_floor_wall", "foundation"]
        )
        paths: list[LoadPath] = []

        for bp in bearing_points:
            stages: list[LoadPathStage] = []
            complete = True
            warning  = ""

            for level in levels:
                wall = self._bearing_support_at(
                    bp.location.x, bp.location.y, bp.load_direction
                )
                if wall is None:
                    stages.append(LoadPathStage(
                        level=level, element_kind="transfer_beam_required",
                        element_id=None, status="DISCONTINUITY",
                    ))
                    complete = False
                    warning = (
                        f"No bearing wall at ({bp.location.x:.1f}, {bp.location.y:.1f}) "
                        f"at level '{level}'. Transfer beam required."
                    )
                    break

                kind = "bearing_wall" if "wall" in level else "foundation"
                stages.append(LoadPathStage(
                    level=level, element_kind=kind,
                    element_id=wall.id, status="OK",
                ))

                if not wall.continuous_to_foundation and level != "foundation":
                    stages.append(LoadPathStage(
                        level=f"below_{level}",
                        element_kind="transfer_beam_required",
                        element_id=None, status="DISCONTINUITY",
                    ))
                    complete = False
                    warning = (
                        f"Wall '{wall.id}' under {bp.id} is NOT continuous to foundation. "
                        f"Transfer beam required below {level}."
                    )
                    break

            paths.append(LoadPath(
                bearing_point_id=bp.id, stages=stages,
                complete=complete, warning=warning,
            ))
        return paths

    def _bearing_support_at(
        self, x: float, y: float,
        load_direction: Optional[str] = None,
    ) -> Optional[BearingWall]:
        """
        Find the supporting wall under point (x, y).

        CORNER-POINT DISAMBIGUATION:
        When multiple walls meet at the same point (e.g. corner of L-shaped house),
        use the girder's load_direction to prefer the wall perpendicular to the span:
          - load_direction="y" (girder runs N-S) → load goes into E-W walls (direction=="x")
          - load_direction="x" (girder runs E-W) → load goes into N-S walls (direction=="y")
        This ensures the correct wall's continuous_to_foundation flag is checked.
        """
        TOL = 2.0
        cands: list[tuple[float, BearingWall]] = []
        for w in self.arch.bearing_walls:
            d = _pt_to_segment_dist(x, y, w.start.x, w.start.y, w.end.x, w.end.y)
            if d <= TOL:
                cands.append((d, w))
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0][1]

        # Disambiguate by perpendicular wall direction
        if load_direction == "y":
            preferred = [(d, w) for d, w in cands if w.direction == "x"]
        elif load_direction == "x":
            preferred = [(d, w) for d, w in cands if w.direction == "y"]
        else:
            preferred = []

        pool = preferred if preferred else cands
        return min(pool, key=lambda t: t[0])[1]

    # ── Step 8: Package Outputs ───────────────────────────────────────────────

    def _package_point_loads(self, bearing_points: list[BearingPoint]) -> list[dict]:
        return [
            {
                "id": bp.id, "source_kind": bp.source_kind,
                "x_ft": bp.location.x, "y_ft": bp.location.y, "z_ft": bp.location.z,
                "dead_lbs": bp.dead_lbs, "snow_lbs": bp.snow_lbs,
                "unfactored_lbs": bp.unfactored_lbs, "factored_lbs": bp.factored_lbs,
                "required_support": bp.post_requirement,
                "must_reach_foundation": True, "critical": bp.critical,
            }
            for bp in bearing_points if bp.critical
        ]

    def _package_wall_loads(self) -> list[dict]:
        """Aggregate truss reactions per bearing wall, DL and SL separate."""
        dead_map: dict[str, float] = {}
        snow_map: dict[str, float] = {}
        for t in self._trusses:
            for wid in t.bearing_wall_ids:
                dead_map[wid] = dead_map.get(wid, 0.0) + t.reaction_dead_lbs
                snow_map[wid] = snow_map.get(wid, 0.0) + t.reaction_snow_lbs

        results = []
        for wid, td in dead_map.items():
            ts   = snow_map.get(wid, 0.0)
            wall = next((w for w in self.arch.bearing_walls if w.id == wid), None)
            if wall and wall.length_ft > 0:
                L = wall.length_ft
                results.append({
                    "wall_id": wid,
                    "start_x": wall.start.x, "start_y": wall.start.y,
                    "end_x":   wall.end.x,   "end_y":   wall.end.y,
                    "wall_length_ft": round(L, 1),
                    "dead_total_lbs":  round(td, 0),
                    "snow_total_lbs":  round(ts, 0),
                    "dead_plf":        round(td / L, 0),
                    "snow_plf":        round(ts / L, 0),
                    "factored_plf":    round((1.25 * td + 1.5 * ts) / L, 0),
                    "continuous_to_foundation": wall.continuous_to_foundation,
                })
        return results

    # ── Warnings ──────────────────────────────────────────────────────────────

    def _generate_warnings(
        self,
        bearing_points: list[BearingPoint],
        load_paths: list[LoadPath],
        drift_loads: list[DriftLoad],
    ) -> list[str]:
        W: list[str] = []

        for lp in load_paths:
            if not lp.complete:
                W.append(f"⚠  LOAD PATH BREAK — {lp.bearing_point_id}: {lp.warning}")

        for bp in bearing_points:
            if bp.factored_lbs > 15_000:
                W.append(
                    f"⚠  HIGH POINT LOAD — {bp.id}: {bp.factored_lbs:,.0f} lbs. "
                    "Verify post + foundation capacity."
                )

        for t in self._trusses:
            if t.span_ft > 40:
                W.append(
                    f"⚠  LONG SPAN — {t.id}: {t.span_ft:.0f} ft. "
                    "L/240 deflection likely governs — verify EI with manufacturer."
                )

        if self._loads.drift_note:
            W.append(f"⚠  DRIFT — {self._loads.drift_note}")

        for d in drift_loads:
            W.append(
                f"⚠  DRIFT ZONE — {d.id}: +{d.peak_additional_psf} psf peak "
                f"over ±{d.drift_width_ft} ft of valley. "
                f"{len(d.affected_truss_ids)} trusses in zone. EOR to confirm."
            )

        if (self.arch.roofing_material in {"clay_tile", "concrete_tile"}
                and self._loads.design_snow_psf > 30):
            W.append(
                "⚠  HEAVY ROOFING + HIGH SNOW — tile under significant snow. "
                "Verify truss top chord under DL+SL."
            )

        for opening in self.arch.roof_openings:
            for girder in self._girders:
                for bp in girder.bearing_points:
                    d = math.hypot(opening.center.x - bp.location.x,
                                   opening.center.y - bp.location.y)
                    if d < 6.0:
                        W.append(
                            f"⚠  OPENING NEAR GIRDER — '{opening.id}' is {d:.1f} ft "
                            f"from bearing '{bp.id}'. Double framing required."
                        )

        if self.arch.has_cathedral_ceiling:
            W.append(
                "⚠  CATHEDRAL CEILING — ridge beam is structural (L/360). "
                "Posts at ridge beam ends must reach foundation."
            )
        return W


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _post_spec(factored_lbs: float) -> str:
    if factored_lbs < 5_000:   return "double_2x6_stud_pack"
    if factored_lbs < 10_000:  return "triple_2x6_stud_pack"
    if factored_lbs < 18_000:  return "4x4_post_or_quad_2x6"
    if factored_lbs < 30_000:  return "6x6_post_or_engineered_column"
    return "engineered_steel_column — consult structural"


def _pt_to_segment_dist(
    px: float, py: float,
    x1: float, y1: float, x2: float, y2: float,
) -> float:
    dx, dy = x2 - x1, y2 - y1
    sq = dx * dx + dy * dy
    if sq == 0.0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / sq))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _bbox_shared_edge(
    a: RoofPlane, b: RoofPlane
) -> tuple[Optional[float], Optional[float]]:
    TOL = 1.5
    ax0, ay0, ax1, ay1 = a.bbox
    bx0, by0, bx1, by1 = b.bbox
    if abs(ax1 - bx0) < TOL:  return ax1, min(ay0, by0)
    if abs(bx1 - ax0) < TOL:  return bx1, min(ay0, by0)
    if abs(ay1 - by0) < TOL:  return min(ax0, bx0), ay1
    if abs(by1 - ay0) < TOL:  return min(ax0, bx0), by1
    return None, None


# ──────────────────────────────────────────────────────────────────────────────
# JSON Export
# ──────────────────────────────────────────────────────────────────────────────

def to_json(output: RoofDesignOutput) -> dict:
    """
    Return a structured dict (json-serialisable) with these top-level keys:

    metadata, design_loads, truss_layout, girder_layout, drift_zones,
    deflection_checks, bearing_points, load_paths, warnings, summary

    Consumer packages:
      for_foundation      — column + wall loads for footing design
      for_frame_analyzer  — DL/SL-split, PyNite-ready nodes + distributed loads
      for_drawing         — 3-D geometry, annotations, notes table
    """
    L = output.loads

    loads_d = {
        "dead_psf": L.dead_psf,
        "ground_snow_kPa": L.ground_snow_kPa,
        "rain_kPa": L.rain_kPa,
        "design_snow_psf": L.design_snow_psf,
        "slope_factor_Cs": L.slope_factor_Cs,
        "exposure_factor_Cw": L.exposure_factor_Cw,
        "importance_factor_Is": L.Is,
        "service_combo_psf": L.service_combo_psf,
        "strength_combo_psf": L.strength_combo_psf,
        "recommended_spacing_in": L.truss_spacing_in,
        "drift_note": L.drift_note,
        "load_combinations": {
            "strength_gravity": "1.25D + 1.5S",
            "service_gravity": "D + S",
            "uplift": "0.9D - 1.4W (wind — verify separately)",
        },
    }

    trusses_j = [
        {
            "id": t.id, "kind": t.kind, "plane_id": t.plane_id,
            "position_ft": t.position_ft, "span_ft": t.span_ft,
            "spacing_in": t.spacing_in,
            "bearing_wall_ids": t.bearing_wall_ids,
            "reaction_dead_lbs": t.reaction_dead_lbs,
            "reaction_snow_lbs": t.reaction_snow_lbs,
            "reaction_factored_lbs": t.reaction_factored_lbs,
            "in_drift_zone": t.in_drift_zone,
        }
        for t in output.trusses
    ]

    girders_j = [
        {
            "id": g.id, "kind": g.kind,
            "span_ft": g.span_ft, "span_direction": g.span_direction,
            "start": g.start_pt.to_dict(), "end": g.end_pt.to_dict(),
            "total_dead_lbs": g.total_dead_load_lbs,
            "total_snow_lbs": g.total_snow_load_lbs,
            "total_factored_lbs": g.total_factored_load_lbs,
            "note": g.note,
            "bearing_points": [
                {
                    "id": bp.id, "location": bp.location.to_dict(),
                    "dead_lbs": bp.dead_lbs, "snow_lbs": bp.snow_lbs,
                    "unfactored_lbs": bp.unfactored_lbs,
                    "factored_lbs": bp.factored_lbs,
                    "post_requirement": bp.post_requirement,
                }
                for bp in g.bearing_points
            ],
        }
        for g in output.girders
    ]

    drift_j = [
        {
            "id": d.id, "kind": d.kind,
            "valley_x": d.valley_x, "y_start": d.y_start, "y_end": d.y_end,
            "drift_width_ft": d.drift_width_ft,
            "peak_additional_psf": d.peak_additional_psf,
            "avg_additional_psf": d.avg_additional_psf,
            "affected_truss_count": len(d.affected_truss_ids),
            "affected_truss_ids": d.affected_truss_ids,
            "note": d.note,
        }
        for d in output.drift_loads
    ]

    defl_j = [
        {
            "element_id": d.element_id, "element_type": d.element_type,
            "span_ft": d.span_ft, "service_load_plf": d.service_load_plf,
            "limit_live_in": d.limit_live_in, "limit_total_in": d.limit_total_in,
            "req_EI_live_lb_in2": d.req_EI_live_lb_in2,
            "req_EI_total_lb_in2": d.req_EI_total_lb_in2,
            "governing_EI_lb_in2": d.governing_EI_lb_in2,
            "min_depth_rec_in": d.min_depth_rec_in,
            "flag": d.flag,
        }
        for d in output.deflection_checks
    ]

    bp_j = [
        {
            "id": bp.id, "source_kind": bp.source_kind,
            "location": bp.location.to_dict(),
            "dead_lbs": bp.dead_lbs, "snow_lbs": bp.snow_lbs,
            "unfactored_lbs": bp.unfactored_lbs,
            "factored_lbs": bp.factored_lbs,
            "post_requirement": bp.post_requirement,
            "critical": bp.critical,
        }
        for bp in output.bearing_points
    ]

    lp_j = [
        {
            "bearing_point_id": lp.bearing_point_id,
            "complete": lp.complete, "warning": lp.warning,
            "stages": [
                {"level": s.level, "element_kind": s.element_kind,
                 "element_id": s.element_id, "status": s.status}
                for s in lp.stages
            ],
        }
        for lp in output.load_paths
    ]

    # ── for_foundation ─────────────────────────────────────────────────────────
    for_foundation = {
        "_description": (
            "All vertical roof loads for foundation sizing. "
            "Apply NBCC 4.1.3.2 combinations. "
            "Footing size = load / soil bearing capacity (not determined here)."
        ),
        "column_loads": [
            {
                "id": pl["id"], "x_ft": pl["x_ft"], "y_ft": pl["y_ft"],
                "source_kind": pl["source_kind"],
                "service_lbs": pl["unfactored_lbs"],
                "factored_lbs": pl["factored_lbs"],
                "required_post": pl["required_support"],
                "note": "Continuous vertical load path to footing required.",
            }
            for pl in output.point_loads_to_floors
        ],
        "wall_line_loads": [
            {
                "wall_id": wl["wall_id"],
                "start": {"x": wl["start_x"], "y": wl["start_y"]},
                "end":   {"x": wl["end_x"],   "y": wl["end_y"]},
                "length_ft": wl["wall_length_ft"],
                "dead_plf": wl["dead_plf"],
                "snow_plf": wl["snow_plf"],
                "factored_plf": wl["factored_plf"],
                "continuous_to_foundation": wl["continuous_to_foundation"],
            }
            for wl in output.bearing_wall_loads
        ],
        "drift_zone_additional": [
            {
                "zone_id": d.id,
                "valley_x": d.valley_x, "y_range": [d.y_start, d.y_end],
                "peak_additional_psf": d.peak_additional_psf,
                "drift_width_ft": d.drift_width_ft,
                "note": "Add to base snow for foundation sizing in this zone.",
            }
            for d in output.drift_loads
        ],
    }

    # ── for_frame_analyzer ─────────────────────────────────────────────────────
    for_frame = {
        "_description": (
            "Roof loads for frame analyzer (PyNite). "
            "Dead and snow are separated for NBCC load combinations. "
            "Exterior walls are always bearing. "
            "interior_support_requirements: frame analyzer decides post vs wall vs beam."
        ),
        "load_cases": {
            "dead": {
                "distributed_wall_loads": [
                    {
                        "wall_id": wl["wall_id"],
                        "start": {"x": wl["start_x"], "y": wl["start_y"]},
                        "end":   {"x": wl["end_x"],   "y": wl["end_y"]},
                        "w_plf": wl["dead_plf"], "direction": "Fy",
                    }
                    for wl in output.bearing_wall_loads
                ],
                "point_loads": [
                    {
                        "id": pl["id"],
                        "x_ft": pl["x_ft"], "y_ft": pl["y_ft"], "z_ft": pl["z_ft"],
                        "Fy_lbs": -pl["dead_lbs"],
                    }
                    for pl in output.point_loads_to_floors
                ],
            },
            "snow": {
                "distributed_wall_loads": [
                    {
                        "wall_id": wl["wall_id"],
                        "start": {"x": wl["start_x"], "y": wl["start_y"]},
                        "end":   {"x": wl["end_x"],   "y": wl["end_y"]},
                        "w_plf": wl["snow_plf"], "direction": "Fy",
                    }
                    for wl in output.bearing_wall_loads
                ],
                "point_loads": [
                    {
                        "id": pl["id"],
                        "x_ft": pl["x_ft"], "y_ft": pl["y_ft"], "z_ft": pl["z_ft"],
                        "Fy_lbs": -pl["snow_lbs"],
                    }
                    for pl in output.point_loads_to_floors
                ],
                "drift_zone_upgrades": [
                    {
                        "zone_id": d.id,
                        "valley_x": d.valley_x, "y_range": [d.y_start, d.y_end],
                        "drift_width_ft": d.drift_width_ft,
                        "peak_additional_psf": d.peak_additional_psf,
                        "note": "Triangular additional snow in drift zone.",
                    }
                    for d in output.drift_loads
                ],
            },
        },
        "load_combinations": {
            "factored_gravity": "1.25 × Dead + 1.5 × Snow",
            "service_for_deflection": "Dead + Snow",
        },
        "deflection_requirements": defl_j,
        "interior_support_requirements": [
            {
                "id": pl["id"],
                "x_ft": pl["x_ft"], "y_ft": pl["y_ft"],
                "factored_lbs": pl["factored_lbs"],
                "required_support_hint": pl["required_support"],
                "note": (
                    "Frame analyzer must provide vertical support here. "
                    "Determine whether post, extended bearing wall, "
                    "or transfer beam — based on frame analysis."
                ),
            }
            for pl in output.point_loads_to_floors
        ],
    }

    # ── for_drawing ─────────────────────────────────────────────────────────────
    for_drawing = {
        "_description": (
            "3-D geometry and annotation data for structural roof framing plan. "
            "Coordinates in feet, Z = elevation above grade."
        ),
        "building_outline": {
            "roof_type": output.summary.get("roof_type", "gable"),
            "planes": output.roof_planes_geometry,
        },
        "truss_layout_plan": {
            "typical_spacing_in": L.truss_spacing_in,
            "trusses": [
                {
                    "id": t.id, "kind": t.kind,
                    "position_ft": t.position_ft, "span_ft": t.span_ft,
                    "plane_id": t.plane_id, "in_drift_zone": t.in_drift_zone,
                    "line_weight": "thin" if t.kind == "common" else "heavy",
                }
                for t in output.trusses
            ],
        },
        "girders": [
            {
                "id": g.id, "kind": g.kind,
                "start_2d": {"x": g.start_pt.x, "y": g.start_pt.y},
                "end_2d":   {"x": g.end_pt.x,   "y": g.end_pt.y},
                "start_3d": g.start_pt.to_dict(),
                "end_3d":   g.end_pt.to_dict(),
                "span_ft": g.span_ft,
                "total_factored_lbs": g.total_factored_load_lbs,
                "line_weight": "heavy",
                "label": (
                    f"{g.id.upper()} {g.kind.replace('_',' ').upper()} | "
                    f"{g.span_ft:.0f}'-0\" | {g.total_factored_load_lbs:,.0f} lbs factored"
                ),
            }
            for g in output.girders
        ],
        "post_symbols": [
            {
                "id": bp.id,
                "x_ft": bp.location.x, "y_ft": bp.location.y,
                "symbol": "filled_triangle",
                "label": f"{bp.post_requirement}\n{bp.factored_lbs:,.0f} lbs",
                "flag": "CRITICAL — continuous to foundation",
            }
            for bp in output.bearing_points if bp.critical
        ],
        "drift_zone_hatching": [
            {
                "id": d.id, "valley_x": d.valley_x,
                "y_start": d.y_start, "y_end": d.y_end,
                "hatch_width_ft": d.drift_width_ft,
                "label": f"DRIFT ZONE\n+{d.peak_additional_psf} psf peak",
            }
            for d in output.drift_loads
        ],
        "notes_table": [
            f"ALL TRUSSES @ {L.truss_spacing_in}\" O.C. UNLESS NOTED",
            f"DESIGN SNOW LOAD: {L.design_snow_psf} psf  "
            f"(Ss={L.ground_snow_kPa} kPa, Cs={L.slope_factor_Cs}, Cw={L.exposure_factor_Cw})",
            f"DEAD LOAD: {L.dead_psf} psf",
            f"FACTORED COMBINATION: {L.strength_combo_psf} psf  (1.25D + 1.5S)",
            "DESIGN CODE: NBCC 2020",
            "ALL GIRDER BEARING POINTS — CONTINUOUS LOAD PATH TO FOUNDATION REQUIRED.",
            "TRUSS DESIGN BY MANUFACTURER — SUPPLY SPAN, LOAD, AND DEFLECTION LIMITS.",
        ] + (["DRIFT ZONE PRESENT — SEE HATCHING. UPGRADE TRUSSES IN ZONE."]
             if output.drift_loads else []),
    }

    return {
        "metadata": {
            "city": output.summary["city"],
            "province": output.summary["province"],
            "code": "NBCC_2020",
        },
        "design_loads": loads_d,
        "truss_layout": trusses_j,
        "girder_layout": girders_j,
        "drift_zones": drift_j,
        "deflection_checks": defl_j,
        "bearing_points": bp_j,
        "load_paths": lp_j,
        "warnings": output.warnings,
        "summary": output.summary,
        "for_foundation": for_foundation,
        "for_frame_analyzer": for_frame,
        "for_drawing": for_drawing,
    }


# ──────────────────────────────────────────────────────────────────────────────
# CSV Export
# ──────────────────────────────────────────────────────────────────────────────

def to_csv_tables(output: RoofDesignOutput) -> dict[str, str]:
    """
    Six CSV tables covering all consumers.

    point_loads.csv        → foundation / frame (concentrated loads)
    wall_loads.csv         → foundation / frame (distributed DL+SL split)
    truss_layout.csv       → drawing / manufacturer (all trusses)
    girder_layout.csv      → drawing / frame (all girders + bearing points)
    drift_zones.csv        → EOR review / truss manufacturer upgrade
    deflection_checks.csv  → truss manufacturer (EI requirements)
    """
    tables: dict[str, str] = {}

    def _csv(rows: list[list]) -> str:
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        return buf.getvalue()

    # 1. point_loads.csv
    tables["point_loads.csv"] = _csv(
        [["id", "x_ft", "y_ft", "z_ft", "source_kind",
          "dead_lbs", "snow_lbs", "unfactored_lbs", "factored_lbs",
          "required_support", "critical"]]
        + [[pl["id"], pl["x_ft"], pl["y_ft"], pl["z_ft"], pl["source_kind"],
            pl["dead_lbs"], pl["snow_lbs"], pl["unfactored_lbs"], pl["factored_lbs"],
            pl["required_support"], pl["critical"]]
           for pl in output.point_loads_to_floors]
    )

    # 2. wall_loads.csv
    tables["wall_loads.csv"] = _csv(
        [["wall_id", "start_x", "start_y", "end_x", "end_y", "length_ft",
          "dead_plf", "snow_plf", "factored_plf",
          "dead_total_lbs", "snow_total_lbs", "continuous_to_foundation"]]
        + [[wl["wall_id"], wl["start_x"], wl["start_y"], wl["end_x"], wl["end_y"],
            wl["wall_length_ft"], wl["dead_plf"], wl["snow_plf"], wl["factored_plf"],
            wl["dead_total_lbs"], wl["snow_total_lbs"], wl["continuous_to_foundation"]]
           for wl in output.bearing_wall_loads]
    )

    # 3. truss_layout.csv
    tables["truss_layout.csv"] = _csv(
        [["id", "kind", "plane_id", "position_ft", "span_ft", "spacing_in",
          "bearing_wall_1", "bearing_wall_2",
          "reaction_dead_lbs", "reaction_snow_lbs", "reaction_factored_lbs",
          "in_drift_zone"]]
        + [[t.id, t.kind, t.plane_id, t.position_ft, t.span_ft, t.spacing_in,
            t.bearing_wall_ids[0] if t.bearing_wall_ids else "",
            t.bearing_wall_ids[1] if len(t.bearing_wall_ids) > 1 else "",
            t.reaction_dead_lbs, t.reaction_snow_lbs, t.reaction_factored_lbs,
            t.in_drift_zone]
           for t in output.trusses]
    )

    # 4. girder_layout.csv
    tables["girder_layout.csv"] = _csv(
        [["id", "kind", "span_ft", "span_dir",
          "start_x", "start_y", "start_z", "end_x", "end_y", "end_z",
          "total_dead_lbs", "total_snow_lbs", "total_factored_lbs",
          "bp1_id", "bp1_x", "bp1_y", "bp1_factored_lbs", "bp1_post",
          "bp2_id", "bp2_x", "bp2_y", "bp2_factored_lbs", "bp2_post",
          "note"]]
        + [[g.id, g.kind, g.span_ft, g.span_direction,
            g.start_pt.x, g.start_pt.y, g.start_pt.z,
            g.end_pt.x,   g.end_pt.y,   g.end_pt.z,
            g.total_dead_load_lbs, g.total_snow_load_lbs, g.total_factored_load_lbs,
            *(([bp.id, bp.location.x, bp.location.y, bp.factored_lbs, bp.post_requirement]
               if (bp := g.bearing_points[0] if g.bearing_points else None) else
               ["", "", "", "", ""])),
            *(([bp2.id, bp2.location.x, bp2.location.y, bp2.factored_lbs, bp2.post_requirement]
               if (bp2 := g.bearing_points[1] if len(g.bearing_points) > 1 else None) else
               ["", "", "", "", ""])),
            g.note]
           for g in output.girders]
    )

    # 5. drift_zones.csv
    tables["drift_zones.csv"] = _csv(
        [["id", "kind", "valley_x", "y_start", "y_end",
          "drift_width_ft", "peak_additional_psf", "avg_additional_psf",
          "affected_truss_count", "note"]]
        + [[d.id, d.kind, d.valley_x, d.y_start, d.y_end,
            d.drift_width_ft, d.peak_additional_psf, d.avg_additional_psf,
            len(d.affected_truss_ids), d.note]
           for d in output.drift_loads]
    )

    # 6. deflection_checks.csv
    tables["deflection_checks.csv"] = _csv(
        [["element_id", "element_type", "span_ft", "service_load_plf",
          "limit_live_in", "limit_total_in",
          "req_EI_live_lb_in2", "req_EI_total_lb_in2", "governing_EI_lb_in2",
          "min_depth_rec_in", "flag"]]
        + [[d.element_id, d.element_type, d.span_ft, d.service_load_plf,
            d.limit_live_in, d.limit_total_in,
            d.req_EI_live_lb_in2, d.req_EI_total_lb_in2, d.governing_EI_lb_in2,
            d.min_depth_rec_in, d.flag]
           for d in output.deflection_checks]
    )

    return tables


def export_to_files(output: RoofDesignOutput, directory: str = ".") -> list[str]:
    """Write JSON + all CSV tables. Returns list of paths written."""
    import os
    os.makedirs(directory, exist_ok=True)
    written: list[str] = []

    jp = os.path.join(directory, "roof_structural_output.json")
    with open(jp, "w") as f:
        json.dump(to_json(output), f, indent=2)
    written.append(jp)

    for name, content in to_csv_tables(output).items():
        p = os.path.join(directory, name)
        with open(p, "w", newline="") as f:
            f.write(content)
        written.append(p)

    return written


# ──────────────────────────────────────────────────────────────────────────────
# Pretty-print Summary
# ──────────────────────────────────────────────────────────────────────────────

def print_summary(output: RoofDesignOutput) -> None:
    L, s = output.loads, output.summary
    print("=" * 65)
    print("ROOF STRUCTURAL DESIGN SUMMARY")
    print("=" * 65)
    print(f"  Location:          {s['city']}, {s['province']}")
    print(f"  Dead load:         {L.dead_psf} psf")
    print(f"  Design snow:       {L.design_snow_psf} psf  "
          f"(Ss={L.ground_snow_kPa} kPa, Cs={L.slope_factor_Cs}, Cw={L.exposure_factor_Cw})")
    print(f"  Strength combo:    {L.strength_combo_psf} psf  (1.25D + 1.5S)")
    print(f"  Truss spacing:     {L.truss_spacing_in}\" o.c.")
    print(f"  Trusses / Girders: {s['truss_count']} / {s['girder_count']}")
    print(f"  Drift zones:       {s['drift_zones']}")
    print(f"  Critical BPs:      {s['critical_bearing_points']}")
    print(f"  Load paths OK/broken: "
          f"{s['load_paths_complete']} / {s['load_paths_incomplete']}")
    df = s["deflection_flags"]
    print(f"  Deflection flags:  "
          f"OK={df['OK_typical']}  "
          f"VERIFY={df['VERIFY_DEFLECTION']}  "
          f"CRITICAL={df['CRITICAL_DEFLECTION']}")

    if output.girders:
        print("\nGIRDERS")
        print("-" * 65)
        for g in output.girders:
            print(f"  {g.id}  [{g.kind}]  span={g.span_ft} ft  "
                  f"D={g.total_dead_load_lbs:,.0f}  "
                  f"S={g.total_snow_load_lbs:,.0f}  "
                  f"factored={g.total_factored_load_lbs:,.0f} lbs")
            print(f"    {g.note}")
            for bp in g.bearing_points:
                print(f"    {bp.id}: "
                      f"({bp.location.x:.1f}, {bp.location.y:.1f}) ft  "
                      f"→  {bp.factored_lbs:,.0f} lbs  |  {bp.post_requirement}")

    if output.drift_loads:
        print("\nDRIFT ZONES (NBCC 4.1.6.5 estimate — EOR to confirm)")
        print("-" * 65)
        for d in output.drift_loads:
            print(f"  {d.id}  x={d.valley_x:.1f} ft  "
                  f"+{d.peak_additional_psf} psf peak  "
                  f"±{d.drift_width_ft} ft wide  "
                  f"({len(d.affected_truss_ids)} trusses)")

    flagged_defl = [d for d in output.deflection_checks if d.flag != "OK_typical"]
    if flagged_defl:
        print("\nDEFLECTION FLAGS")
        print("-" * 65)
        for d in flagged_defl:
            print(f"  {d.element_id:<25}  span={d.span_ft:.0f} ft  "
                  f"govEI={d.governing_EI_lb_in2/1e6:.1f}×10⁶ lb·in²  "
                  f"min_depth={d.min_depth_rec_in:.1f}\"  [{d.flag}]")

    if output.bearing_wall_loads:
        print("\nWALL LOADS → FRAME MODULE")
        print("-" * 65)
        for wl in output.bearing_wall_loads:
            print(f"  {wl['wall_id']:<22}  "
                  f"D={wl['dead_plf']:>5,.0f}  "
                  f"S={wl['snow_plf']:>5,.0f}  "
                  f"factored={wl['factored_plf']:>6,.0f} plf")

    if output.warnings:
        print("\nWARNINGS")
        print("-" * 65)
        for w in output.warnings:
            print(f"  {w}")

    print()
    print("=" * 65)
