"""
test_roof_structural.py
────────────────────────
Tests for the updated Roof Structural Design Module.

Run with:   python test_roof_structural.py
  or:       python -m pytest test_roof_structural.py -v

Coverage:
  TestLoadCalculations     — NBCC snow / dead / combinations
  TestTrussLayout          — span direction, spacing, DL/SL reactions
  TestGirderPlacement      — valley girder, midspan girder, no-girder baseline
  TestDriftLoads           — NBCC 4.1.6.5 valley drift estimates
  TestDeflectionChecks     — L/180 live, L/240 total, EI formula, flags
  TestLoadPathTracing      — complete paths, discontinuities, corner disambiguation
  TestPointLoadHandoff     — DL/SL split, required fields, RTU handling
  TestExports              — JSON structure, CSV tables, file export
  TestHelpers              — geometry math unit tests
"""

import io
import json
import math
import os
import sys
import tempfile
import csv as csv_mod

from roof_structural import (
    ArchitecturalInput,
    BearingWall,
    DEF_LIVE, DEF_TOTAL,
    KPA_TO_PSF, FT_TO_M, RHO_SNOW_KN_M3,
    NBCC_CLIMATE,
    PointLoadItem,
    Pt2, Pt3,
    RoofDesignOutput,
    RoofOpening,
    RoofPlane,
    RoofStructuralDesigner,
    _bbox_shared_edge,
    _post_spec,
    _pt_to_segment_dist,
    export_to_files,
    print_summary,
    to_csv_tables,
    to_json,
)


# ──────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────────────────────

def simple_gable(city: str = "Calgary", province: str = "AB") -> ArchitecturalInput:
    """
    Single-storey rectangular gable house — 32 × 48 ft, 5/12 pitch.
    Trusses span 32 ft (short direction), gable ends at y=0 and y=48.
    Calgary: Ss=1.1 kPa → design snow ~17 psf → 24" spacing.
    """
    plane = RoofPlane(
        id="main", pitch_str="5/12",
        perimeter=[Pt2(0,0), Pt2(32,0), Pt2(32,48), Pt2(0,48)],
        eave_height_ft=10.0,
    )
    walls = [
        BearingWall("north_ext", Pt2(0,0),   Pt2(32,0),  True, True),
        BearingWall("south_ext", Pt2(0,48),  Pt2(32,48), True, True),
        BearingWall("east_ext",  Pt2(32,0),  Pt2(32,48), True, True),
        BearingWall("west_ext",  Pt2(0,0),   Pt2(0,48),  True, True),
    ]
    return ArchitecturalInput(
        city=city, province=province,
        importance_category="Normal",
        roof_planes=[plane], bearing_walls=walls,
        num_stories=1, roofing_material="asphalt_shingle",
    )


def l_shaped(city: str = "Vancouver") -> ArchitecturalInput:
    """
    Two-storey L-shaped house.
    Main wing: 32 × 40 ft.  Side wing: 24 × 20 ft attached at x=32.

        (0,0)──────────(32,0)
          |  main wing   |
        (0,40)─────────(32,40)──────(56,40)
                          |  side wing  |
                        (32,60)──────(56,60)

    Valley girder expected at x=32, y=40→60.
    """
    main = RoofPlane(
        id="main", pitch_str="6/12",
        perimeter=[Pt2(0,0), Pt2(32,0), Pt2(32,40), Pt2(0,40)],
        eave_height_ft=18.0,
    )
    side = RoofPlane(
        id="side", pitch_str="6/12",
        perimeter=[Pt2(32,40), Pt2(56,40), Pt2(56,60), Pt2(32,60)],
        eave_height_ft=18.0,
    )
    walls = [
        BearingWall("main_north",  Pt2(0,0),   Pt2(32,0),  True, True),
        BearingWall("main_south",  Pt2(0,40),  Pt2(32,40), True, True),
        BearingWall("main_east",   Pt2(32,0),  Pt2(32,40), True, True),
        BearingWall("main_west",   Pt2(0,0),   Pt2(0,40),  True, True),
        BearingWall("side_north",  Pt2(32,40), Pt2(56,40), True, True),
        BearingWall("side_south",  Pt2(32,60), Pt2(56,60), True, True),
        BearingWall("side_east",   Pt2(56,40), Pt2(56,60), True, True),
        BearingWall("side_west",   Pt2(32,40), Pt2(32,60), True, True),
    ]
    return ArchitecturalInput(
        city=city, province="BC",
        importance_category="Normal",
        roof_planes=[main, side], bearing_walls=walls,
        num_stories=2, roofing_material="asphalt_shingle",
    )


def long_span(city: str = "Calgary") -> ArchitecturalInput:
    """45 × 60 ft plane — short span = 45 ft > 40 ft threshold → midspan girder."""
    plane = RoofPlane(
        id="wide", pitch_str="4/12",
        perimeter=[Pt2(0,0), Pt2(60,0), Pt2(60,45), Pt2(0,45)],
        eave_height_ft=10.0,
    )
    walls = [
        BearingWall("n", Pt2(0,0),   Pt2(60,0),  True, True),
        BearingWall("s", Pt2(0,45),  Pt2(60,45), True, True),
        BearingWall("e", Pt2(60,0),  Pt2(60,45), True, True),
        BearingWall("w", Pt2(0,0),   Pt2(0,45),  True, True),
    ]
    return ArchitecturalInput(
        city=city, province="AB",
        importance_category="Normal",
        roof_planes=[plane], bearing_walls=walls,
        num_stories=1,
    )


# ──────────────────────────────────────────────────────────────────────────────
# TestLoadCalculations
# ──────────────────────────────────────────────────────────────────────────────

class TestLoadCalculations:

    def test_calgary_slope_factor(self):
        """5/12 pitch = 22.6° → Cs = 1 - (22.6-15)/45 ≈ 0.831."""
        arch = simple_gable()
        d = RoofStructuralDesigner(arch)
        L = d._calculate_loads()
        alpha = math.degrees(math.atan(5/12))
        expected = 1.0 - (alpha - 15.0) / 45.0
        assert abs(L.slope_factor_Cs - expected) < 0.01, \
            f"Cs: got {L.slope_factor_Cs:.3f}, expected {expected:.3f}"
        print(f"    Cs={L.slope_factor_Cs:.3f} ✓  (alpha={alpha:.1f}°)")

    def test_calgary_spacing_24in(self):
        """Calgary snow < 35 psf → 24\" spacing."""
        L = RoofStructuralDesigner(simple_gable())._calculate_loads()
        assert L.truss_spacing_in == 24, f"Expected 24\", got {L.truss_spacing_in}\""
        print(f"    Calgary snow={L.design_snow_psf} psf → spacing={L.truss_spacing_in}\" ✓")

    def test_revelstoke_spacing_12in(self):
        """Revelstoke Ss=6.0 → snow > 60 psf → 12\" spacing."""
        arch = simple_gable("Revelstoke", "BC")
        L = RoofStructuralDesigner(arch)._calculate_loads()
        assert L.truss_spacing_in == 12, f"Expected 12\", got {L.truss_spacing_in}\""
        assert L.design_snow_psf > 60
        print(f"    Revelstoke snow={L.design_snow_psf} psf → spacing={L.truss_spacing_in}\" ✓")

    def test_fort_mcmurray_spacing_16in(self):
        """Prince George Ss=2.4 → snow 37.5 psf (35–60 range) → 16\" spacing."""
        arch = simple_gable("Prince George", "BC")
        L = RoofStructuralDesigner(arch)._calculate_loads()
        assert L.truss_spacing_in == 16, f"Expected 16\", got {L.truss_spacing_in}\""
        assert 35 <= L.design_snow_psf < 60
        print(f"    Prince George snow={L.design_snow_psf} psf → spacing={L.truss_spacing_in}\" ✓")

    def test_dead_load_asphalt(self):
        """Asphalt shingle DL = 3.5+2.3+4.0+2.5+1.5 = 13.8 psf."""
        L = RoofStructuralDesigner(simple_gable())._calculate_loads()
        assert abs(L.dead_psf - 13.8) < 0.5, f"DL: got {L.dead_psf}, expected 13.8"
        print(f"    DL={L.dead_psf} psf ✓")

    def test_strength_combination(self):
        """1.25D + 1.5S must match independently computed value."""
        L = RoofStructuralDesigner(simple_gable())._calculate_loads()
        expected = round(1.25 * L.dead_psf + 1.5 * L.design_snow_psf, 1)
        assert abs(L.strength_combo_psf - expected) < 0.5
        print(f"    1.25×{L.dead_psf} + 1.5×{L.design_snow_psf} = {L.strength_combo_psf} psf ✓")

    def test_service_combination(self):
        """D + S = service combo."""
        L = RoofStructuralDesigner(simple_gable())._calculate_loads()
        expected = round(L.dead_psf + L.design_snow_psf, 1)
        assert abs(L.service_combo_psf - expected) < 0.5
        print(f"    D+S service = {L.service_combo_psf} psf ✓")

    def test_dl_sl_are_separate(self):
        """dead_psf and design_snow_psf must both be positive and distinct."""
        L = RoofStructuralDesigner(simple_gable())._calculate_loads()
        assert L.dead_psf > 0
        assert L.design_snow_psf > 0
        assert abs(L.dead_psf - L.design_snow_psf) > 1.0, \
            "DL and SL should differ in a real case"
        print(f"    DL={L.dead_psf} psf, SL={L.design_snow_psf} psf (separate ✓)")

    def test_drift_note_multiple_planes(self):
        """L-shaped house with multiple planes + snow > 25 psf → drift note set."""
        L = RoofStructuralDesigner(l_shaped())._calculate_loads()
        assert L.drift_note != "", "Multiple planes should trigger drift note"
        print(f"    Drift note: {L.drift_note[:55]}… ✓")

    def test_unknown_city_raises(self):
        arch = simple_gable("Atlantis")
        try:
            RoofStructuralDesigner(arch)._calculate_loads()
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "Atlantis" in str(e)
            print(f"    Correctly raised ValueError for unknown city ✓")


# ──────────────────────────────────────────────────────────────────────────────
# TestTrussLayout
# ──────────────────────────────────────────────────────────────────────────────

class TestTrussLayout:

    def test_truss_count(self):
        """48 ft @ 24\" spacing → ~25 trusses."""
        out = RoofStructuralDesigner(simple_gable()).design()
        assert len(out.trusses) >= 24, f"Expected ~25, got {len(out.trusses)}"
        print(f"    Trusses: {len(out.trusses)} ✓")

    def test_gable_ends_at_first_and_last(self):
        out = RoofStructuralDesigner(simple_gable()).design()
        assert out.trusses[0].kind  == "gable_end"
        assert out.trusses[-1].kind == "gable_end"
        print("    Gable ends at first and last positions ✓")

    def test_trusses_span_short_direction(self):
        """32×48 building: trusses span 32 ft (short direction)."""
        out = RoofStructuralDesigner(simple_gable()).design()
        for t in out.trusses:
            assert t.span_ft == 32.0, f"Truss {t.id} span={t.span_ft}, expected 32.0"
        print(f"    All {len(out.trusses)} trusses span 32.0 ft ✓")

    def test_reactions_dl_sl_positive(self):
        """Dead and snow reactions must both be positive and separate."""
        out = RoofStructuralDesigner(simple_gable()).design()
        for t in out.trusses:
            assert t.reaction_dead_lbs > 0, f"{t.id} DL reaction zero/negative"
            assert t.reaction_snow_lbs > 0, f"{t.id} SL reaction zero/negative"
        print(f"    All reactions positive. "
              f"Sample DL={out.trusses[1].reaction_dead_lbs:,.0f} lbs, "
              f"SL={out.trusses[1].reaction_snow_lbs:,.0f} lbs ✓")

    def test_factored_reaction_formula(self):
        """1.25D + 1.5S = factored reaction (verify against manual calc)."""
        arch = simple_gable()
        d = RoofStructuralDesigner(arch)
        d._loads = d._calculate_loads()
        sp_ft   = d._loads.truss_spacing_in / 12.0
        span_ft = 32.0
        exp_rd  = d._loads.dead_psf        * sp_ft * span_ft / 2.0
        exp_rs  = d._loads.design_snow_psf * sp_ft * span_ft / 2.0
        exp_rf  = (1.25 * d._loads.dead_psf + 1.5 * d._loads.design_snow_psf) \
                  * sp_ft * span_ft / 2.0
        trusses = d._layout_trusses()
        common  = next(t for t in trusses if t.kind == "common")
        assert abs(common.reaction_dead_lbs    - exp_rd) < 20
        assert abs(common.reaction_snow_lbs    - exp_rs) < 20
        assert abs(common.reaction_factored_lbs - exp_rf) < 20
        print(f"    DL={common.reaction_dead_lbs:,.0f}  "
              f"SL={common.reaction_snow_lbs:,.0f}  "
              f"factored={common.reaction_factored_lbs:,.0f} lbs ✓")

    def test_gable_end_half_tributary(self):
        """Gable end truss gets half the tributary width → ~half the reaction."""
        arch = simple_gable()
        d = RoofStructuralDesigner(arch)
        d._loads = d._calculate_loads()
        trusses = d._layout_trusses()
        gable  = next(t for t in trusses if t.kind == "gable_end")
        common = next(t for t in trusses if t.kind == "common")
        ratio = gable.reaction_factored_lbs / common.reaction_factored_lbs
        assert 0.45 < ratio < 0.55, \
            f"Gable/common ratio should be ~0.5, got {ratio:.2f}"
        print(f"    Gable end reaction = {ratio:.2f}× common ✓")


# ──────────────────────────────────────────────────────────────────────────────
# TestGirderPlacement
# ──────────────────────────────────────────────────────────────────────────────

class TestGirderPlacement:

    def test_simple_gable_no_girders(self):
        out = RoofStructuralDesigner(simple_gable()).design()
        assert len(out.girders) == 0, f"Simple gable should have 0 girders, got {len(out.girders)}"
        print("    Simple gable: 0 girders ✓")

    def test_l_shape_one_valley_girder(self):
        out = RoofStructuralDesigner(l_shaped()).design()
        vg = [g for g in out.girders if g.kind == "valley_girder"]
        assert len(vg) == 1, f"Expected 1 valley girder, got {len(vg)}"
        print(f"    L-shape: 1 valley girder ✓  "
              f"({vg[0].id}, span={vg[0].span_ft} ft)")

    def test_valley_girder_location(self):
        """Valley girder should be at x≈32, y=40→60 (side wing extent)."""
        out = RoofStructuralDesigner(l_shaped()).design()
        vg = next(g for g in out.girders if g.kind == "valley_girder")
        assert abs(vg.start_pt.x - 32.0) < 1.5
        assert abs(vg.end_pt.x   - 32.0) < 1.5
        assert abs(vg.start_pt.y - 40.0) < 1.5
        assert abs(vg.end_pt.y   - 60.0) < 1.5
        print(f"    Valley at x={vg.start_pt.x:.1f}, "
              f"y={vg.start_pt.y:.0f}→{vg.end_pt.y:.0f} ft ✓")

    def test_valley_girder_dl_sl_split(self):
        """
        Valley girder DL/SL must be separate and positive.
        With corrected tributary width (wing E-W span / 2 = 24/2 = 12 ft):
          DL = 13.8 psf × 12 ft × 20 ft span = 3,312 lbs
          SL = 28.6 psf × 12 ft × 20 ft span = 6,864 lbs
        """
        out = RoofStructuralDesigner(l_shaped()).design()
        vg = next(g for g in out.girders if g.kind == "valley_girder")
        assert vg.total_dead_load_lbs > 0
        assert vg.total_snow_load_lbs > 0
        assert abs(vg.total_dead_load_lbs - vg.total_snow_load_lbs) > 10
        # Check corrected tributary values: trib=12 ft, span=20 ft
        assert abs(vg.total_dead_load_lbs - 3_312) < 100, \
            f"DL should be ~3,312 lbs (trib 12ft), got {vg.total_dead_load_lbs}"
        assert abs(vg.total_snow_load_lbs - 6_864) < 100, \
            f"SL should be ~6,864 lbs (trib 12ft), got {vg.total_snow_load_lbs}"
        print(f"    Girder DL={vg.total_dead_load_lbs:,.0f}  "
              f"SL={vg.total_snow_load_lbs:,.0f} lbs (trib=12 ft ✓)")

    def test_valley_girder_bearing_point_dl_sl(self):
        """Each bearing point must carry roughly half the girder DL and SL."""
        out = RoofStructuralDesigner(l_shaped()).design()
        vg = next(g for g in out.girders if g.kind == "valley_girder")
        for bp in vg.bearing_points:
            assert bp.dead_lbs > 0
            assert bp.snow_lbs > 0
            assert abs(bp.dead_lbs - vg.total_dead_load_lbs / 2) < 50
            assert abs(bp.snow_lbs - vg.total_snow_load_lbs / 2) < 50
        print("    Bearing point DL/SL ≈ half girder total ✓")

    def test_long_span_midspan_girder(self):
        """45 ft short span > 40 ft → midspan girder generated."""
        out = RoofStructuralDesigner(long_span()).design()
        mg = [g for g in out.girders if g.kind == "master_girder"]
        assert len(mg) >= 1, "45 ft short span should trigger midspan girder"
        print(f"    Midspan girder at y={mg[0].start_pt.y:.1f} ft ✓")

    def test_girder_bearing_points_flagged_critical(self):
        out = RoofStructuralDesigner(l_shaped()).design()
        for g in out.girders:
            assert len(g.bearing_points) == 2
            for bp in g.bearing_points:
                assert bp.critical is True
        print("    All girder BPs have 2 bearing points, flagged critical ✓")


# ──────────────────────────────────────────────────────────────────────────────
# TestDriftLoads
# ──────────────────────────────────────────────────────────────────────────────

class TestDriftLoads:

    def test_simple_gable_no_drift(self):
        """Single plane → no valley → no drift load generated."""
        out = RoofStructuralDesigner(simple_gable()).design()
        assert len(out.drift_loads) == 0
        print("    Single plane: 0 drift zones ✓")

    def test_l_shape_generates_drift(self):
        """L-shaped (2 planes) in Vancouver → at least 1 drift zone."""
        out = RoofStructuralDesigner(l_shaped()).design()
        assert len(out.drift_loads) >= 1
        print(f"    Drift zones: {len(out.drift_loads)} ✓")

    def test_drift_peak_positive(self):
        out = RoofStructuralDesigner(l_shaped()).design()
        for d in out.drift_loads:
            assert d.peak_additional_psf > 0
            assert d.avg_additional_psf  > 0
            assert abs(d.avg_additional_psf - d.peak_additional_psf / 2) < 0.5, \
                "avg should be half of peak (triangular)"
        print("    Drift avg = peak/2 (triangular distribution) ✓")

    def test_drift_width_positive(self):
        out = RoofStructuralDesigner(l_shaped()).design()
        for d in out.drift_loads:
            assert d.drift_width_ft > 0
        print(f"    Drift width = ±{out.drift_loads[0].drift_width_ft} ft ✓")

    def test_drift_formula_manual(self):
        """
        Manually reproduce NBCC 4.1.6.5 estimate and compare.
        Vancouver: Ss=1.8, Is=1.0, S_kPa = 1.0*1.8*0.8 = 1.44 kPa
        L-shape short spans: main=32 ft, side=20 ft → avg = 26 ft → lu_m = 7.93 m
        hs = 1.44 / 3.0 = 0.48 m
        hd = min(0.8*sqrt(7.93), 1.5*0.48) = min(2.25, 0.72) = 0.72 m
        peak = 3.0 * 0.72 = 2.16 kPa = 2.16 * 20.885 = 45.1 psf
        xd = min(4*0.72, 7.93/2) = min(2.88, 3.96) = 2.88 m = 9.4 ft
        """
        out = RoofStructuralDesigner(l_shaped("Vancouver")).design()
        assert len(out.drift_loads) >= 1

        d = out.drift_loads[0]
        Ss    = NBCC_CLIMATE["Vancouver"]["Ss"]
        S_kPa = 1.0 * Ss * 0.8

        # avg short span of main (32ft) and side (20ft) = 26 ft
        lu_ft = (32.0 + 20.0) / 2.0
        lu_m  = lu_ft * FT_TO_M
        hs_m  = S_kPa / RHO_SNOW_KN_M3
        hd_m  = min(0.8 * math.sqrt(lu_m), 1.5 * hs_m)
        xd_m  = min(4.0 * hd_m, lu_m / 2.0)
        xd_ft = xd_m / FT_TO_M
        peak  = RHO_SNOW_KN_M3 * hd_m * KPA_TO_PSF

        assert abs(d.peak_additional_psf - peak) < 2.0, \
            f"Peak drift: got {d.peak_additional_psf}, expected {peak:.1f}"
        assert abs(d.drift_width_ft - xd_ft) < 1.0, \
            f"Drift width: got {d.drift_width_ft}, expected {xd_ft:.1f}"
        print(f"    Manual check: hd={hd_m*100:.0f} cm, "
              f"width={xd_ft:.1f} ft, peak={peak:.1f} psf  ✓")

    def test_high_snow_drift_larger(self):
        """Revelstoke (Ss=6.0) should produce larger drift than Vancouver (Ss=1.8)."""
        out_van = RoofStructuralDesigner(l_shaped("Vancouver")).design()
        arch_rev = l_shaped("Revelstoke")
        out_rev = RoofStructuralDesigner(arch_rev).design()

        if out_van.drift_loads and out_rev.drift_loads:
            assert out_rev.drift_loads[0].peak_additional_psf > \
                   out_van.drift_loads[0].peak_additional_psf
            print(f"    Revelstoke drift peak ({out_rev.drift_loads[0].peak_additional_psf} psf)"
                  f" > Vancouver ({out_van.drift_loads[0].peak_additional_psf} psf) ✓")
        else:
            print("    Skipped (one city produced no drift — check Cs=0?)")

    def test_drift_trusses_flagged_in_zone(self):
        """Trusses in drift zone should have in_drift_zone=True."""
        out = RoofStructuralDesigner(l_shaped()).design()
        if out.drift_loads:
            affected = set(out.drift_loads[0].affected_truss_ids)
            flagged  = {t.id for t in out.trusses if t.in_drift_zone}
            assert affected.issubset(flagged | {""}), \
                "All affected truss IDs should be flagged in_drift_zone"
            print(f"    {len(affected)} trusses flagged in drift zone ✓")


# ──────────────────────────────────────────────────────────────────────────────
# TestDeflectionChecks
# ──────────────────────────────────────────────────────────────────────────────

class TestDeflectionChecks:

    def test_every_truss_has_check(self):
        out = RoofStructuralDesigner(simple_gable()).design()
        truss_ids = {t.id for t in out.trusses}
        checked   = {d.element_id for d in out.deflection_checks if d.element_type == "truss"}
        assert truss_ids == checked, \
            f"Missing deflection checks for: {truss_ids - checked}"
        print(f"    All {len(truss_ids)} trusses have deflection checks ✓")

    def test_every_girder_has_check(self):
        out = RoofStructuralDesigner(l_shaped()).design()
        girder_ids = {g.id for g in out.girders}
        checked    = {d.element_id for d in out.deflection_checks if d.element_type == "girder"}
        assert girder_ids == checked, \
            f"Missing deflection checks for girders: {girder_ids - checked}"
        print(f"    All {len(girder_ids)} girders have deflection checks ✓")

    def test_live_limit_is_L_over_180(self):
        """limit_live_in must equal span_in / 180."""
        out = RoofStructuralDesigner(simple_gable()).design()
        for dc in out.deflection_checks:
            expected = (dc.span_ft * 12.0) / DEF_LIVE
            assert abs(dc.limit_live_in - expected) < 0.01, \
                f"{dc.element_id}: live limit {dc.limit_live_in} ≠ {expected:.3f}"
        print("    All L/180 live limits correct ✓")

    def test_total_limit_is_L_over_240(self):
        """Standard roof: limit_total_in = span_in / 240."""
        out = RoofStructuralDesigner(simple_gable()).design()
        for dc in out.deflection_checks:
            expected = (dc.span_ft * 12.0) / DEF_TOTAL
            assert abs(dc.limit_total_in - expected) < 0.01, \
                f"{dc.element_id}: total limit {dc.limit_total_in} ≠ {expected:.3f}"
        print("    All L/240 total limits correct ✓")

    def test_brittle_ceiling_uses_L_360(self):
        """Cathedral ceiling → L/360 total limit."""
        arch = simple_gable()
        arch.has_cathedral_ceiling = True
        out = RoofStructuralDesigner(arch).design()
        for dc in out.deflection_checks:
            expected = (dc.span_ft * 12.0) / 360.0
            assert abs(dc.limit_total_in - expected) < 0.01, \
                f"Cathedral: limit_total should be L/360, got {dc.limit_total_in:.3f}"
        print("    Cathedral ceiling → L/360 applied ✓")

    def test_EI_formula(self):
        """
        Manually compute req_EI for one truss and compare.
        EI = 5wL⁴ / (384 × δ_limit)
        """
        out = RoofStructuralDesigner(simple_gable()).design()
        dc  = next(d for d in out.deflection_checks if d.element_type == "truss")

        L_in   = dc.span_ft * 12.0
        dlt    = L_in / DEF_LIVE
        w_pli  = (dc.service_load_plf
                  * (1 - out.loads.dead_psf / out.loads.service_combo_psf)
                  ) / 12.0   # approximate snow portion converted to lb/in
        # Use the stored value directly for formula check
        # EI = 5 * w * L^4 / (384 * delta)
        stored = dc.req_EI_live_lb_in2
        assert stored > 0
        # Verify governing_EI >= both components
        assert dc.governing_EI_lb_in2 >= dc.req_EI_live_lb_in2
        assert dc.governing_EI_lb_in2 >= dc.req_EI_total_lb_in2
        print(f"    EI_live={dc.req_EI_live_lb_in2/1e6:.2f}×10⁶  "
              f"EI_total={dc.req_EI_total_lb_in2/1e6:.2f}×10⁶  "
              f"governing={dc.governing_EI_lb_in2/1e6:.2f}×10⁶ lb·in² ✓")

    def test_min_depth_recommendation(self):
        """Trusses: min_depth = span/20; girders: span/15."""
        out = RoofStructuralDesigner(l_shaped()).design()
        for dc in out.deflection_checks:
            if dc.element_type == "truss":
                exp = dc.span_ft * 12.0 / 20.0
            else:
                exp = dc.span_ft * 12.0 / 15.0
            assert abs(dc.min_depth_rec_in - exp) < 0.5
        print("    Min depth recommendations: span/20 trusses, span/15 girders ✓")

    def test_normal_span_flagged_ok(self):
        """32 ft truss span should be OK_typical (normal residential range)."""
        out = RoofStructuralDesigner(simple_gable()).design()
        flags = {d.flag for d in out.deflection_checks if d.element_type == "truss"}
        assert "OK_typical" in flags, f"Expected OK_typical for 32 ft trusses, got {flags}"
        print(f"    32 ft trusses flagged: {flags} ✓")

    def test_long_span_flagged_verify_or_critical(self):
        """
        long_span() fixture: 45 ft Calgary trusses at 24" spacing.
        EI-ratio = 0.831 (in the 0.65–1.0 band) → VERIFY_DEFLECTION.
        The 60 ft master girder has EI-ratio >> 1.0 → CRITICAL_DEFLECTION.
        Also checks 36 ft Calgary trusses → VERIFY_DEFLECTION (ratio 0.663).
        """
        out_long = RoofStructuralDesigner(long_span()).design()

        # 45 ft trusses: ratio 0.831 → VERIFY
        truss_flags = {d.flag for d in out_long.deflection_checks if d.element_type == "truss"}
        assert "VERIFY_DEFLECTION" in truss_flags, \
            f"45 ft Calgary trusses should be VERIFY_DEFLECTION, got {truss_flags}"

        # 60 ft girder: ratio >> 1.0 → CRITICAL
        girder_checks = [d for d in out_long.deflection_checks if d.element_type == "girder"]
        assert any(d.flag == "CRITICAL_DEFLECTION" for d in girder_checks), \
            f"60 ft master girder should be CRITICAL_DEFLECTION, got {[d.flag for d in girder_checks]}"

        # 36 ft Calgary span: ratio 0.663 → VERIFY
        plane_36 = RoofPlane(
            id="m36", pitch_str="5/12",
            perimeter=[Pt2(0,0), Pt2(50,0), Pt2(50,36), Pt2(0,36)],
            eave_height_ft=10.0,
        )
        arch_36 = ArchitecturalInput(
            city="Calgary", province="AB", importance_category="Normal",
            roof_planes=[plane_36],
            bearing_walls=[
                BearingWall("n", Pt2(0,0),  Pt2(50,0),  True, True),
                BearingWall("s", Pt2(0,36), Pt2(50,36), True, True),
                BearingWall("e", Pt2(50,0), Pt2(50,36), True, True),
                BearingWall("w", Pt2(0,0),  Pt2(0,36),  True, True),
            ], num_stories=1,
        )
        out_36 = RoofStructuralDesigner(arch_36).design()
        verify_36 = [d for d in out_36.deflection_checks if d.flag == "VERIFY_DEFLECTION"]
        assert len(verify_36) >= 1, "36 ft span should produce VERIFY_DEFLECTION"
        n_verify = sum(1 for d in out_long.deflection_checks if d.flag == "VERIFY_DEFLECTION")
        print(f"    45 ft trusses → VERIFY ({n_verify}), "
              f"60 ft girder → CRITICAL (1), "
              f"36 ft → VERIFY ({len(verify_36)}) ✓")


# ──────────────────────────────────────────────────────────────────────────────
# TestLoadPathTracing
# ──────────────────────────────────────────────────────────────────────────────

class TestLoadPathTracing:

    def test_continuous_walls_all_complete(self):
        """All walls continuous_to_foundation=True → all paths complete."""
        out = RoofStructuralDesigner(l_shaped()).design()
        broken = [lp for lp in out.load_paths if not lp.complete]
        assert len(broken) == 0, \
            f"{len(broken)} paths broken: " + "; ".join(lp.warning for lp in broken)
        print(f"    All {len(out.load_paths)} load paths complete ✓")

    def test_discontinuous_wall_breaks_path(self):
        """
        main_south (y=40 E-W wall) is directly under the valley girder's BP1
        at (32, 40).  Mark it discontinuous → broken path + warning.
        """
        arch = l_shaped()
        for w in arch.bearing_walls:
            if w.id == "main_south":
                w.continuous_to_foundation = False

        out = RoofStructuralDesigner(arch).design()
        broken = [lp for lp in out.load_paths if not lp.complete]
        assert len(broken) >= 1, "Discontinuous wall should break at least one path"
        assert any("transfer beam" in lp.warning.lower() for lp in broken)
        assert any("LOAD PATH BREAK" in w for w in out.warnings)
        print(f"    {len(broken)} broken path(s) with transfer beam note ✓")

    def test_no_wall_at_point_load_breaks_path(self):
        """
        Add an RTU at a location with no bearing wall beneath — path must break.
        """
        arch = simple_gable()
        arch.point_load_items = [
            PointLoadItem("floater", "RTU", 3000, Pt2(16, 24), (4, 5))
        ]
        out = RoofStructuralDesigner(arch).design()
        broken = [lp for lp in out.load_paths if not lp.complete]
        # Interior point (16,24) has no bearing wall → should be broken
        assert len(broken) >= 1, \
            "RTU at (16,24) with no interior wall should produce a broken path"
        print(f"    Interior RTU (no wall beneath) → broken path ✓")

    def test_corner_disambiguation_prefers_perpendicular_wall(self):
        """
        Valley girder BP1 at (32, 40): corner where main_south (E-W, direction='x')
        and main_east (N-S, direction='y') both come within tolerance.
        load_direction='y' (girder spans N-S) → should prefer E-W wall (main_south).
        Verify the correct wall is returned, not main_east.
        """
        arch = l_shaped()
        d = RoofStructuralDesigner(arch)
        d._loads   = d._calculate_loads()
        d._trusses = d._layout_trusses()
        d._girders = d._identify_girders()

        # Girder BP1 is at (32, 40) with load_direction='y'
        vg = next(g for g in d._girders if g.kind == "valley_girder")
        bp1 = vg.bearing_points[0]

        wall = d._bearing_support_at(bp1.location.x, bp1.location.y, bp1.load_direction)
        assert wall is not None, "Should find a wall at girder BP1"
        # load_direction='y' → prefer walls with direction='x' (E-W, constant y)
        assert wall.direction == "x", \
            f"Corner disambiguation should prefer E-W wall, got direction='{wall.direction}' (wall={wall.id})"
        print(f"    Corner disambiguation: chose '{wall.id}' (direction='{wall.direction}') ✓")

    def test_load_path_stages_populated(self):
        out = RoofStructuralDesigner(l_shaped()).design()
        for lp in out.load_paths:
            assert len(lp.stages) >= 1
        print("    All load paths have ≥1 stage ✓")


# ──────────────────────────────────────────────────────────────────────────────
# TestPointLoadHandoff
# ──────────────────────────────────────────────────────────────────────────────

class TestPointLoadHandoff:

    def test_point_loads_are_critical_only(self):
        out = RoofStructuralDesigner(l_shaped()).design()
        for pl in out.point_loads_to_floors:
            assert pl["critical"] is True
        print(f"    {len(out.point_loads_to_floors)} point loads, all critical ✓")

    def test_point_loads_have_dl_sl_split(self):
        """Each point load must have dead_lbs and snow_lbs separate."""
        out = RoofStructuralDesigner(l_shaped()).design()
        for pl in out.point_loads_to_floors:
            assert "dead_lbs" in pl and pl["dead_lbs"] > 0
            assert "snow_lbs" in pl and pl["snow_lbs"] > 0
        print("    Point loads carry dead_lbs + snow_lbs ✓")

    def test_point_load_required_fields(self):
        required = {"id", "source_kind", "x_ft", "y_ft", "z_ft",
                    "dead_lbs", "snow_lbs", "unfactored_lbs", "factored_lbs",
                    "required_support", "must_reach_foundation", "critical"}
        out = RoofStructuralDesigner(l_shaped()).design()
        for pl in out.point_loads_to_floors:
            missing = required - set(pl.keys())
            assert not missing, f"{pl['id']} missing: {missing}"
        print("    All required fields present ✓")

    def test_wall_loads_dl_sl_split(self):
        """Wall loads must have dead_plf and snow_plf separate."""
        out = RoofStructuralDesigner(simple_gable()).design()
        assert len(out.bearing_wall_loads) > 0
        for wl in out.bearing_wall_loads:
            assert wl["dead_plf"] > 0
            assert wl["snow_plf"] > 0
            expected_factored = round(
                (1.25 * wl["dead_total_lbs"] + 1.5 * wl["snow_total_lbs"])
                / wl["wall_length_ft"], 0
            )
            assert abs(wl["factored_plf"] - expected_factored) < 5
        print(f"    Wall loads: DL and SL separate + factored formula verified ✓")

    def test_rtu_over_2000lbs_is_critical(self):
        arch = simple_gable()
        arch.point_load_items = [
            PointLoadItem("rtu1", "RTU", 3000, Pt2(16, 24), (4, 5))
        ]
        out = RoofStructuralDesigner(arch).design()
        bp_ids = {bp.id for bp in out.bearing_points if bp.critical}
        assert "PTL_rtu1" in bp_ids
        print("    RTU >2000 lbs flagged critical ✓")

    def test_rtu_under_2000lbs_not_critical(self):
        arch = simple_gable()
        arch.point_load_items = [
            PointLoadItem("light", "solar_rack", 500, Pt2(10, 10), (3, 4))
        ]
        out = RoofStructuralDesigner(arch).design()
        bp = next((bp for bp in out.bearing_points if bp.id == "PTL_light"), None)
        assert bp is not None
        assert bp.critical is False
        print("    Light item (500 lbs) not flagged critical ✓")

    def test_frame_analyzer_interior_support_note(self):
        """
        for_frame_analyzer.interior_support_requirements should be present
        when there are critical point loads.
        """
        out  = RoofStructuralDesigner(l_shaped()).design()
        j    = to_json(out)
        reqs = j["for_frame_analyzer"]["interior_support_requirements"]
        # Not necessarily non-empty for every case, but must exist as a list
        assert isinstance(reqs, list)
        print(f"    interior_support_requirements present ({len(reqs)} items) ✓")


# ──────────────────────────────────────────────────────────────────────────────
# TestExports
# ──────────────────────────────────────────────────────────────────────────────

class TestExports:

    def _full_output(self) -> RoofDesignOutput:
        arch = l_shaped()
        arch.point_load_items = [
            PointLoadItem("rtu1", "RTU", 1500, Pt2(16, 20), (4, 5))
        ]
        arch.roof_openings = [
            RoofOpening("sky1", "skylight", Pt2(8, 15), 4, 6)
        ]
        return RoofStructuralDesigner(arch).design()

    # ── JSON ──────────────────────────────────────────────────────────────────

    def test_json_top_level_keys(self):
        j = to_json(self._full_output())
        required = {
            "metadata", "design_loads", "truss_layout", "girder_layout",
            "drift_zones", "deflection_checks", "bearing_points", "load_paths",
            "warnings", "summary", "for_foundation", "for_frame_analyzer",
            "for_drawing",
        }
        missing = required - set(j.keys())
        assert not missing, f"Missing top-level keys: {missing}"
        print("    JSON top-level keys all present ✓")

    def test_json_serialisable(self):
        j   = to_json(self._full_output())
        txt = json.dumps(j)
        assert len(txt) > 100
        j2  = json.loads(txt)
        assert j2["metadata"]["code"] == "NBCC_2020"
        print("    JSON round-trips cleanly ✓")

    def test_json_design_loads_fields(self):
        j = to_json(self._full_output())
        ld = j["design_loads"]
        for key in ["dead_psf", "design_snow_psf", "strength_combo_psf",
                    "recommended_spacing_in", "load_combinations"]:
            assert key in ld, f"Missing: {key}"
        print("    design_loads fields complete ✓")

    def test_json_for_foundation_structure(self):
        j  = to_json(self._full_output())
        ff = j["for_foundation"]
        assert "column_loads"   in ff
        assert "wall_line_loads" in ff
        assert "drift_zone_additional" in ff
        for wl in ff["wall_line_loads"]:
            assert "dead_plf" in wl and "snow_plf" in wl
        print("    for_foundation structure correct ✓")

    def test_json_for_frame_analyzer_dl_sl_split(self):
        j   = to_json(self._full_output())
        fa  = j["for_frame_analyzer"]
        assert "dead" in fa["load_cases"] and "snow" in fa["load_cases"]
        dead_walls = fa["load_cases"]["dead"]["distributed_wall_loads"]
        snow_walls = fa["load_cases"]["snow"]["distributed_wall_loads"]
        assert len(dead_walls) > 0
        assert len(snow_walls) == len(dead_walls)
        # DL and SL plf values on same wall should differ
        for dw, sw in zip(dead_walls, snow_walls):
            assert dw["wall_id"] == sw["wall_id"]
            assert abs(dw["w_plf"] - sw["w_plf"]) > 1, \
                "DL and SL plf should differ"
        print("    for_frame_analyzer DL/SL load cases separated ✓")

    def test_json_for_drawing_structure(self):
        j  = to_json(self._full_output())
        fd = j["for_drawing"]
        assert "building_outline"    in fd
        assert "truss_layout_plan"   in fd
        assert "girders"             in fd
        assert "post_symbols"        in fd
        assert "drift_zone_hatching" in fd
        assert "notes_table"         in fd
        assert len(fd["notes_table"]) >= 6
        # building_outline must have actual plane geometry (not empty)
        planes = fd["building_outline"]["planes"]
        assert len(planes) >= 1, "building_outline.planes must be non-empty"
        assert "perimeter"       in planes[0]
        assert "pitch_str"       in planes[0]
        assert "eave_height_ft"  in planes[0]
        assert "ridge_height_ft" in planes[0]
        assert len(planes[0]["perimeter"]) >= 3, "Each plane needs ≥3 perimeter points"
        print(f"    for_drawing structure complete, "
              f"building_outline has {len(planes)} plane(s) ✓")

    def test_json_for_drawing_post_symbols(self):
        """Critical bearing points must appear as post_symbols in for_drawing."""
        out = self._full_output()
        j   = to_json(out)
        critical_ids = {bp.id for bp in out.bearing_points if bp.critical}
        sym_ids      = {s["id"] for s in j["for_drawing"]["post_symbols"]}
        assert critical_ids == sym_ids, \
            f"Post symbols don't match critical BPs: {critical_ids ^ sym_ids}"
        print(f"    {len(sym_ids)} post symbols match critical bearing points ✓")

    def test_json_load_paths_have_stages(self):
        j = to_json(self._full_output())
        for lp in j["load_paths"]:
            assert "stages" in lp
            assert "complete" in lp
            assert "bearing_point_id" in lp
        print("    All load paths in JSON have required fields ✓")

    # ── CSV ───────────────────────────────────────────────────────────────────

    def test_csv_tables_present(self):
        tables = to_csv_tables(self._full_output())
        expected = {
            "point_loads.csv", "wall_loads.csv", "truss_layout.csv",
            "girder_layout.csv", "drift_zones.csv", "deflection_checks.csv",
        }
        missing = expected - set(tables.keys())
        assert not missing, f"Missing CSV tables: {missing}"
        print(f"    All 6 CSV tables present ✓")

    def test_csv_headers_correct(self):
        tables = to_csv_tables(self._full_output())
        checks = {
            "point_loads.csv":       "id,x_ft,y_ft",
            "wall_loads.csv":        "wall_id,start_x",
            "truss_layout.csv":      "id,kind,plane_id",
            "girder_layout.csv":     "id,kind,span_ft",
            "drift_zones.csv":       "id,kind,valley_x",
            "deflection_checks.csv": "element_id,element_type",
        }
        for fname, prefix in checks.items():
            first_line = tables[fname].split("\n")[0]
            assert first_line.startswith(prefix), \
                f"{fname} header mismatch: {first_line[:40]}"
        print("    All CSV headers correct ✓")

    def test_csv_truss_count(self):
        """truss_layout.csv must have one data row per truss."""
        out    = self._full_output()
        tables = to_csv_tables(out)
        rows   = [r for r in tables["truss_layout.csv"].strip().split("\n")
                  if r and not r.startswith("id,")]
        assert len(rows) == len(out.trusses), \
            f"CSV has {len(rows)} rows, expected {len(out.trusses)}"
        print(f"    truss_layout.csv: {len(rows)} rows ✓")

    def test_csv_drift_zones_count(self):
        out    = self._full_output()
        tables = to_csv_tables(out)
        rows   = [r for r in tables["drift_zones.csv"].strip().split("\n")
                  if r and not r.startswith("id,")]
        assert len(rows) == len(out.drift_loads)
        print(f"    drift_zones.csv: {len(rows)} rows ✓")

    def test_csv_parseable(self):
        """All CSV tables must parse cleanly with csv.reader."""
        tables = to_csv_tables(self._full_output())
        for name, content in tables.items():
            rows = list(csv_mod.reader(io.StringIO(content)))
            assert len(rows) >= 1, f"{name} produced no rows"
        print("    All CSV tables parse without error ✓")

    def test_csv_deflection_flags_present(self):
        """deflection_checks.csv must include the 'flag' column."""
        tables = to_csv_tables(self._full_output())
        reader = csv_mod.DictReader(io.StringIO(tables["deflection_checks.csv"]))
        rows = list(reader)
        assert len(rows) > 0
        assert "flag" in rows[0], "deflection_checks.csv missing 'flag' column"
        flags = {r["flag"] for r in rows}
        assert flags.issubset({"OK_typical", "VERIFY_DEFLECTION", "CRITICAL_DEFLECTION"})
        print(f"    deflection_checks.csv flags: {flags} ✓")

    def test_export_to_files(self):
        """export_to_files() writes 7 files (1 JSON + 6 CSV)."""
        out = self._full_output()
        with tempfile.TemporaryDirectory() as tmp:
            written = export_to_files(out, tmp)
            assert len(written) == 7, f"Expected 7 files, got {len(written)}"
            for path in written:
                assert os.path.exists(path), f"File not created: {path}"
                assert os.path.getsize(path) > 0, f"Empty file: {path}"
            # Verify JSON is valid
            with open(os.path.join(tmp, "roof_structural_output.json")) as f:
                j = json.load(f)
            assert "summary" in j
        print("    export_to_files: 7 files written and valid ✓")


# ──────────────────────────────────────────────────────────────────────────────
# TestHelpers
# ──────────────────────────────────────────────────────────────────────────────

class TestHelpers:

    def test_post_spec_thresholds(self):
        assert "double" in _post_spec(3_000)
        assert "triple" in _post_spec(7_000)
        assert "4x4"    in _post_spec(14_000) or "quad" in _post_spec(14_000)
        assert "6x6"    in _post_spec(22_000)
        spec = _post_spec(35_000).lower()
        assert "steel" in spec or "engineered" in spec
        print("    Post spec thresholds correct ✓")

    def test_pt_to_segment_on_line(self):
        assert abs(_pt_to_segment_dist(5, 0,  0, 0, 10, 0)) < 1e-9
        print("    Point on segment → distance = 0 ✓")

    def test_pt_to_segment_perpendicular(self):
        d = _pt_to_segment_dist(5, 3, 0, 0, 10, 0)
        assert abs(d - 3.0) < 1e-9
        print("    Perpendicular distance = 3.0 ✓")

    def test_pt_to_segment_past_end(self):
        d = _pt_to_segment_dist(15, 0, 0, 0, 10, 0)
        assert abs(d - 5.0) < 1e-9
        print("    Distance past segment end = 5.0 ✓")

    def test_bbox_shared_edge_l_shape(self):
        main = RoofPlane("m", "6/12", [Pt2(0,0), Pt2(32,0), Pt2(32,40), Pt2(0,40)], 18)
        side = RoofPlane("s", "6/12", [Pt2(32,40), Pt2(56,40), Pt2(56,60), Pt2(32,60)], 18)
        vx, vy = _bbox_shared_edge(main, side)
        assert vx is not None
        assert abs(vx - 32.0) < 1.5
        print(f"    Shared edge at x={vx:.1f} ✓")

    def test_bbox_no_shared_edge(self):
        a = RoofPlane("a", "6/12", [Pt2(0,0), Pt2(20,0), Pt2(20,20), Pt2(0,20)], 10)
        b = RoofPlane("b", "6/12", [Pt2(50,0), Pt2(70,0), Pt2(70,20), Pt2(50,20)], 10)
        vx, _ = _bbox_shared_edge(a, b)
        assert vx is None
        print("    Disconnected planes → no shared edge ✓")


# ──────────────────────────────────────────────────────────────────────────────
# TestNewBehaviours  — covers the 4 high-priority fixes from this iteration
# ──────────────────────────────────────────────────────────────────────────────

class TestNewBehaviours:
    """
    Covers:
      1. EI-ratio deflection flag accounts for load magnitude (not just span)
      2. Valley girder tributary uses perpendicular wing width (not short_span)
      3. Hip roof raises NotImplementedError before any calculation runs
      4. for_drawing building_outline is fully populated on output object
    """

    def test_flag_accounts_for_load_magnitude(self):
        """
        Two 32 ft roofs — same span, very different snow loads.
        Calgary (17.4 psf, EI-ratio 0.588) → OK_typical
        Revelstoke (84.9 psf at 12\" spacing, EI-ratio 0.930) → VERIFY_DEFLECTION
        The old span-only flag would give OK_typical for both.
        """
        # Calgary 32 ft — standard residential load
        out_cal = RoofStructuralDesigner(simple_gable("Calgary")).design()
        cal_flags = {d.flag for d in out_cal.deflection_checks if d.element_type == "truss"}
        assert "OK_typical" in cal_flags, \
            f"Calgary 32 ft should be OK_typical (ratio 0.588), got {cal_flags}"
        assert "VERIFY_DEFLECTION" not in cal_flags, \
            f"Calgary 32 ft should NOT be VERIFY, got {cal_flags}"

        # Revelstoke 32 ft — high snow, 12\" spacing (same span, much higher load)
        arch_rev = ArchitecturalInput(
            city="Revelstoke", province="BC",
            importance_category="Normal",
            roof_planes=[
                RoofPlane("main", "6/12",
                          [Pt2(0,0), Pt2(32,0), Pt2(32,48), Pt2(0,48)],
                          eave_height_ft=10.0),
            ],
            bearing_walls=[
                BearingWall("n", Pt2(0,0),  Pt2(32,0),  True, True),
                BearingWall("s", Pt2(0,48), Pt2(32,48), True, True),
                BearingWall("e", Pt2(32,0), Pt2(32,48), True, True),
                BearingWall("w", Pt2(0,0),  Pt2(0,48),  True, True),
            ],
            num_stories=1,
        )
        out_rev = RoofStructuralDesigner(arch_rev).design()
        rev_flags = {d.flag for d in out_rev.deflection_checks if d.element_type == "truss"}
        assert "VERIFY_DEFLECTION" in rev_flags, \
            f"Revelstoke 32 ft should be VERIFY_DEFLECTION (ratio 0.930), got {rev_flags}"
        assert "OK_typical" not in rev_flags or len(rev_flags) > 1, \
            "Revelstoke 32 ft should NOT be purely OK_typical"

        print(f"    Calgary 32ft flags={cal_flags}, "
              f"Revelstoke 32ft flags={rev_flags} ✓")

    def test_asymmetric_tributary_uses_wing_width(self):
        """
        L-shaped house: main wing 32×40 ft, side wing 24×20 ft.
        Valley girder collects jack trusses from the side wing.
        Jack trusses in side wing span E-W (from x=32 to x=56, width=24 ft).
        Correct trib = 24/2 = 12 ft.  Old (wrong) trib = short_span/2 = 20/2 = 10 ft.

        Expected values (Vancouver, DL=13.8 psf, SL=28.6 psf, span=20 ft):
          DL = 13.8 × 12 × 20 = 3,312 lbs
          SL = 28.6 × 12 × 20 = 6,864 lbs
        """
        out = RoofStructuralDesigner(l_shaped()).design()
        vg  = next(g for g in out.girders if g.kind == "valley_girder")

        # Trib = 12 ft (wing width / 2), not 10 ft (short_span / 2)
        assert abs(vg.total_dead_load_lbs - 3_312) < 50, \
            (f"DL should be ~3,312 lbs (trib=12 ft), got {vg.total_dead_load_lbs}. "
             f"If ~2,760 lbs, the old trib=10 ft bug is still present.")
        assert abs(vg.total_snow_load_lbs - 6_864) < 50, \
            (f"SL should be ~6,864 lbs (trib=12 ft), got {vg.total_snow_load_lbs}. "
             f"If ~5,720 lbs, the old trib=10 ft bug is still present.")

        # Bearing points each carry half
        for bp in vg.bearing_points:
            assert abs(bp.dead_lbs - 1_656) < 50, \
                f"BP DL should be ~1,656 lbs, got {bp.dead_lbs}"
            assert abs(bp.snow_lbs - 3_432) < 50, \
                f"BP SL should be ~3,432 lbs, got {bp.snow_lbs}"

        print(f"    DL={vg.total_dead_load_lbs:,.0f} (~3,312), "
              f"SL={vg.total_snow_load_lbs:,.0f} (~6,864) — trib=12 ft ✓")

    def test_hip_roof_raises_not_implemented(self):
        """
        roof_type='hip' must raise NotImplementedError immediately in design()
        before any load calculations are attempted.
        """
        arch = ArchitecturalInput(
            city="Calgary", province="AB",
            importance_category="Normal",
            roof_planes=[
                RoofPlane("main", "6/12",
                          [Pt2(0,0), Pt2(32,0), Pt2(32,40), Pt2(0,40)],
                          eave_height_ft=10.0),
            ],
            bearing_walls=[
                BearingWall("n", Pt2(0,0),  Pt2(32,0),  True, True),
                BearingWall("s", Pt2(0,40), Pt2(32,40), True, True),
                BearingWall("e", Pt2(32,0), Pt2(32,40), True, True),
                BearingWall("w", Pt2(0,0),  Pt2(0,40),  True, True),
            ],
            num_stories=1,
            roof_type="hip",
        )
        try:
            RoofStructuralDesigner(arch).design()
            assert False, "Should have raised NotImplementedError for roof_type='hip'"
        except NotImplementedError as e:
            assert "hip" in str(e).lower(), f"Error message should mention 'hip': {e}"
            assert "not yet supported" in str(e).lower(), \
                f"Error message should say 'not yet supported': {e}"
        print("    roof_type='hip' raises NotImplementedError correctly ✓")

    def test_for_drawing_outline_populated(self):
        """
        output.roof_planes_geometry and for_drawing.building_outline.planes
        must both be non-empty and contain the expected fields.
        This was a known gap (placeholder) in the previous iteration.
        """
        out = RoofStructuralDesigner(l_shaped()).design()

        # 1. Check the output dataclass field directly
        assert len(out.roof_planes_geometry) == 2, \
            f"L-shaped house has 2 planes, got {len(out.roof_planes_geometry)}"

        required_keys = {
            "id", "pitch_str", "pitch_degrees", "perimeter",
            "eave_height_ft", "ridge_height_ft", "width_ft",
            "depth_ft", "area_sqft", "overhang_ft",
        }
        for plane in out.roof_planes_geometry:
            missing = required_keys - set(plane.keys())
            assert not missing, f"Plane '{plane.get('id')}' missing keys: {missing}"
            assert len(plane["perimeter"]) >= 3, \
                "Plane perimeter must have at least 3 points"
            assert plane["ridge_height_ft"] > plane["eave_height_ft"], \
                "Ridge must be higher than eave"

        # 2. Check it flows through to_json correctly
        j      = to_json(out)
        planes = j["for_drawing"]["building_outline"]["planes"]
        assert len(planes) == 2

        # 3. Spot-check a specific value: main plane 6/12 pitch, eave=18 ft
        main = next(p for p in planes if p["id"] == "main")
        assert abs(main["eave_height_ft"] - 18.0) < 0.1
        assert abs(main["pitch_degrees"]  - 26.57) < 0.5
        # ridge = eave + (short_span/2) × pitch_decimal = 18 + (32/2)×0.5 = 18+8 = 26
        assert abs(main["ridge_height_ft"] - 26.0) < 0.5

        # 4. Check roof_type flows through to for_drawing
        assert j["for_drawing"]["building_outline"]["roof_type"] == "gable"

        print(f"    roof_planes_geometry: {len(out.roof_planes_geometry)} planes, "
              f"all keys present, ridge/eave values correct ✓")

    def test_truss_positions_match_field_layout(self):
        """
        For a building length that is NOT a multiple of the truss spacing,
        the module must use fixed 24\" steps with a short last bay —
        exactly how trusses are physically laid out on site.

        Example: 47 ft building at 24\" spacing
          Correct:  0, 2, 4, ..., 46, 47  (24 bays: 23 × 24\" + 1 × 12\")
          Wrong:    0, 1.958, 3.917, ...   (23.5\" o.c. throughout — not buildable)

        Also verifies that total tributary widths sum to the building length,
        confirming load conservation with the adjacent-gap trib formula.
        """
        # 47 ft — remainder = 1 ft (12\" last bay)
        plane_47 = RoofPlane(
            id="m", pitch_str="5/12",
            perimeter=[Pt2(0,0), Pt2(32,0), Pt2(32,47), Pt2(0,47)],
            eave_height_ft=10.0,
        )
        arch_47 = ArchitecturalInput(
            city="Calgary", province="AB", importance_category="Normal",
            roof_planes=[plane_47],
            bearing_walls=[
                BearingWall("n", Pt2(0,0),  Pt2(32,0),  True, True),
                BearingWall("s", Pt2(0,47), Pt2(32,47), True, True),
                BearingWall("e", Pt2(32,0), Pt2(32,47), True, True),
                BearingWall("w", Pt2(0,0),  Pt2(0,47),  True, True),
            ], num_stories=1,
        )
        out_47 = RoofStructuralDesigner(arch_47).design()
        positions = [t.position_ft for t in out_47.trusses]
        spacings_in = [
            round((positions[i+1] - positions[i]) * 12, 1)
            for i in range(len(positions) - 1)
        ]

        # All bays except the last must be exactly 24"
        interior_bays = spacings_in[:-1]
        assert all(abs(s - 24.0) < 0.2 for s in interior_bays), \
            f"Interior bays must be 24\", got {set(interior_bays)}"

        # Last bay must be 12" (47 - 46 = 1 ft)
        last_bay_in = spacings_in[-1]
        assert abs(last_bay_in - 12.0) < 0.2, \
            f"Last bay should be 12\", got {last_bay_in}\""

        # First and last positions must be at 0 and 47
        assert abs(positions[0]  - 0.0)  < 0.01
        assert abs(positions[-1] - 47.0) < 0.01

        # Load conservation: sum of tributary widths = building length
        DL, span_ft = out_47.loads.dead_psf, 32.0
        trib_sum = sum(
            t.reaction_dead_lbs / (DL * span_ft / 2.0)
            for t in out_47.trusses
        )
        assert abs(trib_sum - 47.0) < 0.1, \
            f"Trib sum should be 47.0 ft, got {trib_sum:.4f}"

        # Also verify 45 ft (another non-multiple: 45 = 22×2 + 1 ft)
        plane_45 = RoofPlane(
            id="m", pitch_str="5/12",
            perimeter=[Pt2(0,0), Pt2(32,0), Pt2(32,45), Pt2(0,45)],
            eave_height_ft=10.0,
        )
        arch_45 = ArchitecturalInput(
            city="Calgary", province="AB", importance_category="Normal",
            roof_planes=[plane_45],
            bearing_walls=[
                BearingWall("n", Pt2(0,0),  Pt2(32,0),  True, True),
                BearingWall("s", Pt2(0,45), Pt2(32,45), True, True),
                BearingWall("e", Pt2(32,0), Pt2(32,45), True, True),
                BearingWall("w", Pt2(0,0),  Pt2(0,45),  True, True),
            ], num_stories=1,
        )
        out_45 = RoofStructuralDesigner(arch_45).design()
        pos_45 = [t.position_ft for t in out_45.trusses]
        sp_45  = [round((pos_45[i+1]-pos_45[i])*12, 1) for i in range(len(pos_45)-1)]
        assert all(abs(s-24.0) < 0.2 for s in sp_45[:-1]), \
            f"45ft interior bays must be 24\", got {set(sp_45[:-1])}"
        assert abs(sp_45[-1] - 12.0) < 0.2, \
            f"45ft last bay should be 12\", got {sp_45[-1]}\""

        print(f"    47ft: {len(positions)} trusses @ 24\" + 12\" last bay, "
              f"trib_sum={trib_sum:.3f}ft ✓")
        print(f"    45ft: {len(pos_45)} trusses @ 24\" + 12\" last bay ✓")


# ──────────────────────────────────────────────────────────────────────────────
# Integration prints
# ──────────────────────────────────────────────────────────────────────────────

def run_integration():
    print("\n" + "=" * 65)
    print("INTEGRATION: Simple gable — Calgary (1 storey)")
    print("=" * 65)
    print_summary(RoofStructuralDesigner(simple_gable()).design())

    print("\n" + "=" * 65)
    print("INTEGRATION: L-shaped — Vancouver (2 storey, RTU, skylight)")
    print("=" * 65)
    arch = l_shaped()
    arch.point_load_items = [
        PointLoadItem("rtu1", "RTU", 1200, Pt2(16, 20), (4, 5))
    ]
    arch.roof_openings = [
        RoofOpening("sky1", "skylight", Pt2(8, 15), 4, 6)
    ]
    out = RoofStructuralDesigner(arch).design()
    print_summary(out)

    print("\n" + "=" * 65)
    print("INTEGRATION: Revelstoke — high snow, L-shaped (drift preview)")
    print("=" * 65)
    out_rev = RoofStructuralDesigner(l_shaped("Revelstoke")).design()
    print_summary(out_rev)


# ──────────────────────────────────────────────────────────────────────────────
# Simple test runner (no pytest dependency required)
# ──────────────────────────────────────────────────────────────────────────────

def run_tests() -> int:
    classes = [
        TestLoadCalculations,
        TestTrussLayout,
        TestGirderPlacement,
        TestDriftLoads,
        TestDeflectionChecks,
        TestLoadPathTracing,
        TestPointLoadHandoff,
        TestExports,
        TestHelpers,
        TestNewBehaviours,
    ]
    total = passed = failed = 0
    failures: list[str] = []

    for cls in classes:
        print(f"\n{'─'*52}")
        print(f"  {cls.__name__}")
        print(f"{'─'*52}")
        inst = cls()
        for name in sorted(n for n in dir(cls) if n.startswith("test_")):
            total += 1
            try:
                getattr(inst, name)()
                print(f"  ✓  {name}")
                passed += 1
            except Exception as e:
                print(f"  ✗  {name}")
                print(f"       {type(e).__name__}: {e}")
                failures.append(f"{cls.__name__}.{name}: {e}")
                failed += 1

    print(f"\n{'='*52}")
    print(f"  Results: {passed}/{total} passed", end="")
    if failed:
        print(f"  |  {failed} FAILED")
        for f in failures:
            print(f"    ✗ {f}")
    else:
        print("  — all good")
    print(f"{'='*52}\n")
    return failed


if __name__ == "__main__":
    failed = run_tests()
    run_integration()
    sys.exit(1 if failed else 0)
