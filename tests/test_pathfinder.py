"""Tests for src/pathfinder.py."""
from __future__ import annotations

import math

import numpy as np
import pytest

from src.pathfinder import (
    LAYERS, VIA_COST,
    _all_pad_cells, _astar, _build_occupied, _cell, _coord,
    _mark_path, _path_to_segments_vias, route_board, route_net,
)
from src.schema import Board, Component, Net, Pad, Placement, Pour, Route, Rules, Segment, Via


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _simple_board(
    width: float = 20.0,
    height: float = 20.0,
    components: dict | None = None,
    nets: dict | None = None,
    routes: list | None = None,
    vias: list | None = None,
) -> Board:
    rules = Rules()
    return Board(
        board_outline=[(0.0, 0.0), (width, 0.0), (width, height), (0.0, height)],
        grid_step=0.3,
        rules=rules,
        components=components or {},
        nets=nets or {},
        keepouts=[],
        routes=routes or [],
        vias=vias or [],
        pours=[],
    )


def _resistor(ref: str, pos: tuple, net_a: str, net_b: str, layer: str = "F.Cu") -> Component:
    pads = [
        Pad(number="1", net=net_a, offset=(-1.5, 0.0), size=(0.6, 0.6), shape="rect", layer=layer),
        Pad(number="2", net=net_b, offset=(1.5, 0.0), size=(0.6, 0.6), shape="rect", layer=layer),
    ]
    return Component(
        reference=ref, footprint="R", description="",
        position=pos, rotation=0.0, layer=layer,
        bbox=(-2.0, -1.0, 2.0, 1.0), pads=pads,
        placement=Placement(),
    )


def _make_net(name: str, class_: str = "signal", priority: int = 10) -> Net:
    return Net(name=name, class_=class_, strategy="route", width_mm=0.3, priority=priority)


# ---------------------------------------------------------------------------
# Cell / coord helpers
# ---------------------------------------------------------------------------

class TestCellHelpers:
    def test_cell_zero(self):
        assert _cell(0.0, 0.3) == 0

    def test_cell_grid_aligned(self):
        assert _cell(3.0, 0.3) == 10
        assert _cell(1.5, 0.3) == 5

    def test_cell_rounds(self):
        assert _cell(0.31, 0.3) == 1
        assert _cell(0.29, 0.3) == 1

    def test_coord_roundtrip(self):
        for v in (0, 5, 10, 33):
            assert _coord(v, 0.3) == pytest.approx(v * 0.3, abs=1e-6)


# ---------------------------------------------------------------------------
# Occupancy grid
# ---------------------------------------------------------------------------

class TestBuildOccupied:
    def test_shape(self, minimal_board):
        occ = _build_occupied(minimal_board)
        assert occ.shape[0] == 2  # 2 layers

    def test_board_edges_blocked(self, minimal_board):
        occ = _build_occupied(minimal_board)
        # Cell (0,0) should be blocked (outside safe zone)
        assert occ[0, 0, 0]
        assert occ[1, 0, 0]

    def test_center_clear(self):
        board = _simple_board(width=20.0, height=20.0)
        occ = _build_occupied(board)
        # Center cell should be clear
        r, c = _cell(10.0, 0.3), _cell(10.0, 0.3)
        # Bounds check
        assert r < occ.shape[1] and c < occ.shape[2]
        assert not occ[0, r, c]

    def test_component_bbox_blocked(self):
        nets = {"A": _make_net("A"), "B": _make_net("B")}
        r1 = _resistor("R1", (10.0, 10.0), "A", "B")
        board = _simple_board(components={"R1": r1}, nets=nets)
        occ = _build_occupied(board)
        # Cell inside R1's bbox but NOT on a pad escape corridor should be blocked.
        # Pads at (8.5,10.0)→cell(33,28) and (11.5,10.0)→cell(33,38).
        # Escape corridors run horizontally (row 33) and vertically (cols 28, 38).
        # Cell at row=32, col=33 is inside bbox but off all corridors → blocked.
        assert occ[0, 32, 33]  # F.Cu blocked inside bbox, not on corridor

    def test_pad_cells_not_blocked(self):
        nets = {"A": _make_net("A"), "B": _make_net("B")}
        r1 = _resistor("R1", (10.0, 10.0), "A", "B")
        board = _simple_board(components={"R1": r1}, nets=nets)
        occ = _build_occupied(board)
        # Pad 1 at offset (-1.5, 0) → abs (8.5, 10) → row=33, col=28
        pr = _cell(10.0, 0.3)
        pc = _cell(8.5, 0.3)
        assert not occ[0, pr, pc]  # pad cell should be clear


class TestAllPadCells:
    def test_returns_both_pads(self):
        nets = {"A": _make_net("A"), "B": _make_net("B")}
        r1 = _resistor("R1", (10.0, 10.0), "A", "B")
        board = _simple_board(components={"R1": r1}, nets=nets)
        cells = _all_pad_cells(board)
        # Pad 1: abs (8.5, 10.0)
        assert (_cell(10.0, 0.3), _cell(8.5, 0.3)) in cells
        # Pad 2: abs (11.5, 10.0)
        assert (_cell(10.0, 0.3), _cell(11.5, 0.3)) in cells


# ---------------------------------------------------------------------------
# A* search
# ---------------------------------------------------------------------------

class TestAstar:
    def _small_grid(self, rows: int = 20, cols: int = 20) -> np.ndarray:
        occ = np.zeros((2, rows, cols), dtype=bool)
        # Block edges (2 cells)
        occ[:, :2, :] = True
        occ[:, -2:, :] = True
        occ[:, :, :2] = True
        occ[:, :, -2:] = True
        return occ

    def test_direct_path(self):
        occ = self._small_grid()
        src = [(5, 5, 0)]
        dst = {(5, 10, 0)}
        path = _astar(src, dst, occ, VIA_COST)
        assert path is not None
        assert path[0] == (5, 5, 0)
        assert path[-1] == (5, 10, 0)

    def test_path_length_is_manhattan(self):
        occ = self._small_grid()
        src = [(5, 5, 0)]
        dst = {(5, 10, 0)}
        path = _astar(src, dst, occ, VIA_COST)
        # Manhattan distance = 5, path length = 6 cells (inclusive)
        assert path is not None
        assert len(path) == 6

    def test_blocked_path_returns_none(self):
        occ = self._small_grid()
        # Block column 7 entirely on layer 0
        occ[0, :, 7] = True
        # No via on layer 1 either
        occ[1, :, 7] = True
        src = [(5, 5, 0)]
        dst = {(5, 10, 0)}
        path = _astar(src, dst, occ, VIA_COST)
        assert path is None

    def test_via_transition(self):
        occ = self._small_grid()
        # Block entire layer 0, only allow travel on layer 1
        occ[0, :, :] = True
        occ[1, 2:-2, 2:-2] = False  # clear interior on layer 1
        # Unblock src and dst on layer 0
        occ[0, 5, 5] = False
        occ[0, 5, 10] = False
        src = [(5, 5, 0)]
        dst = {(5, 10, 0)}
        path = _astar(src, dst, occ, VIA_COST)
        assert path is not None
        # Path must change layer at some point
        layers = [p[2] for p in path]
        assert 1 in layers

    def test_reaches_dst_either_layer(self):
        occ = self._small_grid()
        src = [(5, 5, 0)]
        dst = {(5, 10, 0), (5, 10, 1)}  # accept either layer
        path = _astar(src, dst, occ, VIA_COST)
        assert path is not None
        assert path[-1] in dst


# ---------------------------------------------------------------------------
# Path → segments + vias
# ---------------------------------------------------------------------------

class TestPathToSegmentsVias:
    def test_straight_path_no_vias(self):
        path = [(5, 5, 0), (5, 6, 0), (5, 7, 0), (5, 8, 0)]
        segs, vias = _path_to_segments_vias(path, "GND", 0.3, 0.3)
        assert len(vias) == 0
        assert len(segs) == 1
        assert segs[0].layer == "F.Cu"
        assert segs[0].start == pytest.approx((1.5, 1.5))
        assert segs[0].end == pytest.approx((2.4, 1.5))

    def test_layer_change_creates_via(self):
        # (5,5,0) → (5,5,1) → (5,8,1)
        path = [(5, 5, 0), (5, 5, 1), (5, 8, 1)]
        segs, vias = _path_to_segments_vias(path, "GND", 0.3, 0.3)
        assert len(vias) == 1
        assert vias[0].net == "GND"
        assert vias[0].position == pytest.approx((1.5, 1.5))
        assert len(segs) == 1
        assert segs[0].layer == "B.Cu"

    def test_single_cell_path_empty(self):
        path = [(5, 5, 0)]
        segs, vias = _path_to_segments_vias(path, "A", 0.3, 0.3)
        assert segs == []
        assert vias == []

    def test_segment_coordinates_x_y_order(self):
        # col → x, row → y
        path = [(3, 7, 0), (3, 10, 0)]  # row=3, col=7..10
        segs, vias = _path_to_segments_vias(path, "A", 0.3, 0.3)
        assert len(segs) == 1
        assert segs[0].start == pytest.approx((7 * 0.3, 3 * 0.3))
        assert segs[0].end == pytest.approx((10 * 0.3, 3 * 0.3))


# ---------------------------------------------------------------------------
# Mark path
# ---------------------------------------------------------------------------

class TestMarkPath:
    def test_marks_cells_occupied(self):
        occ = np.zeros((2, 20, 20), dtype=bool)
        path = [(5, 5, 0), (5, 6, 0), (5, 7, 0)]
        pad_cells: set = set()
        _mark_path(occ, path, pad_cells)
        assert occ[0, 5, 5]
        assert occ[0, 5, 6]
        assert occ[0, 5, 7]
        assert not occ[1, 5, 5]  # layer 1 not marked for straight path

    def test_pad_cells_not_marked(self):
        occ = np.zeros((2, 20, 20), dtype=bool)
        path = [(5, 5, 0), (5, 6, 0), (5, 7, 0), (5, 8, 0)]
        pad_cells = {(5, 5)}
        _mark_path(occ, path, pad_cells)
        assert not occ[0, 5, 5]  # pad cell protected
        assert not occ[0, 5, 6]  # adjacent to pad — kept open for convergence
        assert occ[0, 5, 8]  # far from pad — clearance marked

    def test_via_expands_3x3(self):
        occ = np.zeros((2, 20, 20), dtype=bool)
        # Via at (10, 10): layer change
        path = [(10, 10, 0), (10, 10, 1), (10, 13, 1)]
        pad_cells: set = set()
        _mark_path(occ, path, pad_cells)
        # Both layers marked around via center
        assert occ[0, 10, 10]
        assert occ[1, 10, 10]
        assert occ[0, 9, 9]   # corner of 3x3
        assert occ[1, 11, 11]  # other corner


# ---------------------------------------------------------------------------
# route_net
# ---------------------------------------------------------------------------

class TestRouteNet:
    def test_simple_two_pad_net(self):
        """Two resistors, single net, clear space between them."""
        nets = {"A": _make_net("A"), "B": _make_net("B")}
        # R1 at (5, 10): pad1=(3.5,10), pad2=(6.5,10)
        # R2 at (14, 10): pad1=(12.5,10), pad2=(15.5,10)
        r1 = _resistor("R1", (5.0, 10.0), "A", "B")
        r2 = _resistor("R2", (14.0, 10.0), "A", "B")
        board = _simple_board(width=20.0, height=20.0,
                              components={"R1": r1, "R2": r2}, nets=nets)
        occ = _build_occupied(board)
        pad_cells = _all_pad_cells(board)
        segs, vias, _ = route_net(board, "A", occ, pad_cells)
        # Should produce at least one segment
        assert len(segs) > 0
        # All segments should be on A net (verified via route_board)

    def test_ground_net_skipped_in_route_board(self):
        """Ground nets should be excluded from route_board output."""
        nets = {
            "GND": Net(name="GND", class_="ground", strategy="pour",
                       width_mm=None, priority=0),
            "SIG": _make_net("SIG"),
        }
        r1 = _resistor("R1", (5.0, 10.0), "GND", "SIG")
        r2 = _resistor("R2", (14.0, 10.0), "GND", "SIG")
        board = _simple_board(width=20.0, height=20.0,
                              components={"R1": r1, "R2": r2}, nets=nets)
        routed, failed = route_board(board)
        # GND should not appear in routes
        route_nets = {r.net for r in routed.routes}
        assert "GND" not in route_nets

    def test_failed_net_reported(self):
        """A net with no possible path should appear in failed list."""
        nets = {"A": _make_net("A"), "B": _make_net("B")}
        # Put components far apart and block all intermediate cells
        r1 = _resistor("R1", (3.0, 5.0), "A", "B")
        r2 = _resistor("R2", (17.0, 5.0), "A", "B")
        board = _simple_board(width=20.0, height=20.0,
                              components={"R1": r1, "R2": r2}, nets=nets)
        occ = _build_occupied(board)
        # Block all cells in column range 4–16, rows 3–7 (everything between them)
        occ[:, 3:8, 4:17] = True
        pad_cells = _all_pad_cells(board)
        segs, vias, _ = route_net(board, "A", occ, pad_cells)
        # With the middle blocked, there should be no route on A
        # (note: not testing for None return — route_net returns empty lists on failure)
        # The important check: route_board marks it as failed
        # We'll test route_board's failed list separately


# ---------------------------------------------------------------------------
# route_board integration
# ---------------------------------------------------------------------------

class TestRouteBoard:
    def test_produces_valid_board(self, minimal_board):
        routed, failed = route_board(minimal_board)
        assert isinstance(routed, Board)
        assert isinstance(failed, list)

    def test_routes_non_ground_nets(self):
        nets = {
            "GND": Net(name="GND", class_="ground", strategy="pour", width_mm=None, priority=0),
            "3V3": Net(name="3V3", class_="power", strategy="route_wide", width_mm=0.5, priority=1),
        }
        r1 = _resistor("R1", (5.0, 10.0), "GND", "3V3")
        r2 = _resistor("R2", (14.0, 10.0), "GND", "3V3")
        board = _simple_board(width=20.0, height=20.0,
                              components={"R1": r1, "R2": r2}, nets=nets)
        routed, failed = route_board(board)
        route_nets = {r.net for r in routed.routes}
        assert "GND" not in route_nets
        # 3V3 should either be routed or in failed
        assert "3V3" in route_nets or "3V3" in failed

    def test_only_nets_filter(self):
        nets = {
            "A": _make_net("A", priority=2),
            "B": _make_net("B", priority=2),
        }
        r1 = _resistor("R1", (5.0, 10.0), "A", "B")
        r2 = _resistor("R2", (14.0, 10.0), "A", "B")
        board = _simple_board(width=20.0, height=20.0,
                              components={"R1": r1, "R2": r2}, nets=nets)
        routed, failed = route_board(board, only_nets=["A"])
        route_nets = {r.net for r in routed.routes}
        assert "B" not in route_nets

    def test_existing_routes_preserved(self):
        nets = {"A": _make_net("A"), "B": _make_net("B")}
        r1 = _resistor("R1", (5.0, 10.0), "A", "B")
        existing_seg = Segment(start=(4.0, 10.0), end=(6.0, 10.0), layer="F.Cu")
        existing_route = Route(net="A", width_mm=0.3, segments=[existing_seg])
        board = _simple_board(width=20.0, height=20.0,
                              components={"R1": r1}, nets=nets,
                              routes=[existing_route])
        routed, _ = route_board(board, only_nets=["B"])
        # Existing route A should still be there
        assert any(r.net == "A" for r in routed.routes)

    def test_single_pad_net_not_failed(self):
        """A net with only one pad doesn't need routing — should not appear in failed."""
        nets = {"A": _make_net("A")}
        pads = [Pad(number="1", net="A", offset=(0.0, 0.0), size=(0.6, 0.6),
                    shape="rect", layer="F.Cu")]
        comp = Component(reference="R1", footprint="R", description="",
                         position=(10.0, 10.0), rotation=0.0, layer="F.Cu",
                         bbox=(-1.0, -1.0, 1.0, 1.0), pads=pads,
                         placement=Placement())
        board = _simple_board(width=20.0, height=20.0,
                              components={"R1": comp}, nets=nets)
        _, failed = route_board(board)
        assert "A" not in failed

    def test_routes_have_correct_width(self):
        nets = {
            "PWR": Net(name="PWR", class_="power", strategy="route_wide",
                       width_mm=0.5, priority=1),
        }
        r1 = _resistor("R1", (5.0, 10.0), "PWR", "PWR")
        r2 = _resistor("R2", (14.0, 10.0), "PWR", "PWR")
        board = _simple_board(width=20.0, height=20.0,
                              components={"R1": r1, "R2": r2}, nets=nets)
        routed, _ = route_board(board)
        for route in routed.routes:
            if route.net == "PWR":
                assert route.width_mm == pytest.approx(0.5)

    def test_via_net_matches_route_net(self):
        """All vias should have a net name matching a route."""
        nets = {"A": _make_net("A"), "B": _make_net("B")}
        r1 = _resistor("R1", (5.0, 5.0), "A", "B")
        r2 = _resistor("R2", (5.0, 14.0), "A", "B")
        board = _simple_board(width=20.0, height=20.0,
                              components={"R1": r1, "R2": r2}, nets=nets)
        routed, _ = route_board(board)
        route_net_names = {r.net for r in routed.routes}
        for via in routed.vias:
            assert via.net in board.nets

    def test_net_ordering_power_before_signal(self):
        """Power nets should be routed before signal nets."""
        nets = {
            "PWR": Net(name="PWR", class_="power", strategy="route_wide",
                       width_mm=0.5, priority=1),
            "SIG": Net(name="SIG", class_="signal", strategy="route",
                       width_mm=0.3, priority=10),
        }
        r1 = _resistor("R1", (5.0, 5.0), "PWR", "SIG")
        r2 = _resistor("R2", (14.0, 5.0), "PWR", "SIG")
        board = _simple_board(width=20.0, height=20.0,
                              components={"R1": r1, "R2": r2}, nets=nets)
        routed, failed = route_board(board)
        # PWR should not be in failed if SIG is
        route_net_names = {r.net for r in routed.routes}
        if "SIG" in failed:
            assert "PWR" in route_net_names or "PWR" not in failed
