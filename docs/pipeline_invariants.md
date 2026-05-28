# auto_patch — Pipeline geometry & elevation invariants

Working spec agreed during the session-51 single-solve refactor. The pipeline
is built and tested against THESE invariants; tests that encode 2-solve-era
artifacts instead of an invariant here are candidates for redefinition.

## A. Pavement structure

1. Every paved metre belongs to exactly one shape:
   `runway ∪ terminal ∪ taxi_rects ∪ junctions ∪ aprons = pav_union`.
   No overlap, no gap.
2. Junctions = `pav_union − (runways ∪ terminals ∪ taxi_rects)`; aprons =
   junctions where the boundary deviates > 55 m from any centerline.
3. No shape crosses an integer lat/lon tile boundary (10 m cut gap).
4. Every junction / apron vertex lies inside (or on the boundary of)
   `pav_union`, EXCEPT:
   * vertices shared with a runway / taxi rect / terminal edge (those
     anchors legitimately extend past row-110 — e.g. runway stopways);
   * tile-cut seam vertices (positioned ~half_width off the integer
     line by `tile_cut`);
   * vertices shared with a `boundary_dem_bridge` polygon (bridges are
     the transition strip from pavement to DEM terrain — they connect
     ribbon to junction across the boundary by design).
5. Every junction / apron vertex is shared with at least one neighbouring
   shape vertex.

## B. Taxi rect shape

6. Every taxi rect is 4 corners — two short (cross) edges and two long
   (sloping) edges — with `source_axis` parallel to the long edges.
7. **Ortho4XP corner-order contract**: edges `(0, 1)` and `(2, 3)` are the
   long (sloping) edges; `(1, 2)` and `(3, 0)` are the short (cross) edges.
8. **Phase-1 absorption** drops fully-embedded primary_parallels (both long
   edges inside apt.dat pavement) and partial-clips half-embedded ones —
   so no rect emitted has its long edges buried in surrounding pavement.

## C. Rect ↔ neighbour vertex sharing

9.  No junction / apron vertex on a rect's long (sloping) edge interior.
    Only the two endpoint corners may be shared.
10. On a rect's short (cross) edge: only the two corners are legal shared
    vertices (1 : 1 sharing).
11. Rect corners are exact vertices in the adjacent junction / apron polygon
    along the shared boundary.

## D. Runway interface

12. The runway segmenter creates the runway corner nodes; every junction
    vertex on the runway boundary coincides exactly with a runway corner
    (1 : 1 node sharing).
13. No runway-rewrite pass should mutate the runway-junction interface —
    the segmenter places nodes correctly upstream so `junctions = union − rects`
    naturally meets them.

## E. Seam (tile boundary)

14. Slice-edge vertices on both sides of the seam are DEM-pinned (HARD
    anchors) so adjacent tiles compute the same altitude there.
15. Runway FAA profile is reconciled with seam DEM via
    `redistribute_runway_profile`.

## F. Conformance (mesh quality)

16. Adjacent shapes share identical vertex sequences along common edges
    (no T-junctions → no Triangle4XP slivers).
17. Coincident vertices across shapes are exactly equal (no sub-tol drift).
18. The airport boundary ribbon lies inside the row-130 line; pavement
    clips to its inner edge.

## G. Grade (within-shape; ≤ 1.5 % unless noted)

19. Taxi rect: ≤ 1.5 % along `source_axis`; flat cross-section.
20. Junction / apron: ≤ 1.5 % all-pair Euclidean within the polygon.
21. Terminal: **strictly flat, single `altitude` tag**.
22. Runway: FAA vertical profile (1.5 % body, 0.8 % ends).
23. Bridges / tunnels / groundside / clearance: own caps, emitted
    post-solve.

## H. Authority hierarchy & solver freedom

24. **Tile-seam vertices**: HARD.
25. **Runway threshold-containing segments**: HARD. Other runway segments
    may shift slightly within FAA bounds but are effectively settled by
    `redistribute_runway_profile` against the DEM, after which the rest of
    the pavement network adapts to meet the runway.
26. **Terminals**: move as a **whole unit** (single altitude); no per-node
    deviation.
27. **Sloping rects** (taxi rects, non-threshold runway sub-rects): solver
    may change `altitude_high` / `altitude_low` **independently** — raise /
    lower either end, or make flat. Cross-section perpendicular to
    `source_axis`.
28. **Junctions & aprons**: move as a whole shape (cascade picks the base
    altitude); individual nodes may be adjusted to maintain ≤ 1.5 % all-pair
    grade.
