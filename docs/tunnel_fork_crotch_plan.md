# Plan — auto-emit a clean fork crotch for ways splitting out of a tunnel

**Site:** KPHL RWY 26 **north portal** of the road+rail tunnel under the hill past
the threshold. Road and rail share one bore, then **fork** just outside the mouth
(rail bends left/west, road continues south). This is the `_emit_tunnel_portals`
**Y-split** path in `src/auto_patch/bridges.py` (added 2026-06-12).

**What we compared**

* `Patches/+30-080/+39-076/KPHL_auto.patch.osm` — current auto output.
* `Patches/+30-080/+39-076/KPHL_auto_MODIFIED.patch.osm` — user's hand-corrected
  geometry (elevations **not** touched).

Both files are byte-different mostly because node IDs were renumbered. The only
**structural** change is in the second tunnel cluster's fork (shapeIDs **322, 323,
325, 339, 340**): +8 nodes / +5 ways / +33 nd-refs in the MODIFIED file, all at the
crotch.

---

## 1. What the auto output does wrong

Rendered side-by-side (`/tmp/throat_auto.svg.png` vs `/tmp/throat_mod.svg.png`):

```
AUTO                                  MODIFIED (hand-fixed)
                                      
   [340]      [325]                      [340]\       /[325]
   left br.   right br.                   left \     / right
      (floating, gap)                          \   /
                                           [339 nose wall]
   ========= GAP =========                  \ | /
                                            [322] notch top
   [322] throat (flat top)                  [322] throat
   [319] ...                                [319] ...
```

Three concrete defects, all produced by the Y-split branch code
(`bridges.py` ~lines 1173–1306):

1. **Crotch is an unpaved hole.** The throat ramp (`322`) ends in a flat,
   cap-perpendicular top edge at `s_div` (`_emit_chain(throat, …)`, lines 1207–1219).
   Each branch is then **advanced forward** until its start clears the throat +
   sibling corridors (the `while s9 < bl.length - 4.0: … s9 += 2.0` loop, lines
   1252–1266). With a wide combined throat and a wide fork angle (~60–90° here),
   that advance pushes both branch starts metres past the fork, leaving a triangular
   **black hole** between the throat top and the two branch bottoms.

2. **Branches float, detached from the throat.** Each branch's first ramp is a
   constant-half-width rectangle (`_emit_chain` builds parallel offsets at
   `±chain_half`, lines 1120–1123). Its inner edge never converges toward the fork,
   so it does not touch the throat — there is a visible step/seam, not a continuous
   road surface.

3. **No median nose between the diverging carriageways.** Real forks have a paved/
   walled "nose" island where the two roads split. The auto output has nothing
   there; the side walls (`tunnel_wall`) are simply clipped open where a branch
   crosses them (WALL OPENINGS clip, lines 1273–1306) and the gap is left bare.

## 2. What the user's correction does

* **Throat far end reshaped into a notch** — `322` went from a 4-corner quad to a
  **9-vertex** polygon whose top edge follows *both* branches' inner edges down to a
  central V apex (a Y-crotch), instead of a single flat edge.
* **Each branch extended down to the fork** — the first ramp of `325` (right) and
  `340` (left) was lengthened (~10 m → ~25–30 m) so its near end reaches the notch;
  duplicate-shapeID ramp pieces (`325`, `340`) were added at the crotch to fill in
  the taper. No gap remains.
* **Median nose wall added** — a small **9-vertex** `tunnel_wall` (`339`) sits at the
  crotch apex as the island between the two carriageways, plus short bounding walls
  (`323`, `339`).
* **Elevations left continuous, not re-solved.** Throat high-end `-3.2`, both branch
  near-ends `-3.2` → already continuous at the fork `e_div`. The hand pieces simply
  reuse adjacent altitudes. So **this is a geometry fix**; the existing linear
  `e_div` interpolation is correct and needs no change.

## 3. Target model (user-specified)

> "Like a sloping rect and junction, we should have a **throat** with a V-notch that
> **bridges (using `node_altitudes`)** between the sloping rects. The whole **Y**
> should be **enclosed/traced by the wall**, which stays at DEM until the ramp grades
> up to the DEM level. **No grading or pavement between the arms of the Y.**"

This is the taxiway **sloping-rect + junction** pattern applied to the fork, not a
"fill the crotch with ramp" patch. Three parts:

* **Arms = sloping rects.** Each diverging branch stays an ordinary `tunnel_ramp`
  (`profile=plane`, `altitude_low`→`altitude_high`) grading from the fork elevation up
  to DEM — unchanged from today's `_emit_chain` output, just not advanced/floated.

* **Throat = a junction polygon with `node_altitudes`.** The diverging end of the
  shared bore is **one polygon** whose far edge is a **V-notch** (apex pointing back
  into the throat). Its inner edges follow the two arms' inner edges. Because the two
  arms can grade at different rates, the throat is *not planar* — so it carries
  **per-vertex `node_altitudes`** (the codebase's exact mechanism for "polygons that
  slope in more than one direction", `layout.py:281`) to **bridge** the two arm
  slopes at the fork. This is precisely how taxiway junctions bridge their adjacent
  sloping rects.

* **One continuous wall traces the whole Y, including the inner V.** A single
  `ROLE_RETAINING_WALL` outline runs: up arm 1's outer edge → around the throat →
  down arm 2's outer edge → **and back up the inner V between the arms**. The wall
  sits at **DEM/`apt_elev`** and exists **only where the enclosed ramp is below DEM** —
  each arm's wall terminates where that arm's ramp reaches DEM (the existing
  `wall_thresh = apt_elev − 0.05` cutoff, `bridges.py:1056–1068`, already does this
  per-segment; it just needs to run along the *inner* V edge too).

* **Crotch stays bare.** The region between the two arms (inside the inner V) is
  **not paved and not graded** — it is terrain, bounded by the inner walls. The throat
  V-notch is what keeps pavement out of it. (Supersedes the earlier "median nose
  fill" idea — there is no paved nose; the median is just the two inner walls meeting
  at the apex.)

## 4. Proposed implementation

In the `s_div is not None` block of `_emit_tunnel_portals`
(`src/auto_patch/bridges.py` ≈1205–1306). Keep the parallel-bores path
(`s_div is None`) and the SPJC south cluster byte-identical; gate the new behaviour.

### 4.1 Geometry

After `s_div` and `member_chains` are known (1173–1201):

1. **Fork point** `F` = `_point_at(walk_pts, cum_dists, s_div)` (already computed,
   line 1213). The throat ends here — do **not** stop it short.

2. **Stop advancing/floating the branches.** Start each arm at
   `_point_at(w_k, c_k, s_div)` and keep its points beyond `s_div` (line 1241).
   Remove the corridor-clearance `while` loop (1252–1266) — the V-notch + inner walls,
   not a forward shove, are what prevent ramp-on-ramp overlap now.

3. **Build the throat V-notch polygon.** Take the throat's outer corners at `F` for
   each side and the two arms' **inner** edge points just past `F`; the far boundary
   runs inner-arm-1-edge → **V apex** (a point pulled back toward the portal along the
   throat centre, between the two inner edges) → inner-arm-2-edge. Emit as a
   `ROLE_TUNNEL_RAMP` BuiltShape carrying **`node_altitudes`** (not
   `altitude_high/low`): set each vertex's altitude by its position along the
   throat→arm transition so the throat surface meets arm-1's near edge at arm-1's
   start elevation and arm-2's near edge at arm-2's start elevation (both ≈ `e_div`
   today, but `node_altitudes` makes it correct if they differ). The apex altitude =
   `e_div`. This is the "bridge between the sloping rects" piece.

4. **Trace the wall around the whole Y.** Today walls are emitted per-arm as L/R
   offset pairs inside `_emit_chain` (1074–1104). Add the **inner-V wall**: from `F`,
   walk each arm's *inner* edge outward emitting wall segments at `apt_elev` under the
   same `wall_thresh` cutoff, so the two inner walls form the median and stop where
   each arm's ramp reaches DEM. The outer-edge walls already trace arms 1 and 2; the
   throat cap wall already closes the portal end — together they enclose the Y.

5. **No crotch pavement.** Emit nothing for the region inside the inner V. (Removing
   the branch-advance loop is what previously tempted a fill; with the V-notch throat
   the gap is intentional terrain, not a hole.)

### 4.2 Where the `node_altitudes` throat hooks in

`BuiltShape(node_altitudes=[…])` is consumed directly by `layout.to_osm`
(`layout.py:567,588,960` — it emits a `node_altitudes` tag) and by the solver's
non-planar handling (`unified_jacobi.py:3335,3564` treat `node_altitudes` shapes
"all-pair, like a junction"). So a throat emitted with `node_altitudes` is handled by
the existing junction machinery with **no solver change** — exactly the "sloping rect
+ junction" parallel the user drew.

### 4.3 Elevation

No new solver pass. Arms keep `altitude_low/high`; throat uses `node_altitudes` whose
values are derived from the same per-arm linear interpolation already in `_emit_chain`
(arm near-end = `e_div`). Walls are flat at `apt_elev` as today. The user explicitly
did not re-solve elevations and the observed values are already continuous at the fork.

## 5. Gating, scope, and risk

* **Gate** behind a config flag (team convention — e.g. `TUNNEL_FORK_THROAT`
  in `config.py`, default ON, OFF = current Y-split byte-identical). This is the only
  way to keep CYXY/SPJC/HECA fixtures provably unchanged.
* **Touch only the fork path.** Parallel-bores clusters (`s_div is None`, the SPJC
  divided highways) must stay byte-identical — guard the new code inside
  `if s_div is not None:` and behind the gate.
* **Self-overlap test.** `test_no_self_overlap` is the guardrail: throat, arms and
  inner-V walls must abut, not overlap. Keep the existing `wall_gap_m` clearance
  between ramp and wall (the throat-at-cap pattern, lines 955–964); the throat's
  `node_altitudes` ring must share the arms' near-edge vertices so there is no T-seam.
* **`node_altitudes` length invariant.** `len(node_altitudes) == len(closed ring)`
  including the closing repeat (`layout.py:283`) — get this wrong and `to_osm` /
  the solver mis-index the throat.

## 6. Validation protocol

1. Build KPHL standalone (cwd = repo root — single-airport builds must run from the
   repo root or the OSM comes out empty; see memory `hangar_pads_building_role`):
   `venv/bin/python` → `build_airport_pavement("KPHL", xplane_root())` →
   `to_osm("/tmp/KPHL.osm")`.
2. Re-render the north-portal crotch (reuse `/tmp` SVG harness from this session) and
   compare against `KPHL_auto_MODIFIED.patch.osm`: throat V-notch present, arms meet
   it with no hole, one continuous wall around the whole Y, **bare terrain (no
   pavement) between the arms**.
3. `tools/check_grade.py` on the patch — arms stay ≤ 4 %; the `node_altitudes` throat
   is junction-class (multi-direction slope) so confirm `check_grade` reads it per its
   per-vertex rule, not as a planar rect.
4. Full suite `venv/bin/python -m pytest tests/ -q` — expect SPJC/CYXY/HECA fixtures
   unchanged (gate-OFF byte-identical; gate-ON only KPHL geometry differs, and KPHL
   is not a compare-target fixture).
5. In-sim: load the tile, fly the RWY 26 approach, confirm the rail bore fork no
   longer shows a hole at the mouth.

## 7. Resolved by the user (2026-06-12)

* **V-notch throat, not a flat top.** The throat is a junction-style polygon with a
  V-notch carrying `node_altitudes` that bridges the two arm sloping-rects.
* **No paved nose, no crotch pavement.** The region between the arms is bare terrain;
  the median is just the two inner walls. (Supersedes the earlier "median nose fill".)
* **One wall traces the whole Y**, at DEM, terminating per-arm where the ramp reaches
  DEM.

## 8. Implementation outcome (BUILT 2026-06-12)

Gate `TUNNEL_FORK_THROAT` (config.py, default ON; OFF = legacy bare-crotch
byte-identical). New nested helper `_emit_fork_throat` in `bridges.py` inside the
`s_div is not None` branch of `_emit_tunnel_portals`. **Generalised to N arms** (the
user's call): arms sorted by angle about the fork point `F`; the throat is an
N-arm star-fan `node_altitudes` polygon with one V-notch per adjacent pair; the
apex of each notch is the back-extended intersection of the two facing inner edges
(natural fork point), falling back to a pulled-back midpoint. Walls trace every
perimeter edge that does not abut a ramp (outer fan edges + inner-V edges).

KPHL RWY 26 north portal (2-arm road+rail fork) verified, `PYTHONHASHSEED=0`:

* Throat bridges the bore to **both** arms; reflex V-notch confirmed; **crotch
  between arms is unpaved**. 1 notched throat ramp (>4 verts) emitted.
* `node_altitudes` all `= e_div` here (both arms land at the same bore-handoff
  elevation), so `to_osm` collapses it to a flat `altitude` tag — correct; the
  mechanism bridges per-vertex when arm starts differ.
* **Zero regressions vs gate-OFF baseline**: self-overlap pairs `0`; conformance
  T-junctions `5` / edge crossings `2` (both crossings pre-existing runway×apron);
  junctions `13 flat / 38 sloped` (identical); `check_grade` shows only the 8/1/1
  pre-existing junction violations, **zero tunnel violations**. Shapes 365→370
  (throat + 4 walls). Gate-OFF byte-identical to prior behaviour.
* Suite: CYXY self-overlap / SPJC compare-target / HECA+SPJC grade reds are
  **pre-existing** (reproduce with gate OFF — concurrent uncommitted solver WIP),
  not caused by this change.

### Rejected approach — global cluster wall-union (DO NOT REINTRODUCE)

The notch inner-V walls miter-**cross** the arms' side walls at the fork corners (2
extra edge crossings). First fix tried: `unary_union` of all cluster `tunnel_wall`
polygons (all flat at `apt_elev`) into a single Y-tracing wall. It eliminated the
crossings BUT **flattened all 51 KPHL junctions airport-wide to `altitude=3.0`** in
`to_osm` — a global consensus corruption that survived `PYTHONHASHSEED=0` and was
NOT spatial (nearest junction is 1156 m from the fork; merged walls span only
x∈[3451,3666]). Mechanism never fully traced; the merge changes shape count/order
and something in the `to_osm` per-node consensus pass then collapses every sloped
junction. **Shipped fix instead**: trim each notch-wall edge back from both ends by
`retaining_wall_width_m + wall_gap_m` so it can't overlap the abutting bore/arm wall
— crossings drop to the pre-existing 2, no global effect, tiny invisible gaps at
wall corners.

### Known minor / latent

* Small (~1.6 m) gaps where notch walls meet bore/arm walls (the trim) — invisible
  on a retaining wall.
* `deconflict_road_features` treats the `node_altitudes` throat as non-sloped and
  would `difference`-clip it if it overlapped airside pavement; at KPHL coverage is
  `0.00` so it never fires. If a future fork's throat overlaps airside, the clipped
  remainder would lose its `node_altitudes` (gets `altitude=None`) — revisit then.
