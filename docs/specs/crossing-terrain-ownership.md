# Spec: Crossing Terrain Ownership — one writer per crossing zone

**Status:** Draft for owner review · **Motivation (Noah, 2026-07-16):** the
same defect families around the KBNA crossings have now been "fixed"
across four rounds (5–8) and keep regressing in new forms. Each fix was
locally verified and correct; the regressions are INTERACTIONS. This spec
removes the interaction surface instead of patching it again.

## 1. The disease, from four rounds of measurements

Within a crossing's influence zone (a tunnel portal pair, an
object-sourced bridge corridor, or a causeway), terrain-valued geometry is
today produced by **eight independent writers** at **three different
pipeline phases**, coordinated only by pairwise exclusions and
after-the-fact welds:

| Writer | Phase | Avoids others via |
|---|---|---|
| mouth plates / trench floors | post-solve | — |
| crowns | post-solve | anchor-disk bites |
| collars | post-solve | forward sweep, mouth subtraction, road-lane subtraction |
| approach/ramp chains | post-solve | keep-outs, approach registry |
| deck-lip weld strips | post-solve | trench part capture |
| adjacent-ground bands | construct pre-solve, emit post-solve | standoff block, lane union, zone keep-out |
| runway-end skirts | PRE-solve (gated) or finalize | static block, lane clip (no-op pre-solve — round-8 finding) |
| legacy clearance / charter strips | emit | own charter scope |

Five separate exclusion systems reconstruct "the crossing" independently
(`_classifier_owned_crossing_union`, `road_lane_exclusion_union`,
`_tunnel_ramp_standoff_block`, the skirt static block, per-chain
keep-outs), each seeing only the pieces that exist at ITS phase. The
defect catalog of rounds 5–8 maps 1:1 onto this table: the no-op skirt
clip (phase mismatch), band 1355 on the road (exclusion reach), collar
remnant and lateral cliffs (subtraction blind spots), crown
inconsistency (independent DEM sampling), deck gaps (plate vs pavement
cut both derived separately from the deck box), missing bands over the
buried roof (mask over-reach).

**Every one of these was a coordination failure, not a valuation failure.**
The values each writer produces are individually lawful.

## 2. The rule

> Inside a crossing's influence zone, exactly ONE assembly builds ALL
> terrain-valued geometry, at ONE phase, from ONE height model. Everything
> else — bands, skirts, clearance — never enters the zone at all.

Concretely, a new `CrossingTerrainAssembly` (module
`src/auto_patch/crossing_terrain.py`) per recognized crossing:

1. **One influence zone** (the polygon everything else honors): the union
   of deck boxes, portal footprints, ramp corridors (mapped road
   corridor at mapped width, portal-to-portal buried span), plus the
   collar reach ring. Published on the layout at classification time
   (pre-solve), replacing all five exclusion systems FOR CROSSING
   PURPOSES: the band march, the skirt emitter, and clearance take this
   single zone as a hard keep-out — and so does ``gap_fill_spine``
   (round-8 finding: gap-fill strips are a FOURTH corridor consumer that
   never clipped; one buries a tunnel=yes road at 36.1106,-86.6834). (The buried-roof exception lives
   INSIDE the assembly: it emits nothing over the buried span, and the
   zone there is only the road bore — so pavement bands march the roof
   normally, which is the round-6/7 ruling, now by construction.)
2. **One height model** per crossing, built post-solve when all inputs
   exist: the road profile (approach-chain law), the plate/floor levels,
   the portal objects' full footprints and drivable widths, and the
   surrounding TARGET surface — solved pavement edge values where the
   zone borders pavement, inset-DEM terrain elsewhere, both sampled once
   and cached on the assembly (this kills the crown inconsistency class:
   crowns, collars, walls, and flank transitions all read the SAME
   samples).
3. **All pieces cut from that model**: mouth plates, crowns, collars
   (transition = the model's blend, not a per-piece rule), trench walls,
   approach flanks, deck-lip overlaps, and the zone-boundary ring that
   welds to whatever the zone borders (pavement edge values verbatim —
   the weld IS the shared boundary vertices, the repo's standing
   mechanism). Piece emission order inside the assembly is irrelevant —
   they are projections of one surface, so they cannot disagree.
4. **One audit**: the existing bridge-audit invariants keep their
   numbers, but invariants that exist to referee inter-writer fights
   (approach-vs-approach registry, collar-flank rules, deck-lip overlap)
   become internal assertions of the assembly; the external audit checks
   the assembly's boundary contract: zone boundary welds to neighbors
   (≤0.05 m), no terrain step >2.5 m inside the zone except object-hidden
   faces and designed walls, nothing outside the assembly intersects the
   zone.

## 3. What this deletes (the simplify ruling's payoff)

The pairwise rules accumulated in rounds 4–8 become internal details or
disappear: the collar forward sweep and mouth-subtraction special cases,
the collar-reach mask addition, the deck-lip trench-part capture, the
approach same-chain registry exemption, the skirt lane clip, the band
lane clip + emit-time clip, the standoff block's buried-span carve-out.
`road_lanes.py` survives only as the mapped-corridor loader feeding the
zone. Net: fewer modules touching the crossing, and the emit-order
contract shrinks to "assembly runs after the solve; everything else
honors the zone."

## 4. Migration plan (each phase shippable; Noah tests between)

- **Phase 0 (landed rounds 5–8):** the current pairwise fixes stay as the
  working baseline; round-8's four fixes complete it.
- **Phase 1 — the zone.** Publish the influence zone at classification
  time; convert band march, skirts, clearance to consult it (delete
  their crossing-specific carve-outs). Small, verifiable: zone coverage
  probes + the existing audits must stay green; KBNA/SPJC/CYUL class
  airports as fixtures.
- **Phase 2 — the height model + portal pieces.** Move mouth/crown/
  collar/wall emission onto the model (approaches keep their chain
  emitter but read the model's road profile). This is the phase that
  retires the collar special cases. Audit boundary contract lands here.
- **Phase 3 — plates, deck lips, flanks; delete the referee rules.**
- Each phase: lead-session interface design; mechanical extraction
  delegable; acceptance = the audit battery + before/after patch probes
  at every round-5..8 coordinate (the regression corpus this arc built
  is the fixture set — the defects become the tests).

## 5. Open questions for the owner

1. Zone size: collar reach (10 m) beyond the footprints, or wider where
   the skirt interaction bit us (~the RESA overlap at 31)?
2. Phase 1 alone already fixes the recurring "X across the road" class
   permanently — worth shipping before Phase 2's bigger move?
3. The crossing assembly is also where per-crossing config (mapped-width
   overrides, portal object hints) would live if ever needed — name it in
   the tile config now or defer?

## 6. Solver-native end state (owner direction, 2026-07-16)

The assembly must be designed so its height model can be ABSORBED INTO THE
GRADE LAW rather than remain a post-solve emitter: multiple passes that
mutate already-solved geometry are themselves the breakage risk this spec
exists to remove (the arc's weld/adopt/clip patches are all mutation
passes). This is the same trajectory slice B already proved for bands
(B3: band rows became solver variables under law constraints).

Design consequences, binding on Phases 1–3:
- The height model's values must be expressible as LAW terms (road-profile
  law, plate levels, terrain envelope clamps, transition-slope caps), not
  imperative samples — so Phase 4 can register them as variables and
  constraints in the one solve, with the crossing pieces becoming pure
  projections of solved values, and emit doing NO valuation at all.
- The influence zone is published PRE-solve (Phase 1 already does this) so
  the solve can own the zone's coupling to neighboring pavement (the
  boundary weld becomes shared solver nodes, not a post-emit adoption).
- Anything in Phases 2–3 that would stamp a value after the solve must be
  written as "evaluate law expression" so the later move is mechanical.
