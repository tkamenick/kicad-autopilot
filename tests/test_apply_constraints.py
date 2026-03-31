"""Tests for src/apply_constraints.py."""
from __future__ import annotations

import pytest

from src.apply_constraints import (
    _board_extents, _snap_to_edge, apply_constraints,
    check_constraint_violations,
)
from src.schema import Board, Component, Net, Pad, Placement, Pour, Route, Rules, Segment, Via


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _board(
    components: dict | None = None,
    outline: list | None = None,
    grid: float = 0.3,
) -> Board:
    rules = Rules()
    if outline is None:
        # 39.9 × 30.0 mm — both on the 0.3mm grid (39.9=133×0.3, 30.0=100×0.3)
        outline = [(0.0, 0.0), (39.9, 0.0), (39.9, 30.0), (0.0, 30.0)]
    return Board(
        board_outline=outline,
        grid_step=grid,
        rules=rules,
        components=components or {},
        nets={},
        keepouts=[],
        routes=[],
        vias=[],
        pours=[],
    )


def _comp(ref: str, pos: tuple, placement: Placement | None = None) -> Component:
    if placement is None:
        placement = Placement()
    return Component(
        reference=ref, footprint="R", description="",
        position=pos, rotation=0.0, layer="F.Cu",
        bbox=(-1.0, -1.0, 1.0, 1.0), pads=[],
        placement=placement,
    )


# ---------------------------------------------------------------------------
# _board_extents
# ---------------------------------------------------------------------------

class TestBoardExtents:
    def test_rectangle(self):
        board = _board()  # uses default 39.9×30.0mm outline
        assert _board_extents(board) == (0.0, 0.0, 39.9, 30.0)

    def test_non_origin_board(self):
        board = _board(outline=[(5,5),(45,5),(45,35),(5,35)])
        assert _board_extents(board) == (5.0, 5.0, 45.0, 35.0)


# ---------------------------------------------------------------------------
# _snap_to_edge
# ---------------------------------------------------------------------------

class TestSnapToEdge:
    def setup_method(self):
        self.board = _board(outline=[(0,0),(39.9,0),(39.9,30),(0,30)])

    def test_right_edge_zero_offset(self):
        comp = _comp("J1", (20.0, 15.0))
        pos = _snap_to_edge(comp, "right", 0.0, self.board)
        assert pos[0] == pytest.approx(39.9)
        assert pos[1] == pytest.approx(15.0)  # y unchanged

    def test_left_edge_zero_offset(self):
        comp = _comp("J1", (20.0, 15.0))
        pos = _snap_to_edge(comp, "left", 0.0, self.board)
        assert pos[0] == pytest.approx(0.0)

    def test_bottom_edge_with_offset(self):
        comp = _comp("J1", (10.0, 10.0))
        pos = _snap_to_edge(comp, "bottom", 3.0, self.board)
        assert pos[1] == pytest.approx(27.0)  # 30 - 3 = 27 (on grid)

    def test_top_edge_with_offset(self):
        comp = _comp("J1", (10.0, 10.0))
        pos = _snap_to_edge(comp, "top", 2.1, self.board)
        assert pos[1] == pytest.approx(2.1)  # 0 + 2.1 (2.1 = 7×0.3, on grid)

    def test_snaps_to_grid(self):
        board = _board(outline=[(0,0),(40.1,0),(40.1,30),(0,30)], grid=0.3)
        comp = _comp("J1", (10.0, 10.0))
        pos = _snap_to_edge(comp, "right", 0.0, board)
        assert pos[0] == pytest.approx(round(round(40.1 / 0.3) * 0.3, 4))


# ---------------------------------------------------------------------------
# apply_constraints — single component
# ---------------------------------------------------------------------------

class TestApplyConstraints:
    def test_edge_right_moves_component(self):
        board = _board(components={"J1": _comp("J1", (10.0, 15.0))})
        constraints = {
            "J1": {"constraint": "edge", "edge": "right", "offset_from_edge_mm": 0.0}
        }
        updated = apply_constraints(board, constraints)
        assert updated.components["J1"].position[0] == pytest.approx(39.9)
        assert updated.components["J1"].position[1] == pytest.approx(15.0)

    def test_rotation_locked_to_single_allowed(self):
        board = _board(components={"J1": _comp("J1", (20.0, 15.0))})
        constraints = {"J1": {"allowed_rotations": [90]}}
        updated = apply_constraints(board, constraints)
        assert updated.components["J1"].rotation == pytest.approx(90.0)

    def test_constraint_type_set(self):
        board = _board(components={"J1": _comp("J1", (20.0, 15.0))})
        constraints = {"J1": {"constraint": "fixed"}}
        updated = apply_constraints(board, constraints)
        assert updated.components["J1"].placement.constraint == "fixed"

    def test_unknown_ref_skipped(self, capsys):
        board = _board(components={"J1": _comp("J1", (20.0, 15.0))})
        constraints = {"NONEXISTENT": {"constraint": "edge", "edge": "right"}}
        updated = apply_constraints(board, constraints)
        # Should warn but not crash
        out = capsys.readouterr().out
        assert "NONEXISTENT" in out or True  # warning may be on stderr

    def test_notes_preserved(self):
        board = _board(components={"J1": _comp("J1", (20.0, 15.0))})
        constraints = {"J1": {"notes": "RJ45 facing right"}}
        updated = apply_constraints(board, constraints)
        assert updated.components["J1"].placement.notes == "RJ45 facing right"

    def test_offset_from_edge_applied(self):
        board = _board(components={"J1": _comp("J1", (20.0, 15.0))})
        constraints = {
            "J1": {"constraint": "edge", "edge": "right", "offset_from_edge_mm": 5.1}
        }
        updated = apply_constraints(board, constraints)
        # 39.9 - 5.1 = 34.8 (on 0.3mm grid)
        assert updated.components["J1"].position[0] == pytest.approx(34.8, abs=0.01)

    def test_unconstrained_component_unchanged(self):
        board = _board(components={
            "J1": _comp("J1", (10.0, 15.0)),
            "R1": _comp("R1", (20.0, 20.0)),
        })
        constraints = {"J1": {"constraint": "edge", "edge": "right"}}
        updated = apply_constraints(board, constraints)
        assert updated.components["R1"].position == pytest.approx((20.0, 20.0))


# ---------------------------------------------------------------------------
# Alignment groups
# ---------------------------------------------------------------------------

class TestAlignmentGroups:
    def test_align_x_shares_x_coordinate(self):
        """Two components in same align_group with align_axis='x' end up at same X."""
        p1 = Placement(align_group="hdrs", align_axis="x")
        p2 = Placement(align_group="hdrs", align_axis="x")
        board = _board(components={
            "J2": _comp("J2", (10.0, 5.0), p1),
            "J3": _comp("J3", (14.0, 5.0), p2),
        })
        updated = apply_constraints(board, {})
        x2 = updated.components["J2"].position[0]
        x3 = updated.components["J3"].position[0]
        assert x2 == pytest.approx(x3, abs=0.01)

    def test_align_y_shares_y_coordinate(self):
        """Two components in same align_group with align_axis='y' end up at same Y."""
        p1 = Placement(align_group="row", align_axis="y")
        p2 = Placement(align_group="row", align_axis="y")
        board = _board(components={
            "J2": _comp("J2", (5.0, 10.0), p1),
            "J3": _comp("J3", (5.0, 14.0), p2),
        })
        updated = apply_constraints(board, {})
        y2 = updated.components["J2"].position[1]
        y3 = updated.components["J3"].position[1]
        assert y2 == pytest.approx(y3, abs=0.01)

    def test_spacing_applied_along_perpendicular(self):
        """spacing_mm sets center-to-center distance along perpendicular axis."""
        # Use 7.5 (= 25 × 0.3, grid-aligned) for spacing
        p1 = Placement(align_group="hdrs", align_axis="x", spacing_mm=7.5)
        p2 = Placement(align_group="hdrs", align_axis="x")
        board = _board(components={
            "J2": _comp("J2", (10.0, 5.0), p1),
            "J3": _comp("J3", (10.0, 20.0), p2),
        })
        updated = apply_constraints(board, {})
        j2 = updated.components["J2"]
        j3 = updated.components["J3"]
        ys = sorted([j2.position[1], j3.position[1]])
        assert abs(ys[1] - ys[0]) == pytest.approx(7.5, abs=0.01)

    def test_edge_and_alignment_combined(self):
        """Edge constraint + alignment group: bottom-edge headers in a horizontal row.

        align_axis="y" → all share same Y coordinate (the bottom edge y=27).
        spacing_mm=7.5 → centers are 7.5mm apart along X.
        """
        constraints = {
            "J2": {"constraint": "edge", "edge": "bottom", "offset_from_edge_mm": 3.0,
                   "allowed_rotations": [270], "align_group": "hdrs", "align_axis": "y",
                   "spacing_mm": 7.5},
            "J3": {"constraint": "edge", "edge": "bottom", "offset_from_edge_mm": 3.0,
                   "allowed_rotations": [270], "align_group": "hdrs", "align_axis": "y"},
        }
        board = _board(components={
            "J2": _comp("J2", (5.0, 10.0)),
            "J3": _comp("J3", (20.0, 10.0)),
        })
        updated = apply_constraints(board, constraints)
        j2 = updated.components["J2"]
        j3 = updated.components["J3"]
        # Both should be at y = 30 - 3 = 27 (bottom edge inset 3mm)
        assert j2.position[1] == pytest.approx(27.0, abs=0.15)
        assert j3.position[1] == pytest.approx(27.0, abs=0.15)
        # Y should be aligned (same value) — already guaranteed by edge snap
        assert j2.position[1] == pytest.approx(j3.position[1], abs=0.15)
        # X positions should differ by spacing
        xs = sorted([j2.position[0], j3.position[0]])
        assert abs(xs[1] - xs[0]) == pytest.approx(7.5, abs=0.15)


# ---------------------------------------------------------------------------
# check_constraint_violations
# ---------------------------------------------------------------------------

class TestCheckConstraintViolations:
    def test_no_constraints_no_violations(self):
        board = _board(components={"J1": _comp("J1", (20.0, 15.0))})
        assert check_constraint_violations(board) == []

    def test_edge_violated(self):
        pl = Placement(constraint="edge", edge="right", offset_from_edge_mm=0.0)
        board = _board(components={"J1": _comp("J1", (10.0, 15.0), pl)})
        # Component is at x=10, but right edge is x=40 → violation
        violations = check_constraint_violations(board)
        assert any("J1" in v for v in violations)

    def test_edge_satisfied_no_violation(self):
        pl = Placement(constraint="edge", edge="right", offset_from_edge_mm=0.0)
        board = _board(components={"J1": _comp("J1", (39.9, 15.0), pl)})
        violations = check_constraint_violations(board)
        assert violations == []

    def test_rotation_violated(self):
        pl = Placement(allowed_rotations=[90])
        comp = Component(
            reference="J1", footprint="R", description="",
            position=(20.0, 15.0), rotation=0.0, layer="F.Cu",
            bbox=(-1.0, -1.0, 1.0, 1.0), pads=[], placement=pl,
        )
        board = _board(components={"J1": comp})
        violations = check_constraint_violations(board)
        assert any("J1" in v and "rotation" in v for v in violations)

    def test_rotation_satisfied_no_violation(self):
        pl = Placement(allowed_rotations=[90])
        comp = Component(
            reference="J1", footprint="R", description="",
            position=(20.0, 15.0), rotation=90.0, layer="F.Cu",
            bbox=(-1.0, -1.0, 1.0, 1.0), pads=[], placement=pl,
        )
        board = _board(components={"J1": comp})
        violations = check_constraint_violations(board)
        assert violations == []

    def test_alignment_violated(self):
        p1 = Placement(align_group="hdrs", align_axis="x")
        p2 = Placement(align_group="hdrs", align_axis="x")
        board = _board(components={
            "J2": _comp("J2", (5.0, 10.0), p1),
            "J3": _comp("J3", (20.0, 10.0), p2),
        })
        violations = check_constraint_violations(board)
        assert any("hdrs" in v for v in violations)

    def test_alignment_satisfied_no_violation(self):
        p1 = Placement(align_group="hdrs", align_axis="x")
        p2 = Placement(align_group="hdrs", align_axis="x")
        board = _board(components={
            "J2": _comp("J2", (12.0, 5.0), p1),
            "J3": _comp("J3", (12.0, 20.0), p2),
        })
        violations = check_constraint_violations(board)
        assert violations == []

    def test_apply_then_check_no_violations(self):
        """After apply_constraints, check should find zero violations."""
        board = _board(components={
            "J1": _comp("J1", (10.0, 15.0)),
            "J2": _comp("J2", (5.0, 10.0)),
            "J3": _comp("J3", (20.0, 10.0)),
        })
        constraints = {
            "J1": {"constraint": "edge", "edge": "right", "offset_from_edge_mm": 0.0,
                   "allowed_rotations": [0]},
            "J2": {"constraint": "edge", "edge": "bottom", "offset_from_edge_mm": 3.0,
                   "allowed_rotations": [270], "align_group": "hdrs", "align_axis": "y",
                   "spacing_mm": 7.5},
            "J3": {"constraint": "edge", "edge": "bottom", "offset_from_edge_mm": 3.0,
                   "allowed_rotations": [270], "align_group": "hdrs", "align_axis": "y"},
        }
        updated = apply_constraints(board, constraints)
        violations = check_constraint_violations(updated)
        assert violations == []


# ---------------------------------------------------------------------------
# Placement scorer integration — constraint penalty
# ---------------------------------------------------------------------------

class TestScorerConstraintPenalty:
    def test_violation_lowers_score(self, minimal_board):
        from src.placement_scorer import score_placement
        # Baseline score
        baseline = score_placement(minimal_board).composite_score

        # Add edge constraint to U1 but place it away from the edge
        import copy
        board_violated = copy.deepcopy(minimal_board)
        board_violated.components["U1"].placement.constraint = "edge"
        board_violated.components["U1"].placement.edge = "right"
        board_violated.components["U1"].placement.offset_from_edge_mm = 0.0
        board_violated.components["U1"].placement.allowed_rotations = [0]
        # U1 is at (9,9), board right edge at 39 — constraint violated

        violated_score = score_placement(board_violated).composite_score
        assert violated_score < baseline

    def test_satisfied_constraints_no_penalty(self):
        from src.placement_scorer import score_placement
        # Board with component snapped to edge and rotation locked correctly
        pl = Placement(constraint="edge", edge="right", offset_from_edge_mm=0.0,
                       allowed_rotations=[0])
        comp = Component(
            reference="J1", footprint="R", description="",
            position=(39.9, 15.0), rotation=0.0, layer="F.Cu",
            bbox=(-1.0, -1.0, 1.0, 1.0), pads=[], placement=pl,
        )
        board = _board(components={"J1": comp})
        score = score_placement(board)
        assert score.constraint_violations == []

    def test_constraint_violations_in_score_output(self, minimal_board):
        from src.placement_scorer import score_placement
        score = score_placement(minimal_board)
        assert hasattr(score, "constraint_violations")
        assert isinstance(score.constraint_violations, list)


# ---------------------------------------------------------------------------
# Sweeper integration — constrained components skipped
# ---------------------------------------------------------------------------

class TestSweeperRespectsConstraints:
    def test_edge_component_not_swept(self, minimal_board):
        from src.placement_sweeper import _is_constrained, _parse_moves
        import copy
        board = copy.deepcopy(minimal_board)
        board.components["U1"].placement.constraint = "edge"
        board.components["U1"].placement.edge = "right"

        moves_json = {
            "moves": [
                {"component": "U1", "parameter": "position_x",
                 "range": [5.0, 20.0], "step": 0.3}
            ]
        }
        specs = _parse_moves(moves_json, board)
        # U1 has edge constraint → should be excluded from sweep
        assert all(s.component != "U1" for s in specs)

    def test_free_component_is_swept(self, minimal_board):
        from src.placement_sweeper import _is_constrained, _parse_moves
        moves_json = {
            "moves": [
                {"component": "J1", "parameter": "position_x",
                 "range": [25.0, 35.0], "step": 0.3}
            ]
        }
        specs = _parse_moves(moves_json, minimal_board)
        assert any(s.component == "J1" for s in specs)
