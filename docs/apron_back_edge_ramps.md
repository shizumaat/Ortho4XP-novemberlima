# Apron back-edge ramps — flatter building pads via a twisting apron

> ⚠ **SUPERSEDED (2026-06-30 audit) by `taxi_slack_terminals.md`.** The back-edge-ramp
> model is a no-op under the default config (`TAXI_SLACK_TERMINALS` on → `_apron_back_band_nodes`
> returns `{}`) and its validator exemption was deleted (commit `54bd5d2`). Kept only for the
> `APRON_BACK_EDGE_GRADE` / `_apron_back_band_nodes` rationale (those symbols are still live).
> Removing the gated-off machinery is a cleanup candidate in **`OPEN_ITEMS.md`**.

**Status:** built — gate `APRON_BACK_EDGE_RAMPS`, **default ON** (user 2026-06-13,
for in-sim eval; `O4_APRON_BACK_RAMPS=0` disables, byte-identical-OFF proven at
SPJC with `PYTHONHASHSEED=0`). Suite baseline below.

**User direction (2026-06-13):** "Try allowing just the *back* edge of aprons (the
one(s) farthest from taxi routes) to go up to grade along that edge, to allow
steeper slopes between buildings so the buildings can be flatter, and the apron
twists slightly to meet them with ramps between, but the majority of the apron
stays at 1 %."

Decisions confirmed: back-edge cap = **bounded 4 %** (`APRON_BACK_EDGE_GRADE`);
back band = **building-frontage + inward depth** (`APRON_BACK_BAND_DEPTH_M`).

Related: [`apron_follows_resolve.md`](apron_follows_resolve.md),
[`network_profile_model.md`](network_profile_model.md), the DSF-pad-flatten
follow-up noted in the `dsf-building-outlines` memory.

---

## 1. Problem

Under the apron-follows model (`TERMINAL_NATURAL_LEVELS`) a building pad is
transparent during the solve, then **INHERITS** the median of its settled nodes
and flattens — *unless* doing so would make the surrounding apron gain
within-shape grade excess, in which case the flatten is **reverted** to the
sloped settled surface (the FLAT-vs-SLOPE acceptance,
`unified_jacobi._enforce_within_shape_grades` ≈ L2280).

On sloping terrain the binding constraint is the apron **behind / between** the
buildings. The apron there is capped at the 1–1.5 % apron grade, so:

* the **pairwise pad resolution** (`cap_pad9` ≈ L2203, 1.5 %) drags two
  adjacent flat pads to a compromise level (or forces both to slope) once
  their level difference exceeds `1.5 % · gap`; and
* even when a pad flattens, the apron ramp behind it registers as within-shape
  excess and the **acceptance** reverts the flatten.

The 1.5 % cap is *correct* on the taxi-facing front (aircraft move there). It is
*over-constraining* the back, which only has to be drivable, not smooth for
taxiing. This is the SPJC DSF `terminal3` regression (0.2–0.5 m within-shape
residuals after the DSF buildings landed) and the HECA southern-row / `#257`
family.

## 2. Model

Relax the apron grade cap **only on the back band** — the apron strip farthest
from taxi routes (the building frontage and the gaps between buildings) — up to a
bounded **4 %**. Keep the front and interior at 1 %. The apron then **twists**:
its iso-elevation lines fan out from a tight 1 % taxi-facing front to a steeper
back that tracks each building's flat level, with ramps between buildings
absorbing the level differences.

One relaxation, applied at the **three sites that hard-code the 1.5 % apron cap**
plus the corridor machinery and the validator. They must move together — relax
only the solver caps and the acceptance metric reverts the flatten; relax only
the acceptance and the corridor attractor / value bands flatten the twist back
out; relax only the solver and the emitted patch fails `pavement_grade`.

### Back band = the apron's GRASS edge (user refinement 2026-06-13)

> "Ideally the back edge should only be apron on grass; if there's a building it
> should be welded to the building."

A **back-band node** is an apron vertex on the apron's **free perimeter** — NOT
node-welded to any grade-bearing pavement (building, taxiway, junction, runway)
and NOT on the taxi front (within `APRON_CORRIDOR_SEED_RADIUS_M` of a corridor).
Where the apron meets a building it is **welded** (the pad's flat level), and
that frontage is **not** relaxed — the building stays flat. The taxi front stays
1 %. Only the grass-bordering rim may grade up to 4 %, so the apron twists along
its free edge to absorb level changes (between buildings, against terrain).

A **back edge** = an apron grade edge with **both** endpoints in the grass band.
Welds share node ids in the emitted patch, so the validator mirrors the solver
by the same node-sharing test.

⚠ **OPEN — band breadth.** The pure topological "not welded" test is too broad
at large airports (HECA: 576 of 978 apron nodes = 59 %), because an apron's
taxi-*facing* edge also isn't node-welded when it merely abuts the taxiway. This
violates "majority stays at 1 %". A "far from taxi" criterion (corridor distance
beyond the 1 % zone, or a fraction) is needed to keep the band to the true back.

## 3. The touches

All gated on `APRON_BACK_EDGE_RAMPS`; gate-OFF is byte-identical.

| # | Site | Change |
|---|------|--------|
| 0 | `config.py` | `APRON_BACK_EDGE_GRADE = 0.04`, `APRON_BACK_BAND_DEPTH_M = 40.0`, gate `APRON_BACK_EDGE_RAMPS` (env `O4_APRON_BACK_RAMPS`, default OFF). |
| 1 | `_apron_back_band_nodes(layout, nodes, shape_constraints)` | New helper, cached on the layout. Returns the back-band node set per §2. |
| 2 | `_build_shape_constraints` (apron visibility edges, ≈ L3273) | Back edges get `cap = APRON_BACK_EDGE_GRADE`. **Automatically fixes the FLAT-vs-SLOPE acceptance** (≈ L2288) — it sums excess against each edge's stored cap, so a legal back ramp stops counting and flattens stop reverting. |
| 3 | Pairwise pad resolution `cap_pad9` (≈ L2203) | Use `APRON_BACK_EDGE_GRADE` for the `|Δ| ≤ cap·gap` test — the gap between pads runs through the back band, so independently-flat adjacent pads survive. |
| 4 | Two-rate band `extra` slack (≈ L1992) | Back-band verts use `APRON_BACK_EDGE_GRADE` for the slack term so the value band does not re-flatten the warp. |
| 5 | Corridor-plane attractor (≈ L2039) + `_apron_zone_scaled_edges` (≈ L871) | **Skip back-band verts** in the attractor (most are already beyond `R_z` → "far"; make it explicit). Back edges keep the steep cap rather than being scaled to 1 %. |
| 6 | `check_grade._grade_violations` (≈ L705) | Building-frontage zone exemption, mirroring the existing **road-frontage zone**: apron pairs with both endpoints in the back band carry `APRON_BACK_EDGE_GRADE`, not the shape's 1.5 %. Validator-only (the solver already solves at the per-edge cap). |

Front / interior: **never lowered** — only back-band caps are raised; the
corridor smoothing keeps pulling the front to 1 %. "Majority stays at 1 %" holds
by construction.

## 4. Validation protocol

1. **Gate-OFF byte-identical** — diff emitted OSM with the gate OFF against the
   pre-change build at SPJC + CYXY. Must be byte-identical.
2. **Gate-ON builds** — SPJC, HECA, CYXY, KPHL:
   * building pads flatter (SPJC `terminal3` within-shape residuals → 0; HECA
     southern row flattens where it previously sloped/compromised);
   * back ramps present (apron back-band edges carry up to 4 %);
   * **front still 1 %** (taxi-facing apron unchanged);
   * runway invariants exact (HECA 05C 108.70, 05L 57.9–62.8);
   * deterministic across `PYTHONHASHSEED` 0/1/2.
3. **Suite** — `venv/bin/python -m pytest tests/ -q` at the standing baseline
   (no new reds beyond the documented standing set + the SPJC pad red this
   feature is meant to clear).
4. In-sim eval → decide default-ON and the final back-grade value (4 % vs
   steeper).

## 5. Rejected / deferred

* **Unbounded "follow terrain" back edge** — considered for "up to grade";
  rejected for the first build (arbitrary walls between buildings on steep
  terrain). Bounded 4 % chosen; revisit in-sim if pads still cannot flatten.
* **Distance-quantile back band** (no building dependency) — deferred; the
  frontage+depth anchor is more predictable and every back-edge case so far is
  building-driven. Quantile would only matter for building-less aprons, which do
  not have the pad-flatten problem.
* **Relaxing front-to-back chords** — explicitly NOT done; keeping one endpoint
  in the front at the normal cap is what keeps the front-to-back transition
  gradual and the "majority at 1 %" guarantee.
