"""Build the KRDM (Roberts Field, Redmond OR) passenger-terminal prototype.

Composes the terminal from researched facts (research/footprint.json,
research/facade.json) using tools/obj8_building_gen. Produces into
output/: KRDM_terminal.obj + KRDM_terminal.png (2048 atlas).

Layout is expressed in an "apron frame": origin at the southwest corner
of the 202 m apron-edge wall (world (-46.7, 85.8) relative to the OSM
footprint centroid), along = apron edge direction (bearing 60.4 true),
across positive toward the apron (bearing 150.4). In that frame the OSM
footprint decomposes into:
  * main body: a 32.7..147.4, c 0..-96 (with landside notches);
  * two narrow strips c 0..-6.5 at both ends (a 0..32.7, a 147.4..202.3)
    — the LOW covered boarding walkways, not full-height building;
  * a small bump at a 82..94, c -86..-96 — the landside entry vestibule.

Massing (from reference photos + facade facts):
  * main body: one-story base, white membrane parapet roof;
  * airside: two-story glazed departure-lounge box (shallow green shed
    roof, deep pale-fascia overhang, concrete piers) + cantilevered gate
    canopies along the concourse; low glazed walkway strips at each end
    under sloped green canopy roofs;
  * landside: three stepped sage-green ASYMMETRIC GABLES above the
    parapet (ridge near the groundside edge, short slope down to the
    curbside eave, long slope back toward the airside), the entry
    vestibule gable in the same parallel-ridge pattern, basalt stone
    piers, and a continuous curbside canopy.
The detached ATC tower is intentionally NOT part of this object.

Run: venv/bin/python prototypes/KRDM_terminal/build_krdm_terminal.py
Build-time impact: none — not part of the tile build pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROTOTYPE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PROTOTYPE_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from obj8_building_gen import (  # noqa: E402
    AtlasPainter,
    AtlasRect,
    Frame,
    Mesh,
    box,
    gable_roof,
    oriented_slab,
    polygon_cap,
    prism,
    shed_roof,
    stack_bands,
    wall_run,
    write_obj8,
)

# ---------------------------------------------------------------------------
# Apron frame and researched layout (see module docstring)
# ---------------------------------------------------------------------------

APRON_FRAME = Frame(origin_x=-46.7, origin_z=85.8, bearing_degrees=60.4)
APRON_LENGTH = 202.3

# Main full-height body, (along, across) in the apron frame, traced from
# the OSM footprint vertex table (see git history / research notes).
MAIN_RING_FRAME = [
    (32.7, 0.0), (147.4, 0.0), (147.4, -6.6), (147.8, -64.1), (148.0, -88.7),
    (93.9, -88.5), (94.3, -95.9), (88.9, -95.9), (82.5, -95.7), (82.4, -86.1),
    (23.4, -85.9), (22.7, -56.3), (46.0, -57.1), (46.7, -43.1), (32.3, -43.7),
    (32.7, -6.4),
]
WALKWAY_STRIPS = [  # (along range, across range) of the low boarding walkways
    ((0.0, 32.7), (-6.4, 0.0)),
    ((147.4, APRON_LENGTH), (-6.6, 0.0)),
]

PARAPET_Y = 6.5
WALKWAY_WALL_TOP_Y = 4.4
WALKWAY_ROOF_HIGH_Y = 5.5
WALKWAY_ROOF_LOW_Y = 4.7

LOUNGE_ALONG = (38.0, 92.0)
LOUNGE_ACROSS = (-18.0, 0.4)
LOUNGE_TOP_Y = 11.9
LOUNGE_ROOF_HIGH_Y = 13.0
LOUNGE_ROOF_LOW_Y = 11.5
LOUNGE_OVERHANG = 2.2
PIER_SPACING = 5.4

CANOPY_FIRST_ALONG = 104.0
CANOPY_SPACING = 17.0
CANOPY_HALF_ALONG = 3.4

# Landside stepped roof pieces: asymmetric gables — ridge pulled toward
# the groundside (NW) edge, short steep slope dropping to the NW eave,
# long slope running back down toward the airside (reference-mesh
# end-elevation + owner correction). (across range, eave_y, ridge_y,
# along range).
LANDSIDE_GABLES = [
    ((-96.0, -84.0), 5.6, 8.2, (25.0, 92.0)),
    ((-86.0, -70.0), 6.6, 9.4, (28.0, 88.0)),
    ((-72.0, -56.0), 8.0, 10.6, (32.0, 84.0)),
]
RIDGE_ACROSS_FRACTION = 0.28  # ridge sits ~28% in from the NW edge
ENTRY_ALONG_CENTER = 88.5
ENTRY_ACROSS = -95.9

# Colors (facade.json samples, with one judgment override: the airside
# fascia/soffit trim reads pale seafoam in the 2010 ramp photo, not the
# dark sage the sampler returned).
TAN = (195, 177, 145)
TAN_DARK = (168, 150, 118)
STONE = (90, 77, 64)
MORTAR = (58, 50, 42)
ROOF_GREEN = (107, 120, 110)
ROOF_SEAM = (80, 92, 84)
FASCIA_PALE = (166, 190, 175)
GLASS_DARK = (44, 56, 69)
GLASS_LIGHT = (86, 108, 122)
MULLION = (20, 22, 24)
CONCRETE = (156, 150, 139)
CONCRETE_SHADE = (128, 122, 112)
GLULAM = (168, 124, 72)
GLULAM_DARK = (128, 90, 48)
MEMBRANE = (226, 225, 219)
CHARCOAL_WALL = (64, 59, 53)
CHARCOAL_RIB = (48, 44, 40)
DARK_ROOF = (56, 51, 45)
DARK_FASCIA = (46, 42, 38)
FIN_AMBER = (196, 154, 94)
FIN_AMBER_SHADOW = (152, 113, 63)
GLASS_EXPANSION = (30, 36, 42)
SOLAR_PANEL = (38, 43, 54)
SOLAR_FRAME = (66, 70, 78)

ATLAS_SIZE = 2048

BANDS = stack_bands(
    [
        ("wall_tan", 12.0, 6.0),
        ("glaze_lounge", 5.6, 11.0),
        ("glaze_concourse", 6.0, 5.5),
        ("roof_green", 6.0, 16.0),
        ("concrete", 3.0, 11.0),
        ("stone", 3.0, 5.0),
        ("fin_screen", 24.0, 8.0),   # expansion upper level: timber fins + glass
        ("dark_metal", 6.0, 5.5),    # expansion ground level: charcoal ribbed
        ("dark_roof", 6.0, 26.0),    # expansion roof: charcoal + solar array
    ],
    v_bottom=0.0,
    v_top=0.78,
)
RECTS = {
    "white_roof": AtlasRect("white_roof", 0.00, 0.78, 0.35, 1.00),
    "fascia_pale": AtlasRect("fascia_pale", 0.35, 0.78, 0.47, 1.00),
    "glulam": AtlasRect("glulam", 0.47, 0.78, 0.60, 1.00),
    "gable_end": AtlasRect("gable_end", 0.60, 0.78, 0.74, 1.00),
    "green_cap": AtlasRect("green_cap", 0.74, 0.78, 0.87, 1.00),
    "dark_fascia": AtlasRect("dark_fascia", 0.87, 0.78, 1.00, 1.00),
}

# --- Expansion layout (2028, apron frame; scaled from TACP figs 7-4/7-5
#     at 0.20 m/px via the known 202.3 m existing apron edge, massing per
#     flyrdm.com 2025 renderings) ---
CONCOURSE_ALONG = (-135.0, 30.0)
CONCOURSE_ACROSS = (-22.0, 1.5)
CONCOURSE_GROUND_TOP_Y = 4.6
CONCOURSE_ROOF_LOW_Y = 12.7
CONCOURSE_ROOF_HIGH_Y = 13.5
# Gates 5-11 (design animation signage): seven bridges on the concourse.
BRIDGE_ALONG_POSITIONS = (24.0, -2.0, -28.0, -54.0, -80.0, -106.0, -130.0)
PROCESSOR_ALONG = (-24.0, 36.0)
PROCESSOR_ACROSS = (-52.0, -20.5)
PROCESSOR_TOP_Y = 9.2
JUNCTION_ALONG = (30.0, 38.0)


def paint_atlas(path: Path) -> None:
    painter = AtlasPainter(ATLAS_SIZE, background=(120, 120, 120))

    wall = BANDS["wall_tan"]
    painter.flat(wall, TAN, noise_amplitude=5, seed=11)
    painter.window_row(
        wall, sill_meters=1.1, head_meters=3.1, window_width_meters=1.8,
        spacing_meters=3.0, glass_color=GLASS_LIGHT, frame_color=MULLION,
        mullions_per_window=1,
    )
    painter.masonry(
        wall, STONE, MORTAR, course_height_meters=0.35,
        stone_width_meters=0.75, top_height_meters=0.9, seed=12,
    )

    lounge = BANDS["glaze_lounge"]
    painter.glazing_grid(
        lounge, (38, 48, 60), MULLION, mullion_spacing_meters=1.4,
        transom_heights_meters=(4.9, 5.4), glass_gradient_top=(58, 72, 86),
    )

    concourse = BANDS["glaze_concourse"]
    painter.glazing_grid(
        concourse, (38, 48, 60), MULLION, mullion_spacing_meters=1.5,
        transom_heights_meters=(1.0,), glass_gradient_top=(56, 70, 82),
    )
    # Painted concrete pier stripe once per tile for bay rhythm.
    left, top, right, bottom = concourse.pixel_rect(ATLAS_SIZE)
    pier_width = round(0.55 / concourse.tile_width_meters * ATLAS_SIZE)
    painter.draw.rectangle((left, top, left + pier_width, bottom - 1), fill=CONCRETE)
    painter.draw.rectangle(
        (left + pier_width - 2, top, left + pier_width, bottom - 1),
        fill=CONCRETE_SHADE,
    )

    painter.standing_seam(
        BANDS["roof_green"], ROOF_GREEN, ROOF_SEAM, seam_spacing_meters=0.45, seed=13
    )
    painter.flat(BANDS["concrete"], CONCRETE, noise_amplitude=6, seed=14)
    painter.masonry(
        BANDS["stone"], STONE, MORTAR, course_height_meters=0.35,
        stone_width_meters=0.7, seed=15,
    )

    painter.flat(RECTS["white_roof"], MEMBRANE, noise_amplitude=4, seed=16)
    painter.flat(RECTS["fascia_pale"], FASCIA_PALE, noise_amplitude=3, seed=17)
    painter.flat(RECTS["glulam"], GLULAM, noise_amplitude=8, seed=18)
    painter.flat(RECTS["gable_end"], TAN_DARK, noise_amplitude=5, seed=19)
    gable_left, gable_top, gable_right, _ = RECTS["gable_end"].pixel_rect(ATLAS_SIZE)
    painter.draw.rectangle(
        (gable_left, gable_top, gable_right - 1, gable_top + 14), fill=GLULAM_DARK
    )
    painter.flat(RECTS["green_cap"], ROOF_GREEN, noise_amplitude=5, seed=20)
    painter.flat(RECTS["dark_fascia"], DARK_FASCIA, noise_amplitude=3, seed=21)

    # --- Expansion surfaces ---
    import math as _math
    fins = BANDS["fin_screen"]
    painter.vertical_gradient(fins, GLASS_EXPANSION, (44, 52, 60))
    fin_left, fin_top, fin_right, fin_bottom = fins.pixel_rect(ATLAS_SIZE)
    fin_height_pixels = fin_bottom - fin_top
    fins_per_tile = 66
    fin_pitch = ATLAS_SIZE / fins_per_tile
    for k in range(fins_per_tile):
        # Slats HANG from the roof line; the Cascade silhouette is cut
        # into their bottom ends (owner correction). Tile-periodic sines.
        phase = 2.0 * _math.pi * k / fins_per_tile
        length_fraction = (
            0.62
            + 0.16 * _math.sin(3.0 * phase + 0.7)
            + 0.13 * _math.sin(7.0 * phase + 2.1)
            + 0.06 * _math.sin(13.0 * phase + 4.0)
        )
        length_fraction = min(0.95, max(0.40, length_fraction))
        x = fin_left + round(k * fin_pitch)
        fin_width = max(2, round(fin_pitch * 0.55))
        y_end = fin_top + round(length_fraction * fin_height_pixels)
        painter.draw.rectangle((x, fin_top, x + fin_width, y_end), fill=FIN_AMBER)
        painter.draw.rectangle((x + fin_width - 1, fin_top, x + fin_width, y_end), fill=FIN_AMBER_SHADOW)

    dark_metal = BANDS["dark_metal"]
    painter.flat(dark_metal, CHARCOAL_WALL, noise_amplitude=4, seed=22)
    metal_left, metal_top, metal_right, metal_bottom = dark_metal.pixel_rect(ATLAS_SIZE)
    ribs_per_tile = 8
    for k in range(ribs_per_tile):
        x = metal_left + round(k * ATLAS_SIZE / ribs_per_tile)
        painter.draw.line((x, metal_top, x, metal_bottom - 1), fill=CHARCOAL_RIB, width=2)

    dark_roof = BANDS["dark_roof"]
    painter.flat(dark_roof, DARK_ROOF, noise_amplitude=3, seed=23)
    roof_left, roof_top, roof_right, roof_bottom = dark_roof.pixel_rect(ATLAS_SIZE)
    seam_count = 12
    for k in range(seam_count):
        x = roof_left + round(k * ATLAS_SIZE / seam_count)
        painter.draw.line((x, roof_top, x, roof_bottom - 1), fill=DARK_FASCIA)
    # Rooftop solar array across the middle of the slope.
    solar_top = roof_top + round(0.25 * (roof_bottom - roof_top))
    solar_bottom = roof_top + round(0.75 * (roof_bottom - roof_top))
    panel_width = round(ATLAS_SIZE / 6 / 3)
    panel_height = max(6, round((solar_bottom - solar_top) / 8))
    y = solar_top
    while y + panel_height <= solar_bottom:
        x = roof_left
        while x + panel_width <= roof_right:
            painter.draw.rectangle((x + 1, y + 1, x + panel_width - 2, y + panel_height - 2), fill=SOLAR_PANEL)
            painter.draw.rectangle((x + 1, y + 1, x + panel_width - 2, y + panel_height - 2), outline=SOLAR_FRAME)
            x += panel_width
        y += panel_height
    painter.save(path)


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def main_ring_world() -> list[tuple[float, float]]:
    return [APRON_FRAME.to_world(a, c) for a, c in MAIN_RING_FRAME]


def build(era: str = "2028") -> Mesh:
    """era: "2024" = as photographed today; "2028" = with the Skanska /
    Hennebery Eddy expansion (new NE concourse replaces the NE walkway)."""
    mesh = Mesh()
    frame = APRON_FRAME
    strips = list(WALKWAY_STRIPS) if era == "2024" else [WALKWAY_STRIPS[1]]
    ring = main_ring_world()

    # --- Main body walls: apron-line edges get the holdroom glazing,
    #     everything else is tan wall with stone base.
    for k in range(len(MAIN_RING_FRAME)):
        (a0, c0), (a1, c1) = MAIN_RING_FRAME[k], MAIN_RING_FRAME[(k + 1) % len(MAIN_RING_FRAME)]
        is_apron_face = c0 > -8.0 and c1 > -8.0
        band = BANDS["glaze_concourse"] if is_apron_face else BANDS["wall_tan"]
        wall_run(mesh, [frame.to_world(a0, c0), frame.to_world(a1, c1)], 0.0, PARAPET_Y, band)
    polygon_cap(mesh, ring, PARAPET_Y, RECTS["white_roof"])

    # --- Covered boarding walkways: low glazed strips, green sloped roof
    #     rising toward the apron.
    for along_range, across_range in strips:
        box(
            mesh, frame, along_range, across_range, 0.0, WALKWAY_WALL_TOP_Y,
            BANDS["glaze_concourse"],
        )
        shed_roof(
            mesh, frame, along_range, across_range,
            high_y=WALKWAY_ROOF_HIGH_Y, low_y=WALKWAY_ROOF_LOW_Y,
            overhang=0.7, roof_band=BANDS["roof_green"],
            fascia_rect=RECTS["fascia_pale"], fascia_depth=0.35,
            high_side_low_across=False, soffit_rect=RECTS["fascia_pale"],
        )

    # --- Airside two-story departure lounge.
    box(
        mesh, frame, LOUNGE_ALONG, LOUNGE_ACROSS, 0.0, LOUNGE_TOP_Y,
        BANDS["glaze_lounge"], cap_rect=RECTS["white_roof"],
    )
    shed_roof(
        mesh, frame, LOUNGE_ALONG, LOUNGE_ACROSS,
        high_y=LOUNGE_ROOF_HIGH_Y, low_y=LOUNGE_ROOF_LOW_Y,
        overhang=LOUNGE_OVERHANG, roof_band=BANDS["roof_green"],
        fascia_rect=RECTS["fascia_pale"], fascia_depth=0.5,
        high_side_low_across=False, soffit_rect=RECTS["fascia_pale"],
    )
    pier_count = int((LOUNGE_ALONG[1] - LOUNGE_ALONG[0]) / PIER_SPACING)
    for k in range(pier_count + 1):
        along = LOUNGE_ALONG[0] + k * (LOUNGE_ALONG[1] - LOUNGE_ALONG[0]) / pier_count
        box(
            mesh, frame, (along - 0.3, along + 0.3),
            (LOUNGE_ACROSS[1] - 0.05, LOUNGE_ACROSS[1] + 0.55),
            0.0, LOUNGE_TOP_Y + 0.5, BANDS["concrete"],
        )

    # --- Concourse gate canopies on the main body north of the lounge
    #     (retained in both eras; the expansion grows the OTHER way, SW).
    along = CANOPY_FIRST_ALONG
    while along + CANOPY_HALF_ALONG < 147.0:
        shed_roof(
            mesh, frame,
            (along - CANOPY_HALF_ALONG, along + CANOPY_HALF_ALONG),
            (-1.5, 3.4),
            high_y=6.4, low_y=5.7, overhang=0.0,
            roof_band=BANDS["roof_green"], fascia_rect=RECTS["fascia_pale"],
            fascia_depth=0.4, high_side_low_across=True,
            soffit_rect=RECTS["fascia_pale"],
        )
        along += CANOPY_SPACING

    # --- Landside stepped shed roofs above the parapet, with clerestory
    #     back walls and end walls closing each step.
    for across_range, eave_y, ridge_y, along_range in LANDSIDE_GABLES:
        ridge_across = across_range[0] + RIDGE_ACROSS_FRACTION * (
            across_range[1] - across_range[0]
        )
        # Clerestory support walls: under the ridge line and closing the ends.
        ridge_start = frame.to_world(along_range[0], ridge_across)
        ridge_end = frame.to_world(along_range[1], ridge_across)
        wall_run(mesh, [ridge_start, ridge_end], PARAPET_Y - 0.3, ridge_y - 0.2, BANDS["wall_tan"])
        wall_run(mesh, [ridge_end, ridge_start], PARAPET_Y - 0.3, ridge_y - 0.2, BANDS["wall_tan"])
        for end_along in along_range:
            end_start = frame.to_world(end_along, across_range[0])
            end_finish = frame.to_world(end_along, across_range[1])
            wall_run(mesh, [end_start, end_finish], PARAPET_Y - 0.3, ridge_y - 0.2, BANDS["wall_tan"])
            wall_run(mesh, [end_finish, end_start], PARAPET_Y - 0.3, ridge_y - 0.2, BANDS["wall_tan"])
        gable_roof(
            mesh, frame, along_range, across_range,
            eave_y=eave_y, ridge_y=ridge_y, overhang=1.8,
            roof_band=BANDS["roof_green"], fascia_rect=RECTS["fascia_pale"],
            fascia_depth=0.45, end_wall_rect=RECTS["gable_end"],
            soffit_rect=RECTS["fascia_pale"],
            ridge_across_fraction=RIDGE_ACROSS_FRACTION,
        )

    # --- Landside entry: the vestibule bump carries its own gable in the
    #     same parallel-ridge pattern as the stepped roofline (owner
    #     correction: not a projecting perpendicular dormer).
    gable_roof(
        mesh, frame, (80.5, 96.5), (-99.0, -88.0),
        eave_y=4.8, ridge_y=7.4, overhang=1.0,
        roof_band=BANDS["roof_green"], fascia_rect=RECTS["glulam"],
        fascia_depth=0.5, end_wall_rect=RECTS["gable_end"],
        soffit_rect=RECTS["glulam"],
        ridge_across_fraction=RIDGE_ACROSS_FRACTION,
    )
    for pier_along in (82.6, 94.4):
        box(
            mesh, frame, (pier_along - 0.6, pier_along + 0.6),
            (-97.4, -96.0), 0.0, 4.8, BANDS["stone"],
        )

    # --- Concrete pillars separating the glazed sections on the airside.
    for pillar_along in range(96, 146, 7):
        box(
            mesh, frame, (pillar_along - 0.25, pillar_along + 0.25),
            (-0.05, 0.5), 0.0, PARAPET_Y, BANDS["concrete"],
        )
    for strip_along_range, strip_across_range in strips:
        pillar_position = strip_along_range[0] + 3.0
        while pillar_position < strip_along_range[1] - 1.5:
            box(
                mesh, frame, (pillar_position - 0.22, pillar_position + 0.22),
                (strip_across_range[1] - 0.05, strip_across_range[1] + 0.45),
                0.0, WALKWAY_WALL_TOP_Y + 0.2, BANDS["concrete"],
            )
            pillar_position += 7.0

    # --- Continuous curbside canopy along the landside face.
    shed_roof(
        mesh, frame, (26.0, 82.0), (-89.5, -85.4),
        high_y=4.6, low_y=4.0, overhang=0.0,
        roof_band=BANDS["roof_green"], fascia_rect=RECTS["fascia_pale"],
        fascia_depth=0.35, high_side_low_across=False,
        soffit_rect=RECTS["fascia_pale"],
    )

    # --- Basalt stone piers along the landside face (c = -85.9 wall).
    for pier_along in range(26, 80, 9):
        box(
            mesh, frame, (pier_along - 0.6, pier_along + 0.6),
            (-86.6, -85.5), 0.0, 4.6, BANDS["stone"],
        )

    if era == "2028":
        build_expansion(mesh, frame)

    return mesh


def build_expansion(mesh: Mesh, frame: Frame) -> None:
    """The 2025-2028 Skanska / Hennebery Eddy expansion (see module
    docstring constants): two-level mass-timber concourse NE of the
    retained lounge, white processor block, jet bridges, and the NE
    ground-boarding run with its landside connector."""
    # --- Two-level concourse.
    box(
        mesh, frame, CONCOURSE_ALONG, CONCOURSE_ACROSS,
        0.0, CONCOURSE_GROUND_TOP_Y, BANDS["dark_metal"],
    )
    box(
        mesh, frame, CONCOURSE_ALONG, CONCOURSE_ACROSS,
        CONCOURSE_GROUND_TOP_Y, CONCOURSE_ROOF_LOW_Y - 0.1, BANDS["fin_screen"],
        v_zero_y=CONCOURSE_GROUND_TOP_Y,
    )
    shed_roof(
        mesh, frame, CONCOURSE_ALONG, CONCOURSE_ACROSS,
        high_y=CONCOURSE_ROOF_HIGH_Y, low_y=CONCOURSE_ROOF_LOW_Y,
        overhang=2.6, roof_band=BANDS["dark_roof"],
        fascia_rect=RECTS["dark_fascia"], fascia_depth=0.55,
        high_side_low_across=False, soffit_rect=RECTS["glulam"],
    )

    # --- Junction infill between the retained lounge and the concourse.
    box(
        mesh, frame, JUNCTION_ALONG, (-20.5, 1.0), 0.0, 8.6,
        BANDS["glaze_lounge"], cap_rect=RECTS["white_roof"],
    )

    # --- White processor block behind the junction (SSCP / circulation).
    box(
        mesh, frame, PROCESSOR_ALONG, PROCESSOR_ACROSS, 0.0, PROCESSOR_TOP_Y,
        BANDS["concrete"], cap_rect=RECTS["white_roof"],
    )

    # --- Jet bridges: fixed link, rotunda, telescope, support, all dark.
    for bridge_along in BRIDGE_ALONG_POSITIONS:
        face_across = CONCOURSE_ACROSS[1]
        oriented_slab(
            mesh,
            frame.to_world(bridge_along, face_across),
            9.0,
            frame.to_world(bridge_along, face_across + 4.5),
            9.0,
            width=3.0, thickness=3.2,
            side_band=BANDS["dark_metal"], top_rect=RECTS["dark_fascia"],
        )
        prism(
            mesh, frame, bridge_along, face_across + 5.6, 2.2,
            2.2, 9.6, BANDS["dark_metal"], cap_rect=RECTS["dark_fascia"],
        )
        prism(
            mesh, frame, bridge_along, face_across + 5.6, 0.5,
            0.0, 2.4, BANDS["concrete"],
        )
        telescope_start = frame.to_world(bridge_along - 1.0, face_across + 7.0)
        telescope_end = frame.to_world(bridge_along - 13.0, face_across + 27.0)
        oriented_slab(
            mesh, telescope_start, 8.4, telescope_end, 6.0,
            width=2.4, thickness=2.6,
            side_band=BANDS["glaze_lounge"], top_rect=RECTS["dark_fascia"],
        )
        support = frame.to_world(bridge_along - 9.0, face_across + 20.5)
        support_along, support_across = bridge_along - 9.0, face_across + 20.5
        prism(
            mesh, frame, support_along, support_across, 0.35,
            0.0, 4.6, BANDS["concrete"],
        )



def main() -> None:
    output_dir = PROTOTYPE_DIR / "output"
    output_dir.mkdir(exist_ok=True)
    paint_atlas(output_dir / "KRDM_terminal.png")
    for era, object_name in (("2024", "KRDM_terminal.obj"), ("2028", "KRDM_terminal_2028.obj")):
        mesh = build(era)
        write_obj8(
            mesh, output_dir / object_name,
            texture_file_name="KRDM_terminal.png",
            comments=[
                f"KRDM Roberts Field passenger terminal ({era}) - procedural prototype",
                "Sources: OSM way 104478704; TACP 2021 figs 7-4/7-5; flyrdm.com renderings",
            ],
        )
        print(f"era {era}: vertices={len(mesh.vertices)} triangles={mesh.triangle_count} -> {object_name}")


if __name__ == "__main__":
    main()
