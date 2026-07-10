"""Pad-in-solved-pavement HOST LEVEL re-level (user 2026-07-10, round 6 site 3).

A building pad embedded in / abutting SOLVED pavement must sit FLAT at the level
the HOST pavement solved to at the contact — not at a raw-DEM frontage seat that
leaves the flat pad in a pit the apron humps around (CYXY apron #129 →
building8: a -333 %/1.1 m step, "a big hump in this apron").

Synthetic fixture (no airport build): an apron edge solved at a body level, a
flat pad abutting it seated at a PIT value (below the body), and a run of shared
"lip" apron nodes contaminated to the pit value.  The re-level must:

  * seat the pad FLAT at the host BODY level (adopt FROM the host);
  * lift the contaminated shared lip to the body level (no emit cliff);
  * leave the host BODY untouched (pad adopts from host, never the reverse);
  * leave a pad already agreeing with its host, and a pad far from any solved
    pavement, UNCHANGED;
  * honour the ``O4_PAD_HOST_PAVEMENT_LEVEL`` gate (off → no-op).
"""
import os

import pytest
from shapely.geometry import Polygon

from auto_patch.config import (
    PAD_HOST_LEVEL_TRIGGER_M, APRON_MAX_GRADE,
)
from auto_patch.layout import BuiltShape, ROLE_APRON, ROLE_BUILDING
from auto_patch.elevation_per_surface.route_profile.anchors import (
    relevel_pads_to_host_pavement,
)

BODY = 100.0                       # host apron solved (body) level
PIT = 96.5                         # DEM-low pad seat (a 3.5 m pit vs the body)


class _FakeLayout:
    def __init__(self, shapes):
        self.shapes = shapes


def _apron_with_lip():
    """An apron whose top edge (y=10) abuts the pad's bottom edge.

    Body vertices sit at ``BODY`` a couple of metres either side of the pad
    corners (so the pad has host BODY within the contact radius), and two
    vertices coincident with the pad corners are contaminated to ``PIT`` (the
    shared lip the old seat dragged down).
    """
    ring = [
        (0.0, 0.0), (40.0, 0.0), (40.0, 10.0),
        (32.0, 10.0),            # body, ~2 m right of the (30,10) pad corner
        (30.0, 10.0),            # shared lip @ pad corner  → PIT
        (28.0, 10.0),            # body, ~2 m left of (30,10)
        (22.0, 10.0),            # body, ~2 m right of (20,10)
        (20.0, 10.0),            # shared lip @ pad corner  → PIT
        (18.0, 10.0),            # body, ~2 m left of (20,10)
        (0.0, 10.0),
    ]
    alt = [BODY, BODY, BODY,
           BODY, PIT, BODY, BODY, PIT, BODY, BODY]
    poly = Polygon(ring)
    return BuiltShape(polygon=poly, role=ROLE_APRON,
                      node_altitudes=alt + [alt[0]])


def _pad(x0, y0, x1, y1, alt):
    return BuiltShape(polygon=Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]),
                      role=ROLE_BUILDING, altitude=alt)


def _pad_level(s):
    if s.altitude is not None:
        return s.altitude
    vals = [v for v in (s.node_altitudes or []) if v is not None]
    return sum(vals) / len(vals) if vals else None


def test_embedded_pit_pad_adopts_host_body(monkeypatch):
    monkeypatch.setenv("O4_PAD_HOST_PAVEMENT_LEVEL", "1")
    apron = _apron_with_lip()
    pad = _pad(20.0, 10.0, 30.0, 18.0, PIT)        # abuts the apron top edge
    layout = _FakeLayout([apron, pad])

    n = relevel_pads_to_host_pavement(layout)
    assert n == 1, "the embedded pit pad must be re-levelled"

    # (1) The pad seats FLAT at the host BODY level.
    assert pad.altitude == pytest.approx(BODY, abs=0.01)

    # (2) The contaminated shared lip is lifted to the body level (no cliff).
    lip_vals = [apron.node_altitudes[4], apron.node_altitudes[7]]
    assert all(abs(v - BODY) < 0.01 for v in lip_vals), (
        f"shared lip not lifted: {lip_vals}")

    # (3) The host BODY is untouched (pad adopts from host, never the reverse).
    body_idx = [0, 1, 2, 3, 5, 6, 8, 9]
    assert all(abs(apron.node_altitudes[i] - BODY) < 0.01 for i in body_idx)

    # (4) The step at the contact is gone — pad and host lip now agree, so the
    #     residual disagreement is within the apron grade cap over the contact.
    step = abs(_pad_level(pad) - max(lip_vals))
    assert step <= APRON_MAX_GRADE * 100.0 * 0.5, (
        f"a step of {step:.2f} m survives at the pad↔host contact")


def test_pad_already_agreeing_is_untouched(monkeypatch):
    monkeypatch.setenv("O4_PAD_HOST_PAVEMENT_LEVEL", "1")
    apron = _apron_with_lip()
    # Seat the pad already at the body level; its lip is not contaminated.
    for i in (4, 7):
        apron.node_altitudes[i] = BODY
    pad = _pad(20.0, 10.0, 30.0, 18.0, BODY)
    layout = _FakeLayout([apron, pad])

    n = relevel_pads_to_host_pavement(layout)
    assert n == 0
    assert pad.altitude == pytest.approx(BODY, abs=0.01)


def test_pad_far_from_pavement_is_untouched(monkeypatch):
    monkeypatch.setenv("O4_PAD_HOST_PAVEMENT_LEVEL", "1")
    apron = _apron_with_lip()
    pad = _pad(500.0, 500.0, 510.0, 510.0, PIT)    # nowhere near the apron
    layout = _FakeLayout([apron, pad])

    n = relevel_pads_to_host_pavement(layout)
    assert n == 0
    assert pad.altitude == pytest.approx(PIT, abs=0.01)


def test_gate_off_is_noop(monkeypatch):
    monkeypatch.setenv("O4_PAD_HOST_PAVEMENT_LEVEL", "0")
    apron = _apron_with_lip()
    pad = _pad(20.0, 10.0, 30.0, 18.0, PIT)
    layout = _FakeLayout([apron, pad])

    n = relevel_pads_to_host_pavement(layout)
    assert n == 0
    assert pad.altitude == pytest.approx(PIT, abs=0.01)
    # lip stays contaminated (byte-identical to the pre-fix behaviour)
    assert apron.node_altitudes[4] == pytest.approx(PIT, abs=0.01)
