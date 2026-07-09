# Identifying structures in an OBJ8 bake without tearing them apart

**Status:** designed and measured against the eight co-anchored KCLT bakes, 2026-07-08. Not yet built.
**Supersedes:** the `group_components_into_structures(gap_metres=2.0)` heuristic in
`tools/obj8_geometry.py:220` and the plan's §8.3 gap table.
**Feeds:** `docs/dsf_object_integration_spec.md` — workstreams W2 and W4, invariants I-2 and I-6.

This answers the question the plan left as a tuning knob: *what is a structure?* It turns out the
question has an exact answer, and the 2 m gap that the prototype uses is a coincidence — it works
only because it happens to be coarser than the correct partition.

---

## 1. The result first

Measured over the pooled eight bakes: 184,280 vertices, 3,193 triangle-connectivity parts, 1,173 of
them ground-touching. Residual is the plan's per-**component** metric,
`|(terrain(anchor) + min_y(part) + delta) − ground_under(part)|`. "Tear" is the vertical separation
`|delta(a) − delta(b))|` opened up between two parts that are in contact.

| partition | structures | residual p50 | p90 | max | >0.5 m | tears |
|---|---|---|---|---|---|---|
| no correction | – | 2.84 | 9.31 | 15.07 | 1057/1173 | – |
| 2D bbox gap 0.5 m | 116 | 0.11 | 0.74 | 8.08 | 217 | 0 |
| **2D bbox gap 2 m — the prototype** | **105** | **0.14** | **0.83** | **8.08** | **254** | **0** |
| 2D bbox gap 5 m | 87 | 0.19 | 0.96 | 8.26 | 338 | 0 |
| 2D bbox gap 20 m | 50 | 0.45 | 1.56 | 9.80 | 540 | 0 |
| vertex-contact 5 cm | 2720 | 0.00 | 0.14 | 0.78 | 2 | **4453 tears, max 11.39 m** |
| 3D axis-aligned bounding box (AABB) contact 25 cm | 133 | 0.11 | 0.74 | 8.08 | 216 | 0 |
| **3D AABB + surface narrow phase** | **216** | **0.08** | **0.65** | **7.18** | **166** | **0** |

Three things fall out.

1. **Vertex contact is not contact.** These bakes are triangle soup. A wall and the roof it holds up
   abut *without sharing a vertex*. Partitioning on vertex-connectivity gives a beautiful residual
   (p50 0.00 m) and rips 4,453 abutments open by up to 11.39 m. This is the trap, and it is the
   partition an unwary implementer will reach for first because it is the one that provably never
   splits a triangle.

2. **The prototype's 2 m gap leaves fidelity on the table.** 254 parts over 0.5 m, against 166 for the
   correct partition. It is not *wrong* — it tears nothing — it is merely coarse.

3. **The correct partition is not a tuning choice.** See §2. It is the connected components of the
   contact graph, and the only free parameter is ε, a modelling tolerance with a physical meaning.

---

## 2. Why there is an exact answer

### 2.1 The assembly-preservation theorem

Let structure `S` receive, for each object `O` contributing geometry to it,

```
delta(S, O) = ground_under(centroid(S)) − ground_under(anchor(O))
```

X-Plane renders a vertex `v` of object `O` at absolute elevation `terrain(anchor(O)) + v.y`. After the
bake, `v.y` has become `v.y + delta(S, O)`, so it renders at

```
terrain(anchor(O)) + v.y + ground_under(S) − terrain(anchor(O))  =  ground_under(S) + v.y
```

The anchor cancels. Therefore for **any** two vertices `u, v` of the same structure, drawn from **any**
two objects with **any** two anchors and **any** two headings:

```
rendered_y(u) − rendered_y(v)  =  u.y − v.y
```

and `x`, `z` are never touched. **Every structure moves as a rigid body under pure vertical
translation, and its internal assembly is preserved exactly.** This is why the y-bake is safe across
the texture-page split, where a building's walls live in `001` and its roof in `007`.

It is also the precise sense in which §2.4 of the integration spec matters: collapse `delta(S, O)` to a
single per-structure `delta(S)` and the anchor stops cancelling, so two objects with different anchors
shear apart by `terrain(anchor(O₁)) − terrain(anchor(O₂))`.

### 2.2 The corollary that determines the algorithm

Since each structure is internally exact, **all distortion lives on structure boundaries.** Two parts
`a`, `b` in different structures separate vertically by exactly `|delta(S(a)) − delta(S(b))|`.

Define the contact relation: `a ~ b` iff the surfaces of `a` and `b` come within ε.

* A partition tears nothing **iff** every contacting pair lies in the same structure — i.e. iff the
  partition is coarser than or equal to the connected components of `~`.
* Residual improves monotonically as the partition refines: a finer structure has a tighter ground
  sample.

Both conditions bind in opposite directions, so:

> **The optimal structure partition is exactly the set of connected components of the contact graph.**

There is no gap to tune between 0.5 m and 20 m. Every value in that range is a *coarsening* of the same
answer, paying fidelity for nothing. The table in §1 is that statement in numbers: 116 → 105 → 87 → 50
structures, residual monotonically worse, tears zero throughout.

The single parameter ε is not "how far apart are two buildings" — it is "how large a gap did the
modeller leave between a wall and its roof". It is a manufacturing tolerance.

### 2.3 ε: a knee, not a plateau *(corrected 2026-07-08 by the workstream-W2 audit)*

The original probe measured AABB contact only: ε = 0.02 m → 135, ε = 0.05 m → 135, ε = 0.25 m → 133 —
apparently flat. **That plateau was an artifact**: broad-phase over-merging absorbed the sensitivity.
Under the full narrow phase the count is genuinely ε-sensitive
(`tools/obj8_partition_audit.py`, KCLT):

| ε | 0.02 | 0.05 | 0.10 | 0.25 | 0.50 | 1.00 |
|---|---|---|---|---|---|---|
| structures | 890 | 571 | 387 | **220** | 198 | 175 |

Hard tears are zero throughout (a coarser ε only merges). **Default ε = 0.25 m** stands as the knee
of that curve — it flattens sharply beyond it — and remains comfortably above float noise and below
any real building gap. The V3 induced-separation report is the instrument for judging whether a
smaller ε opens visible gaps at real abutments; consult it before tuning, and answer §6's open
question 2 before trusting ε below 0.10.

---

## 3. The algorithm

Three levels, and the user's phrase "all their various parts" is level 1.

```
PART       triangle-connectivity class after position welding     3,193 at KCLT
  ↓  contact graph (ε)
STRUCTURE  the rigid unit; receives one delta per contributing      216 at KCLT
           object, per the theorem
  ↓  support relation (only for structures with no ground contact)
INHERITED  rooftop clutter, canopies, jetbridges that the contact
           graph did not attach to anything
```

### Step 0 — Pool the objects that can touch

**Replace anchor-group discovery with world-AABB overlap.** Contact is a world-space property; anchor
proximity is not. Two objects with a shared anchor may not touch; two objects with anchors 5 km apart
may. Pool the *correction candidates* (definitions passing the reach filter) whose placed world AABBs
overlap, transitively.

Small, correctly anchored objects — a light mast beside a terminal wall — are **not** candidates and
must not be pooled. X-Plane already places them right. Correcting the terminal moves it *towards* the
mast, not away.

### Step 1 — The frame: build the contact graph in *authored* space

Horizontal position comes from each object's own placement (heading + lon/lat), projected into **one**
local ENU frame centred on the group, so `cos(latitude)` scale drift does not accumulate across a
2.2 km sprawl. Vertical position is the **authored `v.y`**, *not* `terrain(anchor) + v.y`.

This is the load-bearing subtlety. The author assembled the parts against a common assumed-flat ground
plane; authored space is the frame in which they fit. Build the graph in *rendered* space and two
objects whose anchors sample terrain 0.3 m apart show a spurious 0.3 m gap — and you fail to detect
that the walls in `paredes` touch the roof in `techos`. Which is exactly the 41 co-anchored KCLT
terminal-layer objects, whose two anchors sit ~10 m apart.

By §2.1 the delta formula then reproduces the authored assembly on real terrain — and, as a free
bonus, repairs the pre-existing inter-anchor mismatch.

### Step 2 — Parts

Weld vertices at 1 mm (`VERTEX_WELD_DECIMALS = 3`), then triangle-adjacency union-find. Exporters
duplicate a position once per texture seam or smoothing group; skip the weld and a single wall
shatters. This is `connected_components` in `tools/obj8_geometry.py:150` and it is already correct.

Two pre-merges before contact, both in the "when in doubt, merge" direction:

* **`ANIM_begin`…`ANIM_end`**: fuse each animation block's geometry into a single part. A per-part
  delta inside an animation block breaks its rotation pivot. (Default remains refuse-and-report until
  `O4_DSF_OBJECT_ALLOW_ANIM`.)
* **`ATTR_LOD`**: LOD copies are spatially coincident and will merge on contact anyway. Compute the
  structure's area-weighted centroid from the **first LOD bucket only**, or the duplicated area drags
  it. Apply the delta to every bucket.

Draped triangles are excluded entirely — they conform to the terrain mesh (`ATTR_draped`). A vertex
shared between a draped and a solid triangle means the object cannot be corrected: refuse and report.

### Step 3 — Contact graph, broad then narrow

**Broad phase — 3D AABB gap ≤ ε**, over a uniform grid keyed on XZ. Note `AABB_gap ≤ surface_gap`
always, so AABB contact is a **superset** of true contact. Merging on it is therefore **sound — it can
never tear** — and merely *incomplete*: two interlocking L-shaped parts have AABB gap 0 while their
surfaces are metres apart, and get spuriously fused. Safe direction. 8,133 candidate pairs at KCLT.

Use the **3D** box, not the 2D one the prototype uses. The 2D box merges a jetbridge at y = 6 m with a
shed beneath it.

**Narrow phase — surface distance ≤ ε**, pruning the broad-phase edges. Vertex-to-triangle distance in
both directions, early-exit at ε, per-part BVH or a cKDTree over triangle centroids. At KCLT this
prunes 3,535 of 8,133 pairs (43%), lifts the partition from 133 to 216 structures, and takes the
residual from p50 0.11 / 166-over-0.5 m to p50 0.08 / 166-over-0.5 m with max 8.08 → 7.18 m.

**A pair whose contact cannot be *proved absent* keeps its edge.** Budget exhaustion, degenerate
triangles, numerical doubt — all merge. Tearing is the unrecoverable failure; over-merging costs
centimetres of residual.

*(Naive edge-edge crossings without vertex proximity are missed by vertex-to-triangle. For abutting
building parts this configuration implies interpenetration, which the vertex tests catch. Say so in
the docstring rather than pretending otherwise.)*

**Structure = connected component of the pruned graph.**

### Step 4 — Ground contact and inheritance

2,020 of 3,193 parts have `min_y > 0.5 m`. Most attach to their supporting roof through the contact
graph and need no special handling — **the contact graph subsumes the plan's §8.4 elevated-structure
rule for everything that is actually attached.**

What remains is a *structure* with no ground-touching part: floating clutter, or clutter the modeller
left a 30 cm gap beneath. It gets no terrain sample of its own. It inherits the delta of the
ground-touching structure whose XZ bounding box contains its centroid, else the nearest by centroid
distance. Report every inheritance.

### Step 5 — Refuse, don't guess

Contact is transitive, and transitivity is dangerous. A perimeter fence touching a hangar and running
2 km chains the whole airfield into one structure.

Detect it not by diameter but by the quantity that actually matters: **the span of `ground_under()`
across the structure's ground-touching parts.** One rigid delta cannot seat a structure whose ground
varies by more than a tolerance. At KCLT exactly one structure fails: the genuinely connected 513 m
terminal complex, ground span 8.08 m, which is the entire residual tail.

**Do not split it. Refuse it, and report that it needs a building pad.** That is precisely the Phase 1
hand-off the integration spec's §7.3 predicts, and it is the honest answer: a rigid body cannot follow
a slope.

Two escape hatches, both loud, neither default:

* **Hinge detection.** If deleting one contact edge splits a large structure into two compact ones,
  that edge is a jetbridge, a canopy, or a fence link. Report it with the separation `|Δδ|` that
  cutting it would open.
* **Weak-contact cut.** Rank edges by contact-patch area. A fence post touching a hangar at one vertex
  is a weak contact. Under `--cut-weak-contacts`, cut below a threshold and print every cut with its
  induced separation. Never silently.

---

## 4. Verification — independent of the algorithm

The partition is where the bugs will be, so none of these checks may be derived from it.

**V1 — hard-tear invariant.** Hash every vertex in the pooled geometry by quantized world position.
Any two coincident vertices must land at the same **post-bake rendered elevation**:
`ground(anchor(O)) + v.y_after` equal across the pair. Do **not** compare deltas — coincident vertices
from objects with different anchors *must* receive different deltas (spec §2.4); a delta-equality check
fails a correct implementation and passes the per-structure-delta bug it exists to catch. Catches every
tearing bug at every level in one pass, using nothing but the output.

**V2 — seam invariant.** For every part pair with surface gap ≤ ε, deltas must be equal. Zero by
construction if the partition is the contact graph's components — which makes it a regression test on
the *implementation*, not on the theory. That is what you want.

**V3 — induced-separation report.** For part pairs with surface gap in `(ε, ε_probe]` — say up to
0.5 m — report the distribution of `|Δδ|`. This is the "how far did we open up the gaps the modeller
deliberately left" number. It has no zero to hit; ship it in the audit output and watch it.

**V4 — rigid-motion check.** Per structure, per object: `x` and `z` tokens byte-identical, `y` shifted
by exactly one scalar. Line count unchanged.

**V5 — per-component residual, never per-structure-centroid.** The plan's §8.3 warning stands. The
centroid check is tautological — it proves only that the offset was applied. Its author reported a
flawless `0.000000 m` while the audit showed `001`/`002`/`003` more than 7 m off.

---

## 5. What this changes in the integration spec

| spec item | change |
|---|---|
| §3.1 `group_components_into_structures` | superseded by `contact_graph` + `connected_structures`; keep the old function only until W8 signs off |
| §3.3 `discover_anchor_groups` | **replace** anchor proximity + heading with world-AABB overlap of candidate placements (§3, step 0) |
| §3.3 `partition_structures` | build in authored space (§3, step 1); broad+narrow contact (§3, step 3) |
| §3.3 `Structure` | add `ground_span_m`, `is_correctable` (false ⇒ needs a pad), `inherited_from` |
| I-2 | unchanged in spirit; the "common world ENU frame" is now specified as **authored space** |
| I-6 | **rewrite.** Not "gap default 2 m" but "structures are the connected components of the ε-contact graph; ε = 0.25 m, on a measured plateau" |
| I-8 | narrowed: the contact graph subsumes attached clutter; the support rule applies only to structures with no ground-touching part |
| new I-19 | a structure whose ground span exceeds tolerance is refused and reported as needing a pad, never split |
| new I-20 | a contact pair that cannot be *proved absent* keeps its edge (merge on doubt) |
| W4 acceptance | add V1–V4; add the KCLT Pareto table as a regression fixture |
| W8 / M2 | the 7.18 m residual max is now attributable to **one** structure with an 8.08 m ground span — M2 becomes a sharp prediction: flatten that pad and the tail disappears |

**Performance.** The narrow phase as prototyped runs 41 s for 8 objects in pure-Python triangle loops.
Per-part BVH with early-exit at ε should bring it to seconds. It runs once per airport, post-mesh, so
this is a nuisance, not a blocker — but measure before shipping, and cache by `(resource, mtime)`.

---

## 6. Open, and worth measuring

1. **ANSWERED 2026-07-08 (workstream W2): the plateau does not survive the narrow phase.** See §2.3 —
   the count is genuinely ε-sensitive (0.02 m → 890, 0.25 m → 220, 1.0 m → 175); 0.25 m is the knee.
2. **V3's tail.** Nobody has looked at how much the parts separated by 0.25–0.5 m move relative to each
   other. If it is large, ε is too small. Now more pressing given answer 1.
3. **Does the contact graph generalise off this pack?** Everything here is one Nimbus bake. The
   audit's first target is the HECA Tai Models pack (spec amendment A11: 341 definitions, 189 sharing
   one anchor, material-split bakes, base errors to +38 m), then KDFW, CYUL.
4. **Hinge frequency.** No structure at KCLT needed a weak-contact cut. That is one pack, and jetbridges
   are usually separate placements. Do not build the cut machinery until a pack demands it — but do
   build the *detection*, because a silent 2 km chain is the failure that would ship.

---

## 7. Reproduction

The measurements in §1 come from two throwaway probes, both against `*.anchor_bak` originals so the
live baked pack is untouched:

* fidelity/tearing Pareto over 2D-bbox, 3D-bbox and vertex-contact partitions
* narrow-phase prune rate and its effect on the partition

They should be promoted to `tools/obj8_partition_audit.py` as part of W4, with a proper docstring —
the ε plateau and the Pareto table need to be re-derivable on every pack, not just this one.
