"""Tests for src/placement_sweeper.py."""
from __future__ import annotations

import copy
import json

import pytest

from src.schema import Board, Component, Net, Pad, Placement, Pour, Rules
from src.placement_scorer import score_placement
from src.placement_sweeper import (
    MoveSpec, SweepResult,
    _expand_range, _filter_rotation_values, _parse_moves,
    _apply_moves, _build_moves_applied,
    sweep_placements, results_to_json,
)


# ---------------------------------------------------------------------------
# _expand_range
# ---------------------------------------------------------------------------

class TestExpandRange:
    def test_two_values(self):
        assert _expand_range([10.0, 10.3], 0.3) == pytest.approx([10.0, 10.3])

    def test_four_values(self):
        result = _expand_range([0.0, 2.7], 0.9)
        assert len(result) == 4
        assert result[0] == pytest.approx(0.0)
        assert result[-1] == pytest.approx(2.7)

    def test_single_value(self):
        result = _expand_range([5.0, 5.0], 1.0)
        assert result == pytest.approx([5.0])

    def test_step_larger_than_range(self):
        result = _expand_range([5.0, 6.0], 10.0)
        assert result == pytest.approx([5.0])

    def test_rotation_range(self):
        result = _expand_range([0.0, 270.0], 90.0)
        assert result == pytest.approx([0.0, 90.0, 180.0, 270.0])

    def test_endpoint_included_despite_float_drift(self):
        # 33 steps of 0.3 from 10.0 should reach 19.9; 34 steps should reach 20.2 > 20.0
        result = _expand_range([10.0, 20.0], 0.3)
        # Check last value is close to 20.0 or slightly above within tolerance
        assert result[-1] <= 20.1
        assert result[-1] >= 19.9


# ---------------------------------------------------------------------------
# _filter_rotation_values
# ---------------------------------------------------------------------------

class TestFilterRotationValues:
    def test_standard_allowed(self, minimal_board):
        # U1 has allowed_rotations=[0,90,180,270]
        values = [0.0, 45.0, 90.0, 135.0, 180.0]
        result = _filter_rotation_values(values, "U1", minimal_board)
        assert 0.0 in result
        assert 90.0 in result
        assert 180.0 in result
        assert 45.0 not in result
        assert 135.0 not in result

    def test_empty_allowed_accepts_multiples_of_90(self, minimal_board):
        comp = minimal_board.components["U1"]
        comp.placement = Placement(allowed_rotations=[])
        values = [0.0, 45.0, 90.0, 135.0, 180.0, 270.0]
        result = _filter_rotation_values(values, "U1", minimal_board)
        assert set(result) == {0.0, 90.0, 180.0, 270.0}

    def test_all_values_filtered(self, minimal_board):
        values = [30.0, 60.0, 120.0]
        result = _filter_rotation_values(values, "U1", minimal_board)
        assert result == []

    def test_unknown_component_returns_all(self, minimal_board):
        values = [0.0, 45.0, 90.0]
        result = _filter_rotation_values(values, "DOESNOTEXIST", minimal_board)
        assert result == values


# ---------------------------------------------------------------------------
# _parse_moves
# ---------------------------------------------------------------------------

class TestParseMoves:
    def test_position_move(self, minimal_board):
        moves_json = {"moves": [
            {"component": "U1", "parameter": "position_x", "range": [5.0, 8.0], "step": 1.5}
        ]}
        specs = _parse_moves(moves_json, minimal_board)
        assert len(specs) == 1
        assert specs[0].component == "U1"
        assert specs[0].parameter == "position_x"
        assert len(specs[0].values) == 3  # 5.0, 6.5, 8.0

    def test_rotation_filtered(self, minimal_board):
        moves_json = {"moves": [
            {"component": "U1", "parameter": "rotation", "range": [0.0, 360.0], "step": 90.0}
        ]}
        specs = _parse_moves(moves_json, minimal_board)
        # 360 not in allowed_rotations=[0,90,180,270] → filtered out
        assert 360.0 not in specs[0].values
        assert 0.0 in specs[0].values
        assert len(specs[0].values) == 4

    def test_empty_moves(self, minimal_board):
        specs = _parse_moves({"moves": []}, minimal_board)
        assert specs == []

    def test_multiple_specs(self, minimal_board):
        moves_json = {"moves": [
            {"component": "U1", "parameter": "position_x", "range": [5.0, 8.0], "step": 3.0},
            {"component": "J1", "parameter": "position_y", "range": [5.0, 8.0], "step": 3.0},
        ]}
        specs = _parse_moves(moves_json, minimal_board)
        assert len(specs) == 2


# ---------------------------------------------------------------------------
# _apply_moves
# ---------------------------------------------------------------------------

class TestApplyMoves:
    def test_position_x_applied(self, minimal_board):
        specs = [MoveSpec(component="U1", parameter="position_x", values=[15.0])]
        board_copy = _apply_moves(minimal_board, (15.0,), specs)
        assert board_copy.components["U1"].position[0] == pytest.approx(15.0)

    def test_position_y_snapped(self, minimal_board):
        specs = [MoveSpec(component="U1", parameter="position_y", values=[10.15])]
        board_copy = _apply_moves(minimal_board, (10.15,), specs)
        from src.schema import snap_to_grid
        expected = snap_to_grid(10.15, minimal_board.grid_step)
        assert board_copy.components["U1"].position[1] == pytest.approx(expected)

    def test_rotation_applied(self, minimal_board):
        specs = [MoveSpec(component="J1", parameter="rotation", values=[90.0])]
        board_copy = _apply_moves(minimal_board, (90.0,), specs)
        assert board_copy.components["J1"].rotation == pytest.approx(90.0)

    def test_original_not_mutated(self, minimal_board):
        original_pos = minimal_board.components["U1"].position
        specs = [MoveSpec(component="U1", parameter="position_x", values=[15.0])]
        _apply_moves(minimal_board, (15.0,), specs)
        assert minimal_board.components["U1"].position == original_pos

    def test_multiple_specs_same_component(self, minimal_board):
        specs = [
            MoveSpec(component="U1", parameter="position_x", values=[12.0]),
            MoveSpec(component="U1", parameter="rotation", values=[90.0]),
        ]
        board_copy = _apply_moves(minimal_board, (12.0, 90.0), specs)
        assert board_copy.components["U1"].position[0] == pytest.approx(12.0)
        assert board_copy.components["U1"].rotation == pytest.approx(90.0)

    def test_y_unchanged_when_only_x_moved(self, minimal_board):
        original_y = minimal_board.components["U1"].position[1]
        specs = [MoveSpec(component="U1", parameter="position_x", values=[15.0])]
        board_copy = _apply_moves(minimal_board, (15.0,), specs)
        assert board_copy.components["U1"].position[1] == pytest.approx(original_y)


# ---------------------------------------------------------------------------
# _build_moves_applied
# ---------------------------------------------------------------------------

class TestBuildMovesApplied:
    def test_single_component(self, minimal_board):
        specs = [MoveSpec(component="U1", parameter="position_x", values=[15.0])]
        board_copy = _apply_moves(minimal_board, (15.0,), specs)
        result = _build_moves_applied((15.0,), specs, board_copy)
        assert len(result) == 1
        assert result[0]["component"] == "U1"
        assert "position" in result[0]
        assert "rotation" in result[0]

    def test_groups_by_component(self, minimal_board):
        specs = [
            MoveSpec(component="U1", parameter="position_x", values=[12.0]),
            MoveSpec(component="U1", parameter="rotation", values=[90.0]),
        ]
        board_copy = _apply_moves(minimal_board, (12.0, 90.0), specs)
        result = _build_moves_applied((12.0, 90.0), specs, board_copy)
        assert len(result) == 1  # same component → one entry

    def test_shows_snapped_position(self, minimal_board):
        specs = [MoveSpec(component="U1", parameter="position_x", values=[10.15])]
        board_copy = _apply_moves(minimal_board, (10.15,), specs)
        result = _build_moves_applied((10.15,), specs, board_copy)
        from src.schema import snap_to_grid
        expected = snap_to_grid(10.15, minimal_board.grid_step)
        assert result[0]["position"][0] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# sweep_placements
# ---------------------------------------------------------------------------

class TestSweepPlacements:
    def _pos_x_sweep(self, board, ref="U1", values=None, top=10):
        if values is None:
            values = [5.0, 10.0, 15.0]
        moves_json = {"moves": [
            {"component": ref, "parameter": "position_x",
             "range": [values[0], values[-1]], "step": values[1] - values[0]}
        ]}
        return sweep_placements(board, moves_json, top_n=top)

    def test_returns_top_n(self, minimal_board):
        results = self._pos_x_sweep(minimal_board, top=2)
        assert len(results) <= 2

    def test_sorted_descending(self, minimal_board):
        results = self._pos_x_sweep(minimal_board, top=10)
        scores = [r.composite_score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_ranks_sequential(self, minimal_board):
        results = self._pos_x_sweep(minimal_board)
        for i, r in enumerate(results, start=1):
            assert r.rank == i

    def test_empty_moves_returns_empty(self, minimal_board):
        results = sweep_placements(minimal_board, {"moves": []})
        assert results == []

    def test_top_n_larger_than_variants(self, minimal_board):
        # 3 position values → 3 variants; top=100 → at most 3 returned
        results = self._pos_x_sweep(minimal_board, values=[5.0, 10.0, 15.0], top=100)
        assert len(results) == 3

    def test_moves_applied_has_required_keys(self, minimal_board):
        results = self._pos_x_sweep(minimal_board)
        assert len(results) > 0
        for key in ("component", "position", "rotation"):
            assert key in results[0].moves_applied[0]

    def test_composite_score_matches_metrics(self, minimal_board):
        results = self._pos_x_sweep(minimal_board)
        for r in results:
            assert r.composite_score == r.metrics["composite_score"]

    def test_two_parameter_sweep(self, minimal_board):
        moves_json = {"moves": [
            {"component": "U1", "parameter": "position_x", "range": [5.0, 8.0], "step": 3.0},
            {"component": "U1", "parameter": "position_y", "range": [5.0, 8.0], "step": 3.0},
        ]}
        results = sweep_placements(minimal_board, moves_json, top_n=10)
        # 2 x positions × 2 y positions = 4 variants
        assert len(results) == 4

    def test_filtered_rotation_reduces_variants(self, minimal_board):
        moves_json = {"moves": [
            {"component": "U1", "parameter": "rotation", "range": [0.0, 360.0], "step": 90.0}
        ]}
        results = sweep_placements(minimal_board, moves_json, top_n=10)
        # allowed=[0,90,180,270], 360 filtered out → 4 variants
        assert len(results) == 4

    def test_all_filtered_rotation_returns_empty(self, minimal_board):
        moves_json = {"moves": [
            {"component": "U1", "parameter": "rotation", "range": [30.0, 60.0], "step": 30.0}
        ]}
        results = sweep_placements(minimal_board, moves_json, top_n=10)
        assert results == []


# ---------------------------------------------------------------------------
# results_to_json
# ---------------------------------------------------------------------------

class TestResultsToJson:
    def _get_results(self, minimal_board):
        moves_json = {"moves": [
            {"component": "U1", "parameter": "position_x", "range": [5.0, 8.0], "step": 3.0}
        ]}
        return sweep_placements(minimal_board, moves_json, top_n=5)

    def test_json_serializable(self, minimal_board):
        results = self._get_results(minimal_board)
        json.dumps(results_to_json(results))  # should not raise

    def test_output_structure(self, minimal_board):
        results = self._get_results(minimal_board)
        for item in results_to_json(results):
            assert "rank" in item
            assert "composite_score" in item
            assert "moves_applied" in item
            assert "metrics" in item

    def test_rank_starts_at_one(self, minimal_board):
        results = self._get_results(minimal_board)
        if results:
            assert results_to_json(results)[0]["rank"] == 1

    def test_empty_results(self):
        assert results_to_json([]) == []


# ---------------------------------------------------------------------------
# Integration with synthetic board
# ---------------------------------------------------------------------------

class TestSweepWithSyntheticBoard:
    def test_full_sweep_pipeline(self, synthetic_kicad_path, tmp_path):
        from src.kicad_export import export_board
        from src.schema import save_board, load_board

        board = export_board(synthetic_kicad_path)
        json_path = tmp_path / "board.json"
        save_board(board, json_path)
        board2 = load_board(json_path)

        moves_json = {"moves": [
            {"component": "C1", "parameter": "position_x", "range": [19.0, 25.0], "step": 3.0}
        ]}
        results = sweep_placements(board2, moves_json, top_n=3)

        assert len(results) >= 1
        assert all(0 <= r.composite_score <= 100 for r in results)
        output = results_to_json(results)
        json.dumps(output)  # serializable
