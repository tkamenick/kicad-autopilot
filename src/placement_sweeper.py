"""Placement variant sweeper.

Given a board.json and a move specification, generates all placement variants
by sweeping component position/rotation parameters, scores each variant, and
returns the top-N results sorted by composite score.

Move spec format (moves.json):
    {
        "moves": [
            {"component": "C1", "parameter": "position_x", "range": [10.0, 20.0], "step": 0.3},
            {"component": "C1", "parameter": "rotation",   "range": [0, 270],     "step": 90}
        ]
    }

Parameters: "position_x" | "position_y" | "rotation"

CLI:
    python src/placement_sweeper.py board.json --moves moves.json --top 10
"""
from __future__ import annotations

import argparse
import copy
import itertools
import json
from dataclasses import dataclass
from pathlib import Path

from src.schema import Board, load_board, snap_to_grid
from src.placement_scorer import PlacementScore, score_placement, score_to_dict


# ---------------------------------------------------------------------------
# Internal dataclasses
# ---------------------------------------------------------------------------

@dataclass
class MoveSpec:
    component: str
    parameter: str        # "position_x" | "position_y" | "rotation"
    values: list[float]   # pre-expanded candidate values


@dataclass
class SweepResult:
    rank: int
    composite_score: float
    moves_applied: list[dict]  # [{"component", "position", "rotation"}, ...]
    metrics: dict              # score_to_dict output


# ---------------------------------------------------------------------------
# Range expansion
# ---------------------------------------------------------------------------

def _expand_range(range_: list[float], step: float) -> list[float]:
    """Generate inclusive list of values from range_[0] to range_[1] by step.

    Uses a 1e-9 tolerance at the end to guard against float-accumulation drift
    (e.g., 10.0 + 34*0.3 might be 20.000000000000004, which should include 20.0).
    """
    start, end = float(range_[0]), float(range_[1])
    if step <= 0:
        return [start]
    values: list[float] = []
    v = start
    while v <= end + 1e-9:
        values.append(round(v, 6))
        v += step
    return values


# ---------------------------------------------------------------------------
# Rotation filtering
# ---------------------------------------------------------------------------

def _filter_rotation_values(
    values: list[float], comp_ref: str, board: Board
) -> list[float]:
    """Filter rotation values against the component's allowed_rotations.

    If allowed_rotations is empty, accepts any multiple of 90 degrees.
    """
    comp = board.components.get(comp_ref)
    if comp is None:
        return values
    allowed = comp.placement.allowed_rotations
    if not allowed:
        return [v for v in values if abs(v % 90) < 1e-6 or abs((v % 90) - 90) < 1e-6]
    return [v for v in values if any(abs(v - a) < 1e-6 for a in allowed)]


# ---------------------------------------------------------------------------
# Move parsing
# ---------------------------------------------------------------------------

def _is_constrained(comp_ref: str, board: Board) -> bool:
    """Return True if the component has a constraint that prevents free movement."""
    comp = board.components.get(comp_ref)
    if comp is None:
        return False
    pl = comp.placement
    # "fixed" or "edge" constraints lock position; single-rotation locks rotation
    return pl.constraint in ("fixed", "edge") or bool(pl.align_group)


def _parse_moves(moves_json: dict, board: Board) -> list[MoveSpec]:
    """Convert raw moves.json dict into MoveSpec list with pre-expanded values.

    Moves targeting constrained components (edge/fixed/align_group) are silently
    skipped so the sweeper never violates placement constraints.
    """
    specs: list[MoveSpec] = []
    for m in moves_json.get("moves", []):
        ref = m["component"]
        if _is_constrained(ref, board):
            continue  # never sweep a constrained component
        values = _expand_range(m["range"], m["step"])
        if m["parameter"] == "rotation":
            values = _filter_rotation_values(values, ref, board)
        specs.append(MoveSpec(
            component=ref,
            parameter=m["parameter"],
            values=values,
        ))
    return specs


# ---------------------------------------------------------------------------
# Move application
# ---------------------------------------------------------------------------

def _apply_moves(
    board: Board,
    combination: tuple[float, ...],
    specs: list[MoveSpec],
) -> Board:
    """Deep-copy the board and apply a set of parameter values.

    Positions are snapped to the board grid. Rotations are assigned directly
    (they have already been filtered against allowed_rotations).

    Raises KeyError if a spec references a component not in the board.
    """
    b = copy.deepcopy(board)
    for spec, value in zip(specs, combination):
        comp = b.components[spec.component]
        if spec.parameter == "position_x":
            comp.position = (snap_to_grid(value, b.grid_step), comp.position[1])
        elif spec.parameter == "position_y":
            comp.position = (comp.position[0], snap_to_grid(value, b.grid_step))
        elif spec.parameter == "rotation":
            comp.rotation = float(value)
    return b


def _build_moves_applied(
    combination: tuple[float, ...],
    specs: list[MoveSpec],
    board_copy: Board,
) -> list[dict]:
    """Summarise the final component state (post-snap) for the output JSON.

    Groups by component reference so that multi-parameter moves on the same
    component produce a single entry showing the complete final state.
    """
    seen: dict[str, dict] = {}
    for spec in specs:
        ref = spec.component
        if ref not in seen:
            comp = board_copy.components[ref]
            seen[ref] = {
                "component": ref,
                "position": list(comp.position),
                "rotation": comp.rotation,
            }
    return list(seen.values())


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def sweep_placements(
    board: Board,
    moves_json: dict,
    top_n: int = 10,
) -> list[SweepResult]:
    """Generate all placement variants, score each, return top-N.

    Args:
        board:      The board to sweep.
        moves_json: Parsed moves.json dict with a "moves" list.
        top_n:      Maximum number of results to return.

    Returns:
        List of SweepResult sorted by composite_score descending.
    """
    specs = _parse_moves(moves_json, board)
    if not specs:
        return []

    value_lists = [s.values for s in specs]
    if any(len(v) == 0 for v in value_lists):
        return []

    raw: list[tuple[float, list[dict], dict]] = []
    for combination in itertools.product(*value_lists):
        board_copy = _apply_moves(board, combination, specs)
        score = score_placement(board_copy)
        moves_applied = _build_moves_applied(combination, specs, board_copy)
        raw.append((score.composite_score, moves_applied, score_to_dict(score)))

    raw.sort(key=lambda x: x[0], reverse=True)

    return [
        SweepResult(
            rank=rank,
            composite_score=comp_score,
            moves_applied=moves_applied,
            metrics=metrics,
        )
        for rank, (comp_score, moves_applied, metrics) in enumerate(raw[:top_n], start=1)
    ]


def results_to_json(results: list[SweepResult]) -> list[dict]:
    """Serialize SweepResult list to JSON-compatible dicts."""
    return [
        {
            "rank": r.rank,
            "composite_score": r.composite_score,
            "moves_applied": r.moves_applied,
            "metrics": r.metrics,
        }
        for r in results
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep PCB placement variants and score each")
    parser.add_argument("input", help="Input board.json file")
    parser.add_argument("--moves", required=True,
                        help="Move spec: path to moves.json OR inline JSON string")
    parser.add_argument("--top", type=int, default=10, help="Number of top results (default 10)")
    args = parser.parse_args()

    board = load_board(args.input)

    # Accept either a file path or an inline JSON string
    moves_arg = args.moves.strip()
    if moves_arg.startswith("{"):
        moves_json = json.loads(moves_arg)
    else:
        with open(moves_arg) as f:
            moves_json = json.load(f)

    results = sweep_placements(board, moves_json, top_n=args.top)
    print(json.dumps(results_to_json(results), indent=2))


if __name__ == "__main__":
    main()
