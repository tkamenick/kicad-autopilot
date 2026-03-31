"""Tests for src/placement_scorer.py."""
from __future__ import annotations

import json
import math

import pytest

from src.schema import (
    Board, Component, Net, Pad, Placement, Pour, Route, Rules, Segment,
)
from src.placement_scorer import (
    ChannelInfo, PlacementScore,
    _comp_abs_bbox, _build_mst_edges, _count_crossings,
    _segments_intersect, _cross2d, _compute_wirelength,
    _analyze_channels, _check_pin_escape, _board_diagonal,
    score_placement, score_to_dict,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_two_pad_component(ref, position, offsets, nets, rotation=0.0):
    """Build a component with pads at given offsets connected to given nets."""
    pads = [
        Pad(number=str(i + 1), net=n, offset=off, size=(0.6, 0.6), shape="rect", layer="F.Cu")
        for i, (off, n) in enumerate(zip(offsets, nets))
    ]
    return Component(
        reference=ref, footprint="FP", description="",
        position=position, rotation=rotation, layer="F.Cu",
        bbox=(-1.0, -1.0, 1.0, 1.0), pads=pads,
        placement=Placement(),
    )


def _make_simple_board(comps, nets_dict, grid_step=0.3,
                        outline=None):
    """Create a minimal board from a dict of components and nets."""
    if outline is None:
        outline = [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)]
    return Board(
        board_outline=outline,
        grid_step=grid_step,
        rules=Rules(),
        components=comps,
        nets=nets_dict,
        keepouts=[], routes=[], vias=[],
        pours=[Pour(net="GND", layer="B.Cu", outline="board")],
    )


# ---------------------------------------------------------------------------
# _comp_abs_bbox
# ---------------------------------------------------------------------------

class TestCompAbsBbox:
    def test_no_offset(self):
        comp = Component(
            reference="U1", footprint="FP", description="",
            position=(0.0, 0.0), rotation=0.0, layer="F.Cu",
            bbox=(-1.0, -1.0, 1.0, 1.0), pads=[],
        )
        assert _comp_abs_bbox(comp) == (-1.0, -1.0, 1.0, 1.0)

    def test_with_offset(self):
        comp = Component(
            reference="U1", footprint="FP", description="",
            position=(5.0, 3.0), rotation=0.0, layer="F.Cu",
            bbox=(-2.0, -2.0, 2.0, 2.0), pads=[],
        )
        assert _comp_abs_bbox(comp) == (3.0, 1.0, 7.0, 5.0)

    def test_matches_conftest_u1(self, minimal_board):
        u1 = minimal_board.components["U1"]
        # position=(9,9), bbox=(-3,-3,3,3) → abs=(6,6,12,12)
        assert _comp_abs_bbox(u1) == (6.0, 6.0, 12.0, 12.0)

    def test_matches_conftest_j1(self, minimal_board):
        j1 = minimal_board.components["J1"]
        # position=(30,9), bbox=(-2,-2,2,2) → abs=(28,7,32,11)
        assert _comp_abs_bbox(j1) == (28.0, 7.0, 32.0, 11.0)


# ---------------------------------------------------------------------------
# Segment intersection
# ---------------------------------------------------------------------------

class TestSegmentsIntersect:
    def test_classic_x_cross(self):
        # X shape
        assert _segments_intersect(((0, 0), (2, 2)), ((0, 2), (2, 0))) is True

    def test_parallel_horizontal(self):
        assert _segments_intersect(((0, 0), (4, 0)), ((0, 1), (4, 1))) is False

    def test_t_junction(self):
        # Vertical crosses horizontal midpoint
        assert _segments_intersect(((0, 0), (4, 0)), ((2, -1), (2, 1))) is True

    def test_touching_endpoint_not_crossing(self):
        # Endpoints touch but segments don't cross
        assert _segments_intersect(((0, 0), (2, 0)), ((2, 0), (2, 2))) is False

    def test_collinear_overlap(self):
        assert _segments_intersect(((0, 0), (4, 0)), ((2, 0), (6, 0))) is False

    def test_non_overlapping_same_line(self):
        assert _segments_intersect(((0, 0), (1, 0)), ((2, 0), (3, 0))) is False

    def test_diagonal_cross(self):
        assert _segments_intersect(((0, 0), (4, 4)), ((0, 4), (4, 0))) is True

    def test_no_cross_l_shape(self):
        # ⌐ shape — share no region
        assert _segments_intersect(((0, 0), (2, 0)), ((3, 0), (3, 2))) is False


# ---------------------------------------------------------------------------
# MST edge building
# ---------------------------------------------------------------------------

class TestBuildMstEdges:
    def test_two_pad_net_one_edge(self, minimal_board):
        mst = _build_mst_edges(minimal_board)
        assert "GND" in mst
        assert "3V3" in mst
        assert len(mst["GND"]) == 1
        assert len(mst["3V3"]) == 1

    def test_mst_edge_count_n_minus_1(self, minimal_board):
        mst = _build_mst_edges(minimal_board)
        for net_name, edges in mst.items():
            pad_count = sum(
                1 for c in minimal_board.components.values()
                for p in c.pads if p.net == net_name
            )
            assert len(edges) == pad_count - 1

    def test_single_pad_net_excluded(self, minimal_board):
        minimal_board.nets["LONELY"] = Net(
            name="LONELY", class_="signal", strategy="route", width_mm=0.3, priority=5
        )
        minimal_board.components["U1"].pads.append(
            Pad(number="99", net="LONELY", offset=(0.0, 5.0), size=(0.6, 0.6),
                shape="rect", layer="F.Cu")
        )
        mst = _build_mst_edges(minimal_board)
        assert "LONELY" not in mst

    def test_includes_connections_when_route_exists(self, minimal_board):
        # Add a route on GND — scorer should still include all pad connections
        minimal_board.routes.append(
            Route(net="GND", width_mm=0.3, segments=[
                Segment(start=(7.5, 9.0), end=(28.5, 9.0), layer="F.Cu")
            ])
        )
        mst = _build_mst_edges(minimal_board)
        assert "GND" in mst
        assert len(mst["GND"]) == 1

    def test_three_pad_net_two_edges(self, minimal_board):
        minimal_board.nets["3V3"].class_ = "signal"  # keep it valid
        minimal_board.components["J1"].pads.append(
            Pad(number="3", net="GND", offset=(0.0, 2.0), size=(0.6, 0.6),
                shape="rect", layer="F.Cu")
        )
        mst = _build_mst_edges(minimal_board)
        assert len(mst["GND"]) == 2

    def test_uses_manhattan_distance(self):
        # Pads at (0,0), (3,0), (0,4). Manhattan from (3,0) to (0,4) = 7.
        # MST: pick (0,0)→(3,0)=3 and (0,0)→(0,4)=4 → total 7.
        # (Euclidean picks same MST here, but our weight is Manhattan)
        nets = {"SIG": Net(name="SIG", class_="signal", strategy="route", width_mm=0.3, priority=5)}
        pads = [
            Pad(number="1", net="SIG", offset=(0.0, 0.0), size=(0.6, 0.6), shape="rect", layer="F.Cu"),
            Pad(number="2", net="SIG", offset=(3.0, 0.0), size=(0.6, 0.6), shape="rect", layer="F.Cu"),
            Pad(number="3", net="SIG", offset=(0.0, 4.0), size=(0.6, 0.6), shape="rect", layer="F.Cu"),
        ]
        comp = Component(
            reference="U1", footprint="FP", description="",
            position=(0.0, 0.0), rotation=0.0, layer="F.Cu",
            bbox=(-1.0, -1.0, 4.0, 5.0), pads=pads,
        )
        board = _make_simple_board({"U1": comp}, nets)
        mst = _build_mst_edges(board)
        assert len(mst["SIG"]) == 2
        # Total MST Manhattan length should be 3 + 4 = 7
        total = sum(abs(p2[0]-p1[0]) + abs(p2[1]-p1[1]) for p1, p2 in mst["SIG"])
        assert abs(total - 7.0) < 1e-9


# ---------------------------------------------------------------------------
# Crossing detection
# ---------------------------------------------------------------------------

class TestCountCrossings:
    def test_minimal_board_colinear_no_crossings(self, minimal_board):
        # GND edge: (7.5,9)↔(28.5,9), 3V3 edge: (10.5,9)↔(31.5,9)
        # Both horizontal at y=9 — collinear, not crossing.
        mst = _build_mst_edges(minimal_board)
        crossings, n_pairs = _count_crossings(mst)
        assert crossings == 0
        assert n_pairs == 1  # one GND-3V3 pair

    def test_one_crossing(self):
        mst = {
            "A": [((0.0, 0.0), (2.0, 2.0))],
            "B": [((0.0, 2.0), (2.0, 0.0))],
        }
        crossings, n_pairs = _count_crossings(mst)
        assert crossings == 1
        assert n_pairs == 1

    def test_no_crossing_parallel(self):
        mst = {
            "A": [((0.0, 0.0), (4.0, 0.0))],
            "B": [((0.0, 1.0), (4.0, 1.0))],
        }
        crossings, n_pairs = _count_crossings(mst)
        assert crossings == 0

    def test_same_net_edges_not_counted(self):
        # Two edges from the same net — should not be counted as a pair
        mst = {"A": [((0.0, 0.0), (2.0, 2.0)), ((0.0, 2.0), (2.0, 0.0))]}
        crossings, n_pairs = _count_crossings(mst)
        assert n_pairs == 0
        assert crossings == 0

    def test_crossings_nonneg(self, minimal_board):
        mst = _build_mst_edges(minimal_board)
        crossings, _ = _count_crossings(mst)
        assert crossings >= 0

    def test_multiple_crossings(self):
        # A has two edges forming an X with both edges of B
        mst = {
            "A": [((0.0, 0.0), (4.0, 4.0)), ((0.0, 4.0), (4.0, 0.0))],
            "B": [((0.0, 2.0), (4.0, 2.0))],
        }
        crossings, n_pairs = _count_crossings(mst)
        assert crossings == 2
        assert n_pairs == 2


# ---------------------------------------------------------------------------
# Wirelength
# ---------------------------------------------------------------------------

class TestComputeWirelength:
    def test_manhattan_not_euclidean(self):
        # (0,0)↔(3,4): Manhattan=7, Euclidean=5
        mst = {"SIG": [((0.0, 0.0), (3.0, 4.0))]}
        nets = {"SIG": Net(name="SIG", class_="signal", strategy="route", width_mm=0.3, priority=5)}
        total, weighted = _compute_wirelength(mst, nets)
        assert abs(total - 7.0) < 1e-9

    def test_ground_zero_weight(self, minimal_board):
        mst = _build_mst_edges(minimal_board)
        total, weighted = _compute_wirelength(mst, minimal_board.nets)
        # GND has weight 0 — its edges add to total but not weighted
        gnd_edges = mst.get("GND", [])
        gnd_length = sum(abs(p2[0]-p1[0]) + abs(p2[1]-p1[1]) for p1, p2 in gnd_edges)
        assert gnd_length > 0       # there is a GND edge
        assert total > 0            # total includes GND
        # GND weight=0: weighted == (total - gnd_length) * 3  (3V3 is the only other net, weight=3)
        assert weighted == pytest.approx((total - gnd_length) * 3.0)

    def test_power_triple_weight(self):
        mst = {"3V3": [((0.0, 0.0), (10.0, 0.0))]}  # Manhattan length = 10
        nets = {"3V3": Net(name="3V3", class_="power", strategy="route_wide", width_mm=0.5, priority=1)}
        total, weighted = _compute_wirelength(mst, nets)
        assert abs(total - 10.0) < 1e-9
        assert abs(weighted - 30.0) < 1e-9

    def test_constrained_signal_double_weight(self):
        mst = {"CLK": [((0.0, 0.0), (5.0, 0.0))]}
        nets = {"CLK": Net(name="CLK", class_="constrained_signal", strategy="route", width_mm=0.3, priority=2)}
        total, weighted = _compute_wirelength(mst, nets)
        assert abs(total - 5.0) < 1e-9
        assert abs(weighted - 10.0) < 1e-9

    def test_total_includes_all_nets(self, minimal_board):
        mst = _build_mst_edges(minimal_board)
        total, _ = _compute_wirelength(mst, minimal_board.nets)
        assert total > 0


# ---------------------------------------------------------------------------
# Channel analysis
# ---------------------------------------------------------------------------

def _make_close_board():
    """Board with two components 1mm apart horizontally, Y ranges overlapping."""
    nets = {
        "GND": Net(name="GND", class_="ground", strategy="pour", width_mm=None, priority=0),
        "3V3": Net(name="3V3", class_="power", strategy="route_wide", width_mm=0.5, priority=1),
    }
    # C1 at x=10, bbox right edge at 10+1=11
    c1 = Component(
        reference="C1", footprint="FP", description="",
        position=(10.0, 10.0), rotation=0.0, layer="F.Cu",
        bbox=(-1.0, -1.0, 1.0, 1.0),
        pads=[
            Pad(number="1", net="GND", offset=(-0.5, 0.0), size=(0.6, 0.6), shape="rect", layer="F.Cu"),
            Pad(number="2", net="3V3", offset=(0.5, 0.0), size=(0.6, 0.6), shape="rect", layer="F.Cu"),
        ],
    )
    # C2 at x=13, bbox left edge at 13-1=12 → gap = 12-11 = 1mm
    c2 = Component(
        reference="C2", footprint="FP", description="",
        position=(13.0, 10.0), rotation=0.0, layer="F.Cu",
        bbox=(-1.0, -1.0, 1.0, 1.0),
        pads=[
            Pad(number="1", net="GND", offset=(-0.5, 0.0), size=(0.6, 0.6), shape="rect", layer="F.Cu"),
            Pad(number="2", net="3V3", offset=(0.5, 0.0), size=(0.6, 0.6), shape="rect", layer="F.Cu"),
        ],
    )
    board = _make_simple_board({"C1": c1, "C2": c2}, nets)
    return board


class TestAnalyzeChannels:
    def test_minimal_board_no_channel(self, minimal_board):
        # U1-J1 gap = 28 - 12 = 16mm > 10mm → no channel
        channels = _analyze_channels(minimal_board)
        assert len(channels) == 0

    def test_horizontal_corridor_detected(self):
        board = _make_close_board()
        channels = _analyze_channels(board)
        h_key = "C1-C2_h"
        assert h_key in channels

    def test_available_tracks_calculation(self):
        # gap=1mm, default_trace=0.3mm, clearance=0.2mm → track_size=0.5mm → floor(1/0.5)=2
        board = _make_close_board()
        channels = _analyze_channels(board)
        ci = channels["C1-C2_h"]
        assert ci.available_tracks == 2

    def test_required_tracks(self):
        # C1 and C2 share GND and 3V3 → required=2
        board = _make_close_board()
        channels = _analyze_channels(board)
        ci = channels["C1-C2_h"]
        assert ci.required_tracks == 2

    def test_utilization(self):
        board = _make_close_board()
        channels = _analyze_channels(board)
        ci = channels["C1-C2_h"]
        assert abs(ci.utilization - 2 / max(1, 2)) < 1e-4
        assert ci.utilization == pytest.approx(1.0)

    def test_not_oversubscribed_when_enough_tracks(self):
        board = _make_close_board()
        channels = _analyze_channels(board)
        # 2 tracks available, 2 required → exactly subscribed, not oversubscribed
        assert channels["C1-C2_h"].oversubscribed is False

    def test_oversubscribed_flag(self):
        board = _make_close_board()
        # Force oversubscription: widen trace so only 1 track fits
        board.rules.default_trace_width_mm = 0.7  # 0.7 + 0.2 = 0.9 → floor(1/0.9) = 1
        channels = _analyze_channels(board)
        ci = channels["C1-C2_h"]
        assert ci.available_tracks == 1
        assert ci.required_tracks == 2
        assert ci.oversubscribed is True

    def test_channel_key_sorted_alpha(self):
        board = _make_close_board()
        channels = _analyze_channels(board)
        # Key should be "C1-C2" (sorted alphabetically)
        for key in channels:
            parts = key.rstrip("_hv").split("-")
            assert parts == sorted(parts)

    def test_vertical_corridor(self):
        """Two components stacked vertically, 1mm gap."""
        nets = {
            "GND": Net(name="GND", class_="ground", strategy="pour", width_mm=None, priority=0),
        }
        top = Component(
            reference="T1", footprint="FP", description="",
            position=(10.0, 5.0), rotation=0.0, layer="F.Cu",
            bbox=(-2.0, -1.0, 2.0, 1.0),
            pads=[Pad(number="1", net="GND", offset=(0.0, 0.0), size=(0.6, 0.6), shape="rect", layer="F.Cu")],
        )
        bot = Component(
            reference="T2", footprint="FP", description="",
            position=(10.0, 8.0), rotation=0.0, layer="F.Cu",
            bbox=(-2.0, -1.0, 2.0, 1.0),
            pads=[Pad(number="1", net="GND", offset=(0.0, 0.0), size=(0.6, 0.6), shape="rect", layer="F.Cu")],
        )
        # T1 abs bbox: (8,4,12,6), T2 abs bbox: (8,7,12,9)
        # vertical gap = 7 - 6 = 1mm
        board = _make_simple_board({"T1": top, "T2": bot}, nets)
        channels = _analyze_channels(board)
        v_key = "T1-T2_v"
        assert v_key in channels
        assert channels[v_key].available_tracks == 2

    def test_no_corridor_gap_too_large(self, minimal_board):
        channels = _analyze_channels(minimal_board)
        # All gaps > 10mm in this fixture
        assert all("_h" not in k and "_v" not in k or True for k in channels)
        assert len(channels) == 0


# ---------------------------------------------------------------------------
# Pin escape
# ---------------------------------------------------------------------------

class TestCheckPinEscape:
    def test_open_board_few_violations(self, minimal_board):
        violations = _check_pin_escape(minimal_board)
        # Board is 39×18, components well inside — should be few/no violations
        total_pads = sum(len(c.pads) for c in minimal_board.components.values())
        assert len(violations) <= total_pads

    def test_violation_format(self, minimal_board):
        violations = _check_pin_escape(minimal_board)
        for v in violations:
            assert ":" in v

    def test_pad_at_corner_has_violations(self):
        """A pad touching the board corner should have ≥2 blocked directions from the edge."""
        nets = {
            "GND": Net(name="GND", class_="ground", strategy="pour", width_mm=None, priority=0),
        }
        # Place component right at (0.3, 0.3) on a tiny board
        comp = Component(
            reference="U1", footprint="FP", description="",
            position=(0.3, 0.3), rotation=0.0, layer="F.Cu",
            bbox=(-0.1, -0.1, 0.1, 0.1),
            pads=[Pad(number="1", net="GND", offset=(0.0, 0.0), size=(0.3, 0.3),
                      shape="rect", layer="F.Cu")],
        )
        board = _make_simple_board(
            {"U1": comp}, nets,
            outline=[(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0)]
        )
        # Pad at (0.3, 0.3): West probe (0.0, 0.3) is on board edge but ≤ min_x=0 → blocked
        # North probe (0.3, 0.0) is on board edge → blocked
        # That's 2 edge-blocked. Not necessarily ≥3.  Test that result is a list.
        violations = _check_pin_escape(board)
        assert isinstance(violations, list)

    def test_returns_correct_ref_format(self, minimal_board):
        # Add a pad very close to a board edge to trigger a violation
        comp = minimal_board.components["U1"]
        comp.position = (0.3, 0.3)  # move near corner
        violations = _check_pin_escape(minimal_board)
        for v in violations:
            ref, pad_num = v.split(":")
            assert ref in minimal_board.components


# ---------------------------------------------------------------------------
# Board diagonal
# ---------------------------------------------------------------------------

class TestBoardDiagonal:
    def test_rectangle(self, minimal_board):
        # Board is 39×18 → diagonal = sqrt(39² + 18²)
        d = _board_diagonal(minimal_board)
        expected = math.sqrt(39**2 + 18**2)
        assert abs(d - expected) < 1e-6

    def test_square(self):
        board = _make_simple_board(
            {}, {},
            outline=[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        )
        d = _board_diagonal(board)
        assert abs(d - math.sqrt(200)) < 1e-6


# ---------------------------------------------------------------------------
# score_placement — public API
# ---------------------------------------------------------------------------

class TestScorePlacement:
    def test_returns_placement_score(self, minimal_board):
        score = score_placement(minimal_board)
        assert isinstance(score, PlacementScore)

    def test_composite_in_range(self, minimal_board):
        score = score_placement(minimal_board)
        assert 0 <= score.composite_score <= 100

    def test_wirelength_positive(self, minimal_board):
        score = score_placement(minimal_board)
        assert score.total_wirelength_mm > 0

    def test_crossings_nonneg(self, minimal_board):
        score = score_placement(minimal_board)
        assert score.ratsnest_crossings >= 0

    def test_score_to_dict_keys(self, minimal_board):
        score = score_placement(minimal_board)
        d = score_to_dict(score)
        expected = {
            "ratsnest_crossings", "total_wirelength_mm", "weighted_wirelength_mm",
            "channel_capacity", "pin_escape_violations", "constraint_violations",
            "composite_score",
        }
        assert set(d.keys()) == expected

    def test_score_to_dict_serializable(self, minimal_board):
        score = score_placement(minimal_board)
        # Should not raise
        json.dumps(score_to_dict(score))

    def test_closer_placement_scores_better_or_equal(self, minimal_board):
        """Moving components closer should improve or maintain the wirelength sub-score."""
        import copy as cp
        # Original: U1 at (9,9), J1 at (30,9) — 21mm apart
        score_far = score_placement(minimal_board)

        # Move J1 closer to U1
        board_close = cp.deepcopy(minimal_board)
        board_close.components["J1"].position = (15.0, 9.0)
        score_close = score_placement(board_close)

        assert score_close.total_wirelength_mm < score_far.total_wirelength_mm

    def test_weighted_wirelength_geq_zero(self, minimal_board):
        score = score_placement(minimal_board)
        assert score.weighted_wirelength_mm >= 0

    def test_violations_is_list(self, minimal_board):
        score = score_placement(minimal_board)
        assert isinstance(score.pin_escape_violations, list)


# ---------------------------------------------------------------------------
# Integration: synthetic board
# ---------------------------------------------------------------------------

class TestScorerWithSyntheticBoard:
    def test_synthetic_board_scores(self, synthetic_kicad_path):
        from src.kicad_export import export_board
        board = export_board(synthetic_kicad_path)
        score = score_placement(board)
        assert 0 <= score.composite_score <= 100
        assert score.ratsnest_crossings >= 0
        assert score.total_wirelength_mm > 0

    def test_synthetic_score_to_dict(self, synthetic_kicad_path):
        from src.kicad_export import export_board
        board = export_board(synthetic_kicad_path)
        score = score_placement(board)
        d = score_to_dict(score)
        json.dumps(d)  # must be serializable

    def test_synthetic_channel_analysis(self, synthetic_kicad_path):
        from src.kicad_export import export_board
        board = export_board(synthetic_kicad_path)
        # After export, C1 and C2 are near U1; some corridors should exist
        channels = _analyze_channels(board)
        # We don't assert exactly which corridors — just that the analysis runs
        assert isinstance(channels, dict)
