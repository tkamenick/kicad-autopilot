"""Tests for src/drc_checker.py."""
from __future__ import annotations

import pytest

from src.drc_checker import (
    DRCViolation, _check_edge_clearance, _check_shorts, _check_trace_width,
    _check_unrouted, check_drc, drc_to_dict,
)
from src.schema import Board, Component, Net, Pad, Placement, Pour, Route, Rules, Segment, Via


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _board(
    routes: list | None = None,
    vias: list | None = None,
    nets: dict | None = None,
    components: dict | None = None,
    outline: list | None = None,
) -> Board:
    rules = Rules()
    if nets is None:
        nets = {
            "GND": Net(name="GND", class_="ground", strategy="pour", width_mm=None, priority=0),
            "3V3": Net(name="3V3", class_="power", strategy="route_wide", width_mm=0.5, priority=1),
        }
    if outline is None:
        outline = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]
    return Board(
        board_outline=outline,
        grid_step=0.3,
        rules=rules,
        components=components or {},
        nets=nets,
        keepouts=[],
        routes=routes or [],
        vias=vias or [],
        pours=[],
    )


def _seg(start, end, layer="F.Cu", net="GND", width=0.3) -> Route:
    return Route(net=net, width_mm=width, segments=[Segment(start=start, end=end, layer=layer)])


def _comp(ref, pos, nets_list):
    pads = [Pad(number=str(i+1), net=n, offset=(i*2.0, 0.0), size=(0.6, 0.6),
                shape="rect", layer="F.Cu") for i, n in enumerate(nets_list)]
    return Component(reference=ref, footprint="R", description="",
                     position=pos, rotation=0.0, layer="F.Cu",
                     bbox=(-1.0, -1.0, len(nets_list)*2.0, 1.0), pads=pads,
                     placement=Placement())


# ---------------------------------------------------------------------------
# DRCViolation dataclass
# ---------------------------------------------------------------------------

class TestDRCViolationDataclass:
    def test_default_fields(self):
        v = DRCViolation(type="unrouted", severity="error", net_a="GND")
        assert v.net_b == ""
        assert v.location == (0.0, 0.0)
        assert v.message == ""

    def test_to_dict(self):
        v = DRCViolation(type="short", severity="error", net_a="A", net_b="B",
                         location=(1.0, 2.0), message="short!")
        d = drc_to_dict(v)
        assert d["type"] == "short"
        assert d["net_a"] == "A"
        assert d["net_b"] == "B"
        assert d["location"] == [1.0, 2.0]


# ---------------------------------------------------------------------------
# Check: unrouted
# ---------------------------------------------------------------------------

class TestCheckUnrouted:
    def test_fully_routed_no_violations(self):
        """Board where pads are connected by routes: no unrouted violations."""
        nets = {"A": Net(name="A", class_="signal", strategy="route", width_mm=0.3, priority=10)}
        comps = {
            "R1": _comp("R1", (5.0, 10.0), ["A"]),
            "R2": _comp("R2", (15.0, 10.0), ["A"]),
        }
        # Route connecting the two A pads
        seg = Segment(start=(5.0, 10.0), end=(15.0, 10.0), layer="F.Cu")
        routes = [Route(net="A", width_mm=0.3, segments=[seg])]
        board = _board(routes=routes, nets=nets, components=comps)
        violations = _check_unrouted(board)
        assert violations == []

    def test_unrouted_net_detected(self):
        """A net with pads but no routes → unrouted error."""
        nets = {"A": Net(name="A", class_="signal", strategy="route", width_mm=0.3, priority=10)}
        comps = {
            "R1": _comp("R1", (5.0, 10.0), ["A"]),
            "R2": _comp("R2", (15.0, 10.0), ["A"]),
        }
        board = _board(nets=nets, components=comps)
        violations = _check_unrouted(board)
        assert any(v.type == "unrouted" and v.net_a == "A" for v in violations)
        assert all(v.severity == "error" for v in violations if v.type == "unrouted")

    def test_ground_net_skipped(self):
        """Ground nets are handled by pour — should not be reported as unrouted."""
        nets = {"GND": Net(name="GND", class_="ground", strategy="pour",
                           width_mm=None, priority=0)}
        comps = {
            "R1": _comp("R1", (5.0, 10.0), ["GND"]),
            "R2": _comp("R2", (15.0, 10.0), ["GND"]),
        }
        board = _board(nets=nets, components=comps)
        violations = _check_unrouted(board)
        unrouted = [v for v in violations if v.type == "unrouted"]
        assert unrouted == []

    def test_no_components_no_violations(self):
        violations = _check_unrouted(_board())
        assert violations == []


# ---------------------------------------------------------------------------
# Check: edge clearance
# ---------------------------------------------------------------------------

class TestCheckEdgeClearance:
    def test_interior_trace_no_violation(self):
        routes = [_seg((10.0, 10.0), (20.0, 10.0))]
        board = _board(routes=routes)
        v = _check_edge_clearance(board, 0.5)
        assert v == []

    def test_trace_near_edge_warning(self):
        routes = [_seg((0.1, 10.0), (5.0, 10.0))]
        board = _board(routes=routes)
        v = _check_edge_clearance(board, 0.5)
        # 0.1 < 0.5 → warning
        assert any(vi.type == "edge_clearance" and vi.severity == "warning" for vi in v)

    def test_via_near_edge_warning(self):
        vias = [Via(position=(0.2, 15.0), net="GND", drill_mm=0.3)]
        board = _board(vias=vias)
        v = _check_edge_clearance(board, 0.5)
        assert any(vi.type == "edge_clearance" for vi in v)

    def test_no_routes_no_violations(self):
        board = _board()
        v = _check_edge_clearance(board)
        assert v == []

    def test_custom_threshold(self):
        routes = [_seg((1.0, 10.0), (5.0, 10.0))]
        board = _board(routes=routes)
        # With 0.5mm threshold: 1.0 > 0.5 → no violation
        assert _check_edge_clearance(board, 0.5) == []
        # With 2.0mm threshold: 1.0 < 2.0 → violation
        v = _check_edge_clearance(board, 2.0)
        assert len(v) > 0


# ---------------------------------------------------------------------------
# Check: shorts
# ---------------------------------------------------------------------------

class TestCheckShorts:
    def test_same_net_no_short(self):
        routes = [
            _seg((10.0, 10.0), (20.0, 10.0), net="GND"),
            _seg((15.0, 10.0), (25.0, 10.0), net="GND"),
        ]
        board = _board(routes=routes)
        v = _check_shorts(board)
        assert v == []

    def test_different_net_same_cell_is_short(self):
        # Segments overlap at (15.0, 10.0)
        routes = [
            _seg((10.0, 10.0), (20.0, 10.0), net="GND"),
            _seg((15.0, 10.0), (25.0, 10.0), net="3V3"),
        ]
        board = _board(routes=routes)
        v = _check_shorts(board)
        assert any(vi.type == "short" and vi.severity == "error" for vi in v)

    def test_different_layers_no_short(self):
        # Same x/y but different layers — not a short
        routes = [
            Route(net="GND", width_mm=0.3, segments=[
                Segment(start=(10.0, 10.0), end=(20.0, 10.0), layer="F.Cu")
            ]),
            Route(net="3V3", width_mm=0.3, segments=[
                Segment(start=(10.0, 10.0), end=(20.0, 10.0), layer="B.Cu")
            ]),
        ]
        board = _board(routes=routes)
        v = _check_shorts(board)
        assert v == []

    def test_short_reported_once_per_pair(self):
        # Two nets overlap across many cells — should produce one violation per pair
        routes = [
            _seg((10.0, 10.0), (25.0, 10.0), net="GND"),
            _seg((10.0, 10.0), (25.0, 10.0), net="3V3"),
        ]
        board = _board(routes=routes)
        v = _check_shorts(board)
        short_violations = [vi for vi in v if vi.type == "short"]
        assert len(short_violations) == 1

    def test_no_routes_no_shorts(self):
        assert _check_shorts(_board()) == []


# ---------------------------------------------------------------------------
# Check: trace width
# ---------------------------------------------------------------------------

class TestCheckTraceWidth:
    def test_valid_width_no_violation(self):
        routes = [Route(net="GND", width_mm=0.3, segments=[])]
        board = _board(routes=routes)
        v = _check_trace_width(board)
        assert v == []

    def test_too_narrow_violation(self):
        rules = Rules(min_clearance_mm=0.2)
        routes = [Route(net="GND", width_mm=0.1, segments=[])]
        board = Board(
            board_outline=[(0,0),(40,0),(40,30),(0,30)],
            grid_step=0.3, rules=rules, components={},
            nets={"GND": Net(name="GND", class_="ground", strategy="pour",
                             width_mm=None, priority=0)},
            keepouts=[], routes=routes, vias=[], pours=[],
        )
        v = _check_trace_width(board)
        assert any(vi.type == "trace_width" and vi.severity == "error" for vi in v)


# ---------------------------------------------------------------------------
# check_drc integration
# ---------------------------------------------------------------------------

class TestCheckDRC:
    def test_empty_board_no_violations(self):
        board = _board()
        violations = check_drc(board)
        assert violations == []

    def test_routed_board_passes(self, routed_kicad_path):
        from src.kicad_export import export_board
        board = export_board(routed_kicad_path)
        violations = check_drc(board)
        # The routed_board fixture has a valid (small) route — may have edge warnings
        # but should have no short errors
        short_errors = [v for v in violations if v.type == "short"]
        assert short_errors == []

    def test_returns_list_of_violations(self):
        board = _board()
        result = check_drc(board)
        assert isinstance(result, list)
        for v in result:
            assert isinstance(v, DRCViolation)
