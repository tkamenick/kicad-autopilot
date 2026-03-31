"""Shared test fixtures."""
from pathlib import Path

import pytest

from src.schema import Board, Component, Keepout, Net, Pad, Placement, Pour, Route, Rules, Segment, Via

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def synthetic_kicad_path():
    return FIXTURES_DIR / "synthetic_board.kicad_pcb"


@pytest.fixture
def rotated_kicad_path():
    """4 resistors at 0/90/180/270° — exercises pad_abs_position through the export pipeline."""
    return FIXTURES_DIR / "rotated_board.kicad_pcb"


@pytest.fixture
def routed_kicad_path():
    """Synthetic board with added trace segments and a via — exercises _extract_routes/_extract_vias."""
    return FIXTURES_DIR / "routed_board.kicad_pcb"


@pytest.fixture
def poly_outline_kicad_path():
    """Board whose outline is a gr_poly node rather than gr_line segments — exercises the fallback path."""
    return FIXTURES_DIR / "poly_outline_board.kicad_pcb"


@pytest.fixture
def minimal_board():
    """2-component, 2-net board for fast unit tests."""
    rules = Rules()
    pads_u1 = [
        Pad(number="1", net="GND", offset=(-1.5, 0.0), size=(0.6, 0.6), shape="rect", layer="F.Cu"),
        Pad(number="2", net="3V3", offset=(1.5, 0.0), size=(0.6, 0.6), shape="rect", layer="F.Cu"),
    ]
    pads_j1 = [
        Pad(number="1", net="GND", offset=(-1.5, 0.0), size=(0.6, 0.6), shape="rect", layer="F.Cu"),
        Pad(number="2", net="3V3", offset=(1.5, 0.0), size=(0.6, 0.6), shape="rect", layer="F.Cu"),
    ]
    u1 = Component(
        reference="U1", footprint="MCU", description="MCU",
        position=(9.0, 9.0), rotation=0.0, layer="F.Cu",
        bbox=(-3.0, -3.0, 3.0, 3.0), pads=pads_u1,
        placement=Placement(),
    )
    j1 = Component(
        reference="J1", footprint="Connector", description="Connector",
        position=(30.0, 9.0), rotation=0.0, layer="F.Cu",
        bbox=(-2.0, -2.0, 2.0, 2.0), pads=pads_j1,
        placement=Placement(),
    )
    nets = {
        "GND": Net(name="GND", class_="ground", strategy="pour", width_mm=None, priority=0),
        "3V3": Net(name="3V3", class_="power", strategy="route_wide", width_mm=0.5, priority=1),
    }
    return Board(
        board_outline=[(0.0, 0.0), (39.0, 0.0), (39.0, 18.0), (0.0, 18.0)],
        grid_step=0.3,
        rules=rules,
        components={"U1": u1, "J1": j1},
        nets=nets,
        keepouts=[],
        routes=[],
        vias=[],
        pours=[Pour(net="GND", layer="B.Cu", outline="board", priority=0)],
    )


@pytest.fixture
def crossing_board():
    """4 components at corners; 2 nets whose ratsnest MST edges cross diagonally.

    Net A: C1(6,6) ↔ C4(24,21)  — diagonal edge going bottom-left → top-right
    Net B: C2(24,6) ↔ C3(6,21) — diagonal edge going bottom-right → top-left
    These two edges cross at the board center → guaranteed 1 crossing, 1 net pair.
    """
    rules = Rules()

    def _comp(ref, pos, net):
        return Component(
            reference=ref, footprint="C", description="Cap",
            position=pos, rotation=0.0, layer="F.Cu",
            bbox=(-0.5, -0.5, 0.5, 0.5),
            pads=[Pad(number="1", net=net, offset=(0.0, 0.0),
                      size=(0.4, 0.4), shape="rect", layer="F.Cu")],
            placement=Placement(allowed_rotations=[0, 90, 180, 270]),
        )

    nets = {
        "A": Net(name="A", class_="signal", strategy="route", width_mm=None, priority=2),
        "B": Net(name="B", class_="signal", strategy="route", width_mm=None, priority=2),
    }
    return Board(
        board_outline=[(0.0, 0.0), (30.0, 0.0), (30.0, 27.0), (0.0, 27.0)],
        grid_step=0.3, rules=rules,
        components={
            "C1": _comp("C1", (6.0,  6.0),  "A"),   # bottom-left,  net A
            "C2": _comp("C2", (24.0, 6.0),  "B"),   # bottom-right, net B
            "C3": _comp("C3", (6.0,  21.0), "B"),   # top-left,     net B
            "C4": _comp("C4", (24.0, 21.0), "A"),   # top-right,    net A
        },
        nets=nets, keepouts=[], routes=[], vias=[], pours=[],
    )


@pytest.fixture
def congested_board():
    """2 components with a 1 mm horizontal gap and 3 shared nets → oversubscribed channel.

    Rules defaults: trace=0.3mm, clearance=0.2mm → track_size=0.5mm.
    available = floor(1.0 / 0.5) = 2 tracks; required = 3 nets → oversubscribed.

    U1 abs bbox: (3, 7, 7, 13) — right edge at x=7
    U2 abs bbox: (8, 7, 12, 13) — left edge at x=8  → gap = 1 mm
    """
    rules = Rules()

    def _pads():
        return [
            Pad(number="1", net="GND", offset=(0.0, 0.0), size=(0.6, 0.6), shape="rect", layer="F.Cu"),
            Pad(number="2", net="3V3", offset=(0.0, 1.0), size=(0.6, 0.6), shape="rect", layer="F.Cu"),
            Pad(number="3", net="SIG", offset=(0.0, 2.0), size=(0.6, 0.6), shape="rect", layer="F.Cu"),
        ]

    u1 = Component(
        reference="U1", footprint="IC", description="IC",
        position=(5.0, 10.0), rotation=0.0, layer="F.Cu",
        bbox=(-2.0, -3.0, 2.0, 3.0), pads=_pads(), placement=Placement(),
    )
    u2 = Component(
        reference="U2", footprint="IC", description="IC",
        position=(10.0, 10.0), rotation=0.0, layer="F.Cu",
        bbox=(-2.0, -3.0, 2.0, 3.0), pads=_pads(), placement=Placement(),
    )
    nets = {
        "GND": Net(name="GND", class_="ground", strategy="pour",       width_mm=None, priority=0),
        "3V3": Net(name="3V3", class_="power",  strategy="route_wide", width_mm=0.5,  priority=1),
        "SIG": Net(name="SIG", class_="signal", strategy="route",      width_mm=None, priority=2),
    }
    return Board(
        board_outline=[(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)],
        grid_step=0.3, rules=rules,
        components={"U1": u1, "U2": u2},
        nets=nets, keepouts=[], routes=[], vias=[],
        pours=[Pour(net="GND", layer="B.Cu", outline="board", priority=0)],
    )


@pytest.fixture
def edge_board():
    """U1 sits at the board corner (0,0); its pad has 3/4 directions blocked → violation.

    Blocked directions for U1 pad at abs (0, 0):
      left  → probe (-0.3, 0)    outside board (x < 0)
      down  → probe (0,   -0.3)  outside board (y < 0)
      right → probe (0.3,  0)    inside U2's bbox (0.3–0.7, -0.5–0.5)
      up    → probe (0,    0.3)  inside board, clear of U2
    3 directions blocked → "U1:1" reported as a pin-escape violation.
    """
    rules = Rules()

    u1 = Component(
        reference="U1", footprint="C", description="Cap",
        position=(0.0, 0.0), rotation=0.0, layer="F.Cu",
        bbox=(-0.1, -0.1, 0.1, 0.1),
        pads=[Pad(number="1", net="SIG", offset=(0.0, 0.0),
                  size=(0.4, 0.4), shape="rect", layer="F.Cu")],
        placement=Placement(),
    )
    # U2's abs bbox: (0.3, -0.5, 0.7, 0.5) — blocks U1's right probe at (0.3, 0)
    u2 = Component(
        reference="U2", footprint="C", description="Cap",
        position=(0.5, 0.0), rotation=0.0, layer="F.Cu",
        bbox=(-0.2, -0.5, 0.2, 0.5),
        pads=[Pad(number="1", net="SIG", offset=(0.0, 0.0),
                  size=(0.4, 0.4), shape="rect", layer="F.Cu")],
        placement=Placement(),
    )
    nets = {
        "SIG": Net(name="SIG", class_="signal", strategy="route", width_mm=None, priority=2),
    }
    return Board(
        board_outline=[(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0)],
        grid_step=0.3, rules=rules,
        components={"U1": u1, "U2": u2},
        nets=nets, keepouts=[], routes=[], vias=[], pours=[],
    )


@pytest.fixture
def minimal_sexpr():
    return '(kicad_pcb (version 20221018) (generator "pcbnew") (net 1 "GND") (net 2 "3V3"))'


@pytest.fixture
def footprint_sexpr():
    return (
        '(footprint "MCU:SOIC-10" (at 130 100) (layer "F.Cu") '
        '(fp_text reference "U1" (at 0 -8) (layer "F.SilkS")) '
        '(pad "1" smd rect (at -3.5 -4) (size 0.6 1.2) (layers "F.Cu") (net 1 "GND")))'
    )
