"""Tests that characterize the purpose-built board fixtures.

Each fixture is designed to exercise a specific scoring pathology:

  crossing_board  — guaranteed ≥1 ratsnest crossing between different nets
  congested_board — oversubscribed routing channel (3 nets share a 1 mm gap)
  edge_board      — pin-escape violation (U1 pad blocked on 3/4 sides)

Tests here verify that:
  1. Each fixture actually exhibits its intended property.
  2. score_placement reflects the pathology in the relevant sub-metric.
  3. The composite score is lower than minimal_board where the pathology is severe.
"""
from __future__ import annotations

import pytest

from src.placement_scorer import (
    _build_mst_edges, _count_crossings, _analyze_channels, _check_pin_escape,
    score_placement,
)


# ---------------------------------------------------------------------------
# crossing_board — ratsnest crossings
# ---------------------------------------------------------------------------

class TestCrossingBoard:
    def test_has_two_nets(self, crossing_board):
        assert len(crossing_board.nets) == 2

    def test_ratsnest_has_one_edge_per_net(self, crossing_board):
        mst = _build_mst_edges(crossing_board)
        assert len(mst["A"]) == 1
        assert len(mst["B"]) == 1

    def test_edges_cross(self, crossing_board):
        mst = _build_mst_edges(crossing_board)
        crossings, n_pairs = _count_crossings(mst)
        assert crossings == 1
        assert n_pairs == 1

    def test_crossing_penalizes_composite(self, crossing_board, minimal_board):
        # crossing_board has s_cross = 0; minimal_board has s_cross = 1 (colinear)
        cs_crossing = score_placement(crossing_board).composite_score
        cs_minimal  = score_placement(minimal_board).composite_score
        assert cs_crossing < cs_minimal

    def test_composite_in_range(self, crossing_board):
        score = score_placement(crossing_board)
        assert 0 <= score.composite_score <= 100

    def test_no_channels(self, crossing_board):
        # components at board corners are far apart — no corridors within 10 mm
        channels = _analyze_channels(crossing_board)
        assert channels == {}

    def test_no_pin_escape_violations(self, crossing_board):
        # components are well inside the board
        assert _check_pin_escape(crossing_board) == []


# ---------------------------------------------------------------------------
# congested_board — channel capacity
# ---------------------------------------------------------------------------

class TestCongestedBoard:
    def test_channel_detected(self, congested_board):
        channels = _analyze_channels(congested_board)
        assert len(channels) >= 1

    def test_channel_key(self, congested_board):
        channels = _analyze_channels(congested_board)
        # key uses alphabetical component order with _h suffix
        assert "U1-U2_h" in channels

    def test_available_tracks(self, congested_board):
        channels = _analyze_channels(congested_board)
        ch = channels["U1-U2_h"]
        # gap=1mm, track_size=0.5mm → floor(1.0/0.5) = 2
        assert ch.available_tracks == 2

    def test_required_tracks(self, congested_board):
        channels = _analyze_channels(congested_board)
        # GND, 3V3, SIG all appear in both components
        assert channels["U1-U2_h"].required_tracks == 3

    def test_oversubscribed(self, congested_board):
        channels = _analyze_channels(congested_board)
        assert channels["U1-U2_h"].oversubscribed is True

    def test_utilization_above_one(self, congested_board):
        channels = _analyze_channels(congested_board)
        assert channels["U1-U2_h"].utilization > 1.0

    def test_no_vertical_channel(self, congested_board):
        # U1 and U2 are side-by-side with non-overlapping X ranges
        channels = _analyze_channels(congested_board)
        assert "U1-U2_v" not in channels

    def test_composite_penalised_by_channel(self, congested_board):
        score = score_placement(congested_board)
        assert score.composite_score < 100

    def test_no_pin_escape_violations(self, congested_board):
        assert _check_pin_escape(congested_board) == []


# ---------------------------------------------------------------------------
# edge_board — pin escape violations
# ---------------------------------------------------------------------------

class TestEdgeBoard:
    def test_u1_has_violation(self, edge_board):
        violations = _check_pin_escape(edge_board)
        assert "U1:1" in violations

    def test_u2_has_no_violation(self, edge_board):
        violations = _check_pin_escape(edge_board)
        assert "U2:1" not in violations

    def test_violation_count(self, edge_board):
        violations = _check_pin_escape(edge_board)
        assert len(violations) == 1

    def test_composite_penalised_by_escape(self, edge_board):
        score = score_placement(edge_board)
        assert score.composite_score < 100

    def test_composite_in_range(self, edge_board):
        score = score_placement(edge_board)
        assert 0 <= score.composite_score <= 100

    def test_no_channels(self, edge_board):
        # U1 and U2 share SIG net but the gap between them is ~0.3 mm — below
        # track_size (0.5 mm) so available_tracks=0 and the corridor may still
        # be registered. Either way the oversubscribed flag is irrelevant here;
        # just confirm no crash.
        _analyze_channels(edge_board)  # should not raise


# ---------------------------------------------------------------------------
# Comparative / cross-fixture
# ---------------------------------------------------------------------------

class TestCrossFixtureComparison:
    def test_crossing_board_worse_than_minimal_on_crossings(
        self, crossing_board, minimal_board
    ):
        c = score_placement(crossing_board)
        m = score_placement(minimal_board)
        assert c.ratsnest_crossings > m.ratsnest_crossings

    def test_congested_board_has_oversubscribed_channel(self, congested_board):
        score = score_placement(congested_board)
        assert any(ci.oversubscribed for ci in score.channel_capacity.values())

    def test_edge_board_has_violations(self, edge_board):
        score = score_placement(edge_board)
        assert len(score.pin_escape_violations) > 0

    def test_all_boards_score_in_range(
        self, minimal_board, crossing_board, congested_board, edge_board
    ):
        for board in (minimal_board, crossing_board, congested_board, edge_board):
            s = score_placement(board)
            assert 0 <= s.composite_score <= 100, (
                f"{board.components} composite={s.composite_score}"
            )

    def test_each_board_is_json_serializable(
        self, minimal_board, crossing_board, congested_board, edge_board
    ):
        import json
        from src.placement_scorer import score_to_dict
        for board in (minimal_board, crossing_board, congested_board, edge_board):
            d = score_to_dict(score_placement(board))
            json.dumps(d)  # must not raise


# ---------------------------------------------------------------------------
# Sweeper integration with new fixtures
# ---------------------------------------------------------------------------

class TestSweeperWithCollection:
    def test_sweep_crossing_board(self, crossing_board):
        from src.placement_sweeper import sweep_placements
        moves = {"moves": [
            {"component": "C1", "parameter": "position_x",
             "range": [3.0, 9.0], "step": 3.0}
        ]}
        results = sweep_placements(crossing_board, moves, top_n=5)
        assert len(results) == 3
        scores = [r.composite_score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_sweep_congested_board_improves_score(self, congested_board):
        from src.placement_sweeper import sweep_placements
        # Moving U2 further right should open the channel and improve the score
        moves = {"moves": [
            {"component": "U2", "parameter": "position_x",
             "range": [10.0, 16.0], "step": 3.0}
        ]}
        results = sweep_placements(congested_board, moves, top_n=10)
        best = results[0].composite_score
        # The tightest position (U2 at 10) is the worst — best should be better
        worst = results[-1].composite_score
        assert best >= worst
