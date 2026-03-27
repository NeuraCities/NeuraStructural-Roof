# Roof Structural Design Module

`roof_structural.py` — Engineer of Record (EOR) Roof Analysis  
Western Canada (BC / AB) · NBCC 2020 / BCBC 2024 / ABC 2019

---

## What This Is

This module performs the structural engineering calculations an EOR carries out at the roof level before any other part of the building can be sized. The core idea is **load path management**: every kilogram of snow, every kilogram of dead weight, every heavy rooftop unit must have a clear, unbroken path from the roof surface to the foundation. This module traces those paths, quantifies the loads at each step, and packages the results in formats the downstream modules — foundation designer, frame analyzer, drawing tool — can consume directly.

It is **not** a truss design tool. It does not size individual lumber members. It does not replace the truss manufacturer's engineering. What it does is establish what the trusses must carry, where the critical concentrated loads land, and what the frame below needs to support — exactly the handoff an EOR writes before those other disciplines pick up their scope.

---

## Where It Sits in the Pipeline

```
Architectural drawings (PDF / DWG)
         │
         ▼
    Processor                   ← extracts geometry → ArchitecturalInput
         │
         ▼
  Roof Structural               ← THIS MODULE
  Design Module
         │
         ├──▶  for_foundation     ──▶  Foundation Designer
         │
         ├──▶  for_frame_analyzer ──▶  Frame Analyzer (PyNite)
         │
         └──▶  for_drawing        ──▶  Drawer (construction drawings)
```

The roof module runs **second** — immediately after the processor extracts geometry from architectural drawings. Foundation design happens **after** the frame analyzer, not directly after this module. The correct order is:

```
Roof → Frame Analyzer → Foundation Designer → Drawer
```

The roof module produces the loads; the frame analyzer accumulates floor dead/live loads on top of them; the foundation designer sees the full accumulated vertical load from all stories combined.

---

## Technical Requirements

```
Python         3.10 or later
Dependencies   None — stdlib only (math, csv, io, json, dataclasses, typing)
Test runner    Built-in (python test_roof_structural.py) or pytest
```

No external structural libraries. All NBCC calculations are implemented directly from the code text, keeping the module fully auditable and dependency-free.

---

## How to Use It

### Minimal example

```python
from roof_structural import (
    ArchitecturalInput, RoofPlane, BearingWall, Pt2,
    RoofStructuralDesigner, print_summary, export_to_files
)

arch = ArchitecturalInput(
    city="Vancouver",
    province="BC",
    importance_category="Normal",    # "Low" | "Normal" | "High" | "Post-Disaster"
    roof_planes=[
        RoofPlane(
            id="main",
            pitch_str="6/12",
            perimeter=[Pt2(0,0), Pt2(32,0), Pt2(32,40), Pt2(0,40)],
            eave_height_ft=18.0,
        )
    ],
    bearing_walls=[
        BearingWall("north", Pt2(0,0),  Pt2(32,0),  is_exterior=True, continuous_to_foundation=True),
        BearingWall("south", Pt2(0,40), Pt2(32,40), is_exterior=True, continuous_to_foundation=True),
        BearingWall("east",  Pt2(32,0), Pt2(32,40), is_exterior=True, continuous_to_foundation=True),
        BearingWall("west",  Pt2(0,0),  Pt2(0,40),  is_exterior=True, continuous_to_foundation=True),
    ],
    num_stories=2,
    roofing_material="asphalt_shingle",
    roof_type="gable",              # "gable" | "shed" | "flat" — "hip" raises NotImplementedError
)

output = RoofStructuralDesigner(arch).design()
print_summary(output)
files = export_to_files(output, directory="./roof_outputs")
```

### Getting the JSON and CSV outputs

```python
from roof_structural import to_json, to_csv_tables
import json

j = to_json(output)

# Three consumer-ready packages inside the JSON:
foundation_package   = j["for_foundation"]
frame_package        = j["for_frame_analyzer"]
drawing_package      = j["for_drawing"]

# Six CSV tables:
tables = to_csv_tables(output)
# {"point_loads.csv": "...", "wall_loads.csv": "...", ...}
```

---

## Inputs (`ArchitecturalInput`)

All inputs flow through a single `ArchitecturalInput` dataclass — this is what the processor populates from the architectural drawings.

| Field | Type | Description |
|---|---|---|
| `city` | `str` | Must match a city in `NBCC_CLIMATE` (20 cities currently). Drives Ss and Sr values. |
| `province` | `str` | `"BC"` or `"AB"` |
| `importance_category` | `str` | `"Low"` / `"Normal"` / `"High"` / `"Post-Disaster"` — sets Is factor |
| `roof_planes` | `list[RoofPlane]` | One entry per distinct roof surface. Multiple planes trigger valley girder detection and drift calculation. |
| `bearing_walls` | `list[BearingWall]` | All exterior walls + any interior walls confirmed on drawings. Exterior always treated as bearing. Interior walls: include only those explicitly shown — the frame analyzer resolves the rest. |
| `num_stories` | `int` | Stories below roof. Controls how many levels the load path trace descends. |
| `roofing_material` | `str` | `"asphalt_shingle"` / `"metal"` / `"clay_tile"` / `"concrete_tile"` — sets dead load |
| `wind_exposure` | `str` | `"sheltered"` or `"exposed"` — sets Cw (1.0 / 0.75) |
| `roof_type` | `str` | `"gable"` / `"shed"` / `"flat"`. `"hip"` raises `NotImplementedError` immediately. |
| `has_cathedral_ceiling` | `bool` | Triggers L/360 deflection limit and ridge beam warning |
| `roof_openings` | `list[RoofOpening]` | Skylights, chimneys, hatches — proximity warnings near girder bearing points |
| `point_load_items` | `list[PointLoadItem]` | RTUs, solar racks, tanks. Items over 2,000 lbs are flagged critical and traced to foundation. |

### `RoofPlane`

```python
RoofPlane(
    id="main",
    pitch_str="6/12",            # rise/run as string
    perimeter=[...],             # list of Pt2(x, y) corners at eave level, in feet
    eave_height_ft=18.0,         # elevation of eave above grade
    overhang_ft=2.0,             # horizontal projection of overhang (default 2 ft)
)
```

Coordinates in **feet** from a consistent project origin — typically the SW corner of the building footprint.

### `BearingWall`

```python
BearingWall(
    id="north_ext",
    start=Pt2(0, 0),
    end=Pt2(32, 0),
    is_exterior=True,
    continuous_to_foundation=True,  # False triggers "transfer beam required" warnings
    floor="all",                    # "all" | "1st" | "2nd"
)
```

`continuous_to_foundation=False` means a known discontinuity exists below this wall — any concentrated load landing on it will flag a transfer beam requirement. Set this when architectural drawings show a wall that stops at an intermediate floor.

---

## Outputs

### Full JSON structure (`to_json`)

```
roof_structural_output.json
├── metadata                    city, province, code reference
├── design_loads                All NBCC load results:
│   ├── dead_psf
│   ├── design_snow_psf         Is × [Ss×Cb×Cw×Cs×Ca + Sr]
│   ├── slope_factor_Cs         per NBCC 4.1.6.2
│   ├── exposure_factor_Cw
│   ├── importance_factor_Is
│   ├── service_combo_psf       D + S  (deflection check)
│   ├── strength_combo_psf      1.25D + 1.5S  (member sizing)
│   ├── recommended_spacing_in  12" / 16" / 24" (snow-driven)
│   └── load_combinations       NBCC 4.1.3.2 reference strings
│
├── truss_layout                Per truss:
│   └── [...]                   id, kind, span_ft, spacing_in,
│                               reaction_dead_lbs, reaction_snow_lbs,
│                               reaction_factored_lbs, in_drift_zone
│
├── girder_layout               Per girder:
│   └── [...]                   id, kind, span_ft, span_direction,
│                               total_dead/snow/factored_lbs,
│                               bearing_points (location + DL/SL + post spec)
│
├── drift_zones                 NBCC 4.1.6.5 valley drift estimates:
│   └── [...]                   valley_x, drift_width_ft,
│                               peak_additional_psf, avg_additional_psf,
│                               affected_truss_ids
│
├── deflection_checks           Per element (every truss + girder):
│   └── [...]                   span_ft, limit_live_in (L/180),
│                               limit_total_in (L/240 or L/360),
│                               req_EI_live_lb_in2,
│                               req_EI_total_lb_in2,
│                               governing_EI_lb_in2,
│                               min_depth_rec_in,
│                               flag (OK_typical / VERIFY_DEFLECTION / CRITICAL_DEFLECTION)
│
├── bearing_points              All critical concentrated loads
├── load_paths                  Roof → foundation traces, stage by stage
├── warnings                    EOR flag list (load path breaks, high loads, drift)
├── summary                     Metrics at a glance
│
├── for_foundation              ← consumed by Foundation Designer
│   ├── column_loads            x/y/z, service_lbs, factored_lbs, required_post
│   ├── wall_line_loads         dead_plf, snow_plf, factored_plf per wall
│   └── drift_zone_additional   extra snow load near valleys for footing sizing
│
├── for_frame_analyzer          ← consumed by Frame Analyzer (PyNite)
│   ├── load_cases
│   │   ├── dead                distributed_wall_loads (w_plf) + point_loads (Fy_lbs, negative = down)
│   │   └── snow                distributed_wall_loads + point_loads + drift_zone_upgrades
│   ├── load_combinations       "1.25D + 1.5S" / "D + S" reference strings
│   ├── deflection_requirements EI requirements by element
│   └── interior_support_req    Locations where frame must provide support;
│                               frame decides post vs wall vs transfer beam
│
└── for_drawing                 ← consumed by Drawer
    ├── building_outline        roof_type + planes array:
    │   └── [per plane]         id, pitch_str, pitch_degrees, perimeter,
    │                           eave_height_ft, ridge_height_ft,
    │                           width_ft, depth_ft, area_sqft, overhang_ft
    ├── truss_layout_plan       position, span, in_drift_zone, line_weight
    ├── girders                 2-D + 3-D coordinates, label text
    ├── post_symbols            x/y, symbol type, label (load + post spec)
    ├── drift_zone_hatching     hatch boundaries + label text
    └── notes_table             Pre-written general notes ready to stamp
```

### CSV tables (`to_csv_tables`)

Six flat tables covering all consumers:

| File | Rows | Primary consumer |
|---|---|---|
| `point_loads.csv` | One per critical bearing point | Foundation, Frame |
| `wall_loads.csv` | One per bearing wall (DL + SL + factored plf) | Foundation, Frame |
| `truss_layout.csv` | One per truss (DL, SL, factored reactions) | Drawing, Manufacturer |
| `girder_layout.csv` | One per girder with both bearing points | Drawing, Frame |
| `drift_zones.csv` | One per drift zone | EOR review, Manufacturer |
| `deflection_checks.csv` | One per truss + girder (EI requirements + flag) | Manufacturer |

---

## What the Module Calculates (for Structural Engineers)

### 1. Snow loads — NBCC 2020 §4.1.6

```
S = Is × [Ss × Cb × Cw × Cs × Ca + Sr]
```

| Factor | Value / Source |
|---|---|
| Ss, Sr | NBCC Table C-2, hard-coded for 20 cities |
| Is | 0.8 / 1.0 / 1.15 / 1.25 (Importance category) |
| Cb | 0.8 (basic roof factor, uniform load) |
| Cw | 1.0 sheltered / 0.75 exposed |
| Cs | Slope factor: 1.0 (α ≤ 15°), linear reduction to 0 at 60° |
| Ca | 1.0 base case; valley drift handled separately |

Truss spacing from snow load: < 35 psf → 24" o.c. · 35–60 psf → 16" o.c. · ≥ 60 psf → 12" o.c.

### 2. Dead loads (psf, by roofing material)

| Material | Roofing | Sheathing | Framing | Ceiling | Insulation | **Total** |
|---|---|---|---|---|---|---|
| Asphalt shingle | 3.5 | 2.3 | 4.0 | 2.5 | 1.5 | **13.8** |
| Metal | 2.0 | 2.3 | 4.0 | 2.5 | 1.5 | **12.3** |
| Clay tile | 10.0 | 2.3 | 4.0 | 2.5 | 1.5 | **20.3** |
| Concrete tile | 12.0 | 2.3 | 4.0 | 2.5 | 1.5 | **22.3** |

Heavy rooftop items are tracked both as distributed dead load (over total roof area, for spacing/snow check) and as discrete concentrated loads (for bearing point / load path analysis).

### 3. Load combinations — NBCC 4.1.3.2

```
Strength (member sizing):  1.25D + 1.5S
Service (deflection):      D + S
Uplift:                    0.9D − 1.4W   ← flagged; not computed here
```

DL and SL are kept separate throughout — not pre-combined until the factored values. The frame analyzer receives them as separate load cases and builds its own combinations.

### 4. Truss layout

Trusses span the **shortest** plan direction. Gable end trusses at the two building ends get half the tributary width of standard trusses. Reactions per end by simple beam mechanics (R = wL/2), with DL and SL reported separately.

### 5. Girder identification

**Valley girders** — placed wherever two roof planes share an edge (L, T, U shaped plans). The girder runs along the valley and collects jack trusses from the secondary wing. **Critical**: the tributary width is the full E-W span of the secondary wing divided by two — not the short span of that wing. For an asymmetric L-shape with a 24 ft wide side wing, trib = 12 ft regardless of how deep the wing is in the N-S direction.

**Midspan support girders** — placed when the truss span (short direction of a plane) exceeds 40 ft, halving the effective span.

Both types produce critical bearing points at each end that must trace to foundation.

### 6. Valley drift — NBCC 4.1.6.5

```
lu   = average short span of both planes (upwind fetch, metres)
hs   = S_kPa / ρ_snow         base snow depth on roof (ρ = 3.0 kN/m³)
hd   = min(0.8 × √lu,  1.5 × hs)     drift height at valley
xd   = min(4 × hd,  lu / 2)          drift width each side (metres)
peak = ρ_snow × hd × KPA_TO_PSF      additional load at valley centreline
avg  = peak / 2                       triangular distribution average
```

The module flags which specific trusses fall within the drift zone (`in_drift_zone = True`), provides the peak and average additional psf, and passes them to the frame package as `drift_zone_upgrades`. This is an **estimate** — the EOR must confirm with the full NBCC 4.1.6.5 calculation before stamping.

### 7. Deflection limits — NBCC Table 4.1.5.3

```
Trusses:   L/180 under snow (live) only
           L/240 under D + S total      (standard roof)
           L/360 under D + S total      (cathedral ceiling or tile finish)

Girders:   same criteria as trusses
```

Required EI in lb·in² for each element:

```
EI_required = 5 × w × L⁴ / (384 × δ_limit)

  w  = service load (lb/in)     L = clear span (in)
  δ  = L / limit_divisor
```

**Deflection flag** — based on EI ratio against a modelled 2×4 SPF #2 parallel-chord truss at the recommended minimum depth:

```
Typical truss model (2×4 SPF chord, d_rec = span/20):
  I_eff  = 2 × 5.36 + 2 × 5.25 × (d_rec/2)²   [parallel-axis theorem]
  EI_typ = 1,600,000 × I_eff

ratio = governing_EI_required / EI_typical

< 0.65   → OK_typical           standard truss at recommended depth expected adequate
0.65–1.0 → VERIFY_DEFLECTION    near capacity; manufacturer must confirm EI
≥ 1.0    → CRITICAL_DEFLECTION  exceeds typical; deeper truss or LVL chords needed
```

This flag accounts for **both span and load magnitude**. A 32 ft truss in Calgary (ratio 0.588) gets `OK_typical`. The same 32 ft truss in Revelstoke under 84.9 psf snow and 12" spacing (ratio 0.930) gets `VERIFY_DEFLECTION`. The old span-only flag could not distinguish these cases.

### 8. Load path tracing

For every critical bearing point, the module walks level by level:

```
Roof bearing point
  → 2nd floor wall: find bearing wall within 2 ft of (x, y)
  → 1st floor wall: find bearing wall within 2 ft of (x, y)
  → Foundation: confirmed
```

No wall found → DISCONTINUITY → "transfer beam required."  
Wall found but `continuous_to_foundation=False` → same warning.

**Corner-point disambiguation**: when a bearing point lands at a corner where two walls intersect, the module uses the girder's `load_direction` to prefer the perpendicular wall — a N-S-spanning girder deposits load on E-W walls. This prevents the wrong wall's `continuous_to_foundation` flag being checked.

---

## What the Module Does NOT Do (Scope Boundaries)

| Item | Where it belongs |
|---|---|
| Truss member sizing (chord, web) | Truss manufacturer |
| Interior post / wall sizing | Frame Analyzer |
| Shear wall / lateral design | Frame Analyzer |
| Connection hardware (hurricane ties, post caps) | Post-frame processing step |
| Ridge beam sizing (cathedral ceiling) | Frame Analyzer / separate beam design |
| Foundation sizing (footing dimensions) | Foundation Designer |
| Seismic analysis | Frame Analyzer |
| Wind pressure on walls | Frame Analyzer |
| Unbalanced snow (NBCC 4.1.6.4) | Not yet implemented |
| Sliding snow (NBCC 4.1.6.6) | Not yet implemented |
| Hip roof geometry | Not yet implemented — raises NotImplementedError |

---

## Known Limitations and Future Work

Items are ordered by impact. High-priority items from the previous iteration have been resolved and are marked accordingly.

### Resolved since previous iteration

**✅ Deflection flag now accounts for load magnitude**  
Previously span-only (≤32/32–40/>40 ft). Now uses EI ratio against a modelled 2×4 SPF chord truss at the recommended depth. Revelstoke 32 ft trusses now correctly get `VERIFY_DEFLECTION` where Calgary 32 ft gets `OK_typical`.

**✅ Valley girder tributary width corrected**  
Previously used `short_span_ft / 2`, which incorrectly used the depth of the secondary wing instead of its width. Now uses `(bx_max − bx_min) / 2` — the actual E-W span of the jack trusses framing into the girder. For a 24 ft wide side wing this changes trib from 10 ft → 12 ft (+20% in girder loads).

**✅ Hip roof guard added**  
Passing `roof_type="hip"` now raises `NotImplementedError` immediately in `design()` before any calculations run, rather than silently producing incorrect rectangular geometry.

**✅ `for_drawing.building_outline` fully populated**  
Previously an empty placeholder. Now contains a `planes` array with all 10 geometry fields per plane: id, pitch_str, pitch_degrees, perimeter (list of x/y points), eave_height_ft, ridge_height_ft, width_ft, depth_ft, area_sqft, overhang_ft.

### Still open — high priority (affect correctness)

**1. Unbalanced snow not computed (NBCC 4.1.6.4)**  
Wind redistributes snow windward-to-leeward on pitched roofs. NBCC 4.1.6.4 specifies unbalanced load cases that can govern truss design. Currently flagged in notes only — not calculated. Fix: compute windward/leeward split based on roof pitch and wind exposure; annotate affected trusses with separate load cases.

**2. Sliding snow not computed (NBCC 4.1.6.6)**  
Snow sliding from an upper roof onto a lower one — stepped roofs, dormers, attached garages. Not the same as valley drift accumulation. Fix: detect elevation transitions between roof planes and compute the sliding load on the lower element per NBCC 4.1.6.6.

**3. Wind uplift not computed (NBCC 4.1.7)**  
`0.9D − 1.4W` is referenced in the output but the wind pressure W is never calculated. Hurricane tie demand, truss-to-wall connection forces, and overturning checks all require it. Fix: implement NBCC 4.1.7 basic wind pressure calculation and compute net uplift force at each truss bearing.

**4. Valley girder tributary is still one-sided**  
The module picks the secondary (incoming) wing and uses its E-W span / 2 as the tributary. For a true valley between two equal wings, both sides contribute load to the girder. The current logic captures jack trusses from one side only. Fix: compute tributary separately for each contributing plane and sum.

### Medium priority (affect completeness)

**5. Only 20 cities in NBCC climate table**  
Unknown cities raise `ValueError`. Rural and small-town sites are common clients in BC and AB. Fix: (a) add more cities from NBCC Appendix C, or (b) allow explicit `Ss` and `Sr` override fields on `ArchitecturalInput`.

**6. Hip roofs not implemented**  
Hip roofs require hip girders, hip jack trusses, and corner geometry that is a meaningfully different engineering problem. The module raises `NotImplementedError` for `roof_type="hip"` — this is correct behaviour, not a bug, but it is a substantial missing capability. Fix: implement a `HipRoofLayouter` subclass that handles corner geometry, hip rafter sizing, and hip jack truss layout.

**7. Girder span limit not enforced**  
EORs generally try to keep girder spans under 30 ft for practical shipping and installation reasons. A 45 ft valley girder is generated without warning. Fix: add a warning when any girder span exceeds 30 ft.

**8. RTU / solar DL/SL split is hard-coded 90/10**  
`PointLoadItem` splits weight 90% dead / 10% snow. Reasonable for RTUs but solar arrays in high-snow areas may accumulate significant snow load on panels. Fix: add a `dead_fraction` field to `PointLoadItem`, or compute from item type.

**9. No dormer handling**  
Dormers create interrupted truss layouts and additional valley/hip conditions. Not modelled — a dormer roof plane will be treated as an isolated rectangle.

**10. No parapet drift**  
Parapet walls create separate drift accumulation zones per NBCC 4.1.6.5. Only valley drift between intersecting roof planes is computed.

### Lower priority (product maturity)

**11. NBCC climate data is hard-coded**  
The climate table should be externalised to a JSON or SQLite file updated independently of the code when NBCC tables are revised.

**12. No multi-storey load accumulation**  
The module traces load paths floor-by-floor but does not accumulate floor dead/live loads along the path. The foundation package shows only **roof** loads. The foundation designer adds floor loads. This is a deliberate scope decision — it could be confusing and should be clearly documented in the handoff.

**13. Connection hardware database not built**  
Simpson, MiTek, and USP product catalogs could be queried to match calculated demand to a specific model number. Deferred to a post-frame processing step.

---

## Running the Tests

```bash
python test_roof_structural.py        # built-in runner, no dependencies
python -m pytest test_roof_structural.py -v
```

**76 tests across 10 test classes — all pass.**

| Test class | Count | What it covers |
|---|---|---|
| `TestLoadCalculations` | 10 | NBCC snow formula, Cs, Cw, spacing thresholds, DL/SL separation |
| `TestTrussLayout` | 6 | Span direction, gable end half-tributary, DL/SL reaction split |
| `TestGirderPlacement` | 7 | Valley detection, corrected tributary width, midspan trigger |
| `TestDriftLoads` | 7 | NBCC 4.1.6.5 formula, triangular distribution, zone flagging |
| `TestDeflectionChecks` | 9 | L/180, L/240, L/360, EI formula, EI-ratio flag by load magnitude |
| `TestLoadPathTracing` | 5 | Complete paths, discontinuities, corner disambiguation |
| `TestPointLoadHandoff` | 7 | DL/SL split, required fields, critical threshold, frame handoff |
| `TestExports` | 16 | JSON structure, CSV headers, round-trip, file writing, building_outline |
| `TestHelpers` | 6 | Geometry math (segment distance, bbox shared edge, post spec) |
| `TestNewBehaviours` | 4 | EI flag by load, tributary width fix, hip guard, drawing outline |

---

## Code Architecture

```
roof_structural.py  (~1,700 lines)
│
├── NBCC reference data         NBCC_CLIMATE, SNOW_IMPORTANCE, DEAD_LOAD_COMPONENTS
│
├── Input dataclasses           Pt2, Pt3, RoofPlane, BearingWall, ArchitecturalInput,
│                               RoofOpening, PointLoadItem
│
├── Output dataclasses          DesignLoads, TrussInfo, GirderInfo, BearingPoint,
│                               DriftLoad, DeflectionCheck, LoadPath, RoofDesignOutput
│                               (includes roof_planes_geometry field)
│
├── RoofStructuralDesigner      Main class — 8-step design workflow:
│   ├── design()                Hip guard → all 8 steps → planes_geo → RoofDesignOutput
│   ├── _calculate_loads        NBCC 4.1.6 snow + dead, spacing recommendation
│   ├── _layout_trusses         Span direction, spacing, gable ends, DL/SL reactions
│   ├── _identify_girders       Valley intersections (corrected trib), long spans
│   ├── _calculate_drift_loads  NBCC 4.1.6.5 valley accumulation estimate
│   ├── _check_deflection_limits L/180 + L/240/360, EI formula, EI-ratio flag
│   ├── _collect_bearing_points Girder ends + heavy point load items
│   ├── _trace_load_paths       Roof → foundation, corner disambiguation
│   └── _package_*              point_loads_to_floors, bearing_wall_loads (DL/SL split)
│
├── Helper functions            _post_spec, _pt_to_segment_dist, _bbox_shared_edge
│
└── Export functions            to_json, to_csv_tables, export_to_files, print_summary
```

The `RoofStructuralDesigner` is stateless between runs — instantiate, call `.design()`, discard. Fully deterministic: same inputs always produce the same outputs.

---

## Notes for Structural Engineers Using This Output

**1. Dead and snow loads are always separate.** The module provides both. Use factored (`1.25D + 1.5S`) for member sizing. Use service (`D + S`) for deflection checks. Never sum them first.

**2. Drift calculations are estimates.** NBCC 4.1.6.5 outputs are a first-pass using the empirical formula. The EOR must perform the full calculation — including wind direction, specific obstruction geometry, and local snow density — before stamping. The module tells you *where* drift accumulates and gives a conservative magnitude.

**3. Interior bearing walls are only what's on the architectural drawings.** If an interior wall is not explicitly shown as bearing, it won't be in the model. Concentrated loads in open interior space will raise "transfer beam required" — that's the signal for the frame analyzer to design vertical support there.

**4. Post requirements are preliminary.** `triple_2x6_stud_pack`, `6x6_post_or_engineered_column`, and similar strings are starting-point recommendations based on factored load buckets. Final sizing must be per CSA O86-19 (wood) or CSA S16 (steel), including buckling checks for the post height.

**5. Governing EI goes to the truss manufacturer.** The module outputs span, spacing, dead reaction, snow reaction, and minimum required EI (lb·in²). The manufacturer sizes all chord and web members. The EOR reviews the manufacturer's sealed drawings against these requirements.

**6. `for_drawing.building_outline.planes` contains full geometry.** Each entry has perimeter (list of x/y plan coordinates), pitch, eave height, ridge height, and plan dimensions. The drawer has everything it needs to render the roof framing plan without going back to the processor.

**7. The flag is directional, not a pass/fail.** `VERIFY_DEFLECTION` does not mean the truss will fail — it means the load is significant enough that the manufacturer's EI confirmation matters. `CRITICAL_DEFLECTION` means the element will almost certainly need to be deeper than the minimum span/20 rule of thumb.

---

## Notes for Developers Integrating This Module

**The processor must populate `ArchitecturalInput` correctly.** The most critical fields:

- `roof_planes[].perimeter` — closed polygon in plan at eave level, in feet, consistent origin
- `bearing_walls[].continuous_to_foundation` — set `False` when drawings show the wall not running all the way down; drives transfer beam warnings
- `city` — must exactly match a key in `NBCC_CLIMATE`
- `roof_type` — set to `"hip"` and the module raises `NotImplementedError` before spending any compute

**The frame analyzer receives DL and SL as separate load cases.** The structure in `for_frame_analyzer.load_cases` has `dead` and `snow` dicts, each containing `distributed_wall_loads` (w_plf per wall) and `point_loads` (Fy_lbs, negative = downward). Apply NBCC combinations internally in the frame analyzer.

**`interior_support_requirements`** is the key handoff for interior posts. These are locations where the roof delivers a concentrated load but no confirmed bearing wall exists underneath. The frame analyzer treats each one as a required vertical support and determines the element type from what the floor plan allows.

**`drift_zone_upgrades`** in the snow load case lists valley zones with peak additional psf and drift width. The frame analyzer should apply this as a triangular additional distributed load on wall segments within the zone boundary.

**`for_drawing.building_outline.planes`** is now fully populated. Each plane has all 10 geometry fields. The drawer should use `perimeter` for plan outline, `eave_height_ft` and `ridge_height_ft` for elevation, and `pitch_degrees` for slope annotation.

**Extending the city list:**

```python
NBCC_CLIMATE["Terrace"] = {"Ss": 3.0, "Sr": 0.4}   # from NBCC 2020 Table C-2
```

**Extending roofing materials:**

```python
DEAD_LOAD_COMPONENTS["green_roof"] = {
    "roofing": 25.0, "sheathing": 2.3, "framing": 4.0,
    "ceiling_drywall": 2.5, "insulation": 1.5,
}
```

**Adding hip roof support** is the largest pending development task. It requires: hip corner detection from polygon geometry, a `HipRoofLayouter` class with hip girder and jack truss layout logic, hip rafter sizing at the ridge, and removal of the current `NotImplementedError` guard. This is a substantial engineering sub-problem and warrants its own module branch.
