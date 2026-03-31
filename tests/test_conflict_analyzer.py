"""Tests for src/conflict_analyzer.py."""
from __future__ import annotations

import pytest

from src.conflict_analyzer import (
    ConflictReport, analyze_conflicts, conflict_to_dict,
)
from src.schema import Board, Component, Net, Pad, Placement, Pour, Route, Rules, Segment, Via


# ---------------------------------------------------------------------------
# Helpers (mirror conftest fixtures)
# ---------------------------------------------------------------------------

def _make_net(name, class_="signal", priority=10):
    return Net(name=name, class_=class_, strategy="route", width_mm=0.3, priority=priority)


def _comp(ref, pos, nets_list, bbox=(-2.0, -1.0, 2.0, 1.0)):
    pads = [Pad(number=str(i+1), net=n, offset=(float(i)*1.5 - 1.5, 0.0),
                size=(0.6, 0.6), shape="rect", layer="F.Cu")
            for i, n in enumerate(nets_list)]
    return Component(reference=ref, footprint="R", description="",
                     position=pos, rotation=0.0, layer="F.Cu",
                     bbox=bbox, pads=pads, placement=Placement())


def _board(components, nets, routes=None, vias=None):
    rules = Rules()
    return Board(
        board_outline=[(0,0),(40,0),(40,30),(0,30)],
        grid_step=0.3, rules=rules,
        components=components, nets=nets,
        keepouts=[], routes=routes or [], vias=vias or [],
        pours=[],
    )


# ---------------------------------------------------------------------------
# ConflictReport dataclass
# ---------------------------------------------------------------------------

class TestConflictReport:
    def test_to_dict(self):
        report = ConflictReport(
            net_difficulty={"A": 1.5},
            bottleneck_channels=[{"channel": "R1-R2_h"}],
            routing_order=["A"],
            estimated_via_count=3,
        )
        d = conflict_to_dict(report)
        assert d["routing_order"] == ["A"]
        assert d["estimated_via_count"] == 3
        assert d["net_difficulty"]["A"] == 1.5


# ---------------------------------------------------------------------------
# analyze_conflicts basics
# ---------------------------------------------------------------------------

class TestAnalyzeConflicts:
    def test_returns_conflict_report(self, minimal_board):
        result = analyze_conflicts(minimal_board)
        assert isinstance(result, ConflictReport)

    def test_routing_order_excludes_ground(self, minimal_board):
        result = analyze_conflicts(minimal_board)
        assert "GND" not in result.routing_order

    def test_all_non_ground_nets_in_order(self, minimal_board):
        result = analyze_conflicts(minimal_board)
        for name, net in minimal_board.nets.items():
            if net.class_ != "ground":
                assert name in result.routing_order

    def test_power_nets_before_signal(self):
        """Power nets (priority=1) should come before signal (priority=10)."""
        nets = {
            "PWR": _make_net("PWR", class_="power", priority=1),
            "SIG1": _make_net("SIG1", priority=10),
            "SIG2": _make_net("SIG2", priority=10),
        }
        r1 = _comp("R1", (5.0, 10.0), ["PWR", "SIG1"])
        r2 = _comp("R2", (15.0, 10.0), ["PWR", "SIG2"])
        board = _board({"R1": r1, "R2": r2}, nets)
        result = analyze_conflicts(board)
        order = result.routing_order
        assert "PWR" in order
        pwr_idx = order.index("PWR")
        for sig in ("SIG1", "SIG2"):
            if sig in order:
                assert pwr_idx < order.index(sig)

    def test_difficulty_scores_non_negative(self, minimal_board):
        result = analyze_conflicts(minimal_board)
        for name, score in result.net_difficulty.items():
            assert score >= 0.0

    def test_estimated_via_count_non_negative(self, minimal_board):
        result = analyze_conflicts(minimal_board)
        assert result.estimated_via_count >= 0

    def test_bottleneck_channels_structure(self, congested_board):
        """Congested board should have at least one bottleneck channel."""
        result = analyze_conflicts(congested_board)
        assert len(result.bottleneck_channels) > 0
        ch = result.bottleneck_channels[0]
        assert "channel" in ch
        assert "available_tracks" in ch
        assert "required_tracks" in ch
        assert ch["required_tracks"] > ch["available_tracks"]

    def test_no_bottlenecks_on_sparse_board(self):
        """Components far apart with few shared nets → no bottlenecks."""
        nets = {
            "A": _make_net("A"),
            "B": _make_net("B"),
        }
        # Place components far apart — no channels should form
        r1 = _comp("R1", (5.0, 5.0), ["A"], bbox=(-1.0, -1.0, 1.0, 1.0))
        r2 = _comp("R2", (35.0, 25.0), ["B"], bbox=(-1.0, -1.0, 1.0, 1.0))
        board = _board({"R1": r1, "R2": r2}, nets)
        result = analyze_conflicts(board)
        assert result.bottleneck_channels == []

    def test_empty_board(self):
        """Board with no components should return empty report."""
        board = _board({}, {})
        result = analyze_conflicts(board)
        assert result.routing_order == []
        assert result.net_difficulty == {}
        assert result.estimated_via_count == 0

    def test_higher_difficulty_for_congested(self, congested_board, minimal_board):
        """Congested board should have higher difficulty scores than minimal."""
        congested_result = analyze_conflicts(congested_board)
        minimal_result = analyze_conflicts(minimal_board)
        max_congested = max(congested_result.net_difficulty.values(), default=0)
        max_minimal = max(minimal_result.net_difficulty.values(), default=0)
        assert max_congested >= max_minimal

    def test_difficulty_is_dict_of_floats(self, minimal_board):
        result = analyze_conflicts(minimal_board)
        for k, v in result.net_difficulty.items():
            assert isinstance(k, str)
            assert isinstance(v, float)

    def test_routing_order_only_contains_board_nets(self, minimal_board):
        result = analyze_conflicts(minimal_board)
        for name in result.routing_order:
            assert name in minimal_board.nets
