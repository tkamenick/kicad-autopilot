"""Apply placement constraints to a board.json.

Reads a constraints.json file (produced by the /pcb-constrain agent skill)
and applies each constraint to the matching component in board.json:

  - edge-snapped:  moves component to the specified board edge + offset
  - alignment groups: aligns all group members to a shared coordinate
  - spacing: distributes group members at the specified center-to-center gap
  - allowed_rotations / rotation lock: updates placement.allowed_rotations
  - fixed: marks position + rotation as locked (sweeper will skip)

constraints.json format:
    {
      "J1": {
        "constraint": "edge",
        "edge": "right",
        "allowed_rotations": [0],
        "offset_from_edge_mm": 0.0,
        "notes": "RJ45 facing outward on right edge"
      },
      "J2": {
        "constraint": "edge",
        "edge": "bottom",
        "allowed_rotations": [270],
        "offset_from_edge_mm": 3.0,
        "align_group": "headers",
        "align_axis": "x",
        "spacing_mm": 7.62,
        "notes": "Header row, 7.62mm pitch"
      }
    }

CLI:
    python src/apply_constraints.py board.json constraints.json -o board.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.schema import Board, Component, Placement, load_board, save_board, snap_to_grid


# ---------------------------------------------------------------------------
# Edge position computation
# ---------------------------------------------------------------------------

def _board_extents(board: Board) -> tuple[float, float, float, float]:
    """Return (min_x, min_y, max_x, max_y) from board outline."""
    xs = [pt[0] for pt in board.board_outline]
    ys = [pt[1] for pt in board.board_outline]
    return min(xs), min(ys), max(xs), max(ys)


def _snap_to_edge(
    comp: Component,
    edge: str,
    offset_mm: float,
    board: Board,
) -> tuple[float, float]:
    """Return the position that places comp's center at 'offset_mm' from 'edge'.

    offset_mm=0 puts the anchor exactly at the board edge.
    Positive offset_mm moves it inward.
    """
    min_x, min_y, max_x, max_y = _board_extents(board)
    grid = board.grid_step
    x, y = comp.position

    if edge == "left":
        x = snap_to_grid(min_x + offset_mm, grid)
    elif edge == "right":
        x = snap_to_grid(max_x - offset_mm, grid)
    elif edge == "top":
        y = snap_to_grid(min_y + offset_mm, grid)
    elif edge == "bottom":
        y = snap_to_grid(max_y - offset_mm, grid)

    return (x, y)


# ---------------------------------------------------------------------------
# Alignment group processing
# ---------------------------------------------------------------------------

def _apply_alignment_groups(board: Board) -> Board:
    """Align and space components within each alignment group.

    For each group:
    - Compute the shared coordinate (mean of current positions, snapped to grid).
    - Sort members along the perpendicular axis.
    - Apply spacing_mm if specified on any member (use first non-None value found).
    """
    # Collect groups: {group_name: [ref, ...]}
    groups: dict[str, list[str]] = {}
    for ref, comp in board.components.items():
        g = comp.placement.align_group
        if g:
            groups.setdefault(g, []).append(ref)

    for group_name, refs in groups.items():
        if len(refs) < 2:
            continue

        comps = [board.components[r] for r in refs]
        axis = comps[0].placement.align_axis  # "x" or "y"
        if not axis:
            continue

        # Find spacing (first non-None among group members)
        spacing = next(
            (c.placement.spacing_mm for c in comps if c.placement.spacing_mm is not None),
            None,
        )

        if axis == "x":
            # All share the same X; sort by Y; apply Y spacing
            shared_x = snap_to_grid(
                sum(c.position[0] for c in comps) / len(comps),
                board.grid_step,
            )
            comps_sorted = sorted(comps, key=lambda c: c.position[1])
            for i, comp in enumerate(comps_sorted):
                new_x = shared_x
                if spacing is not None:
                    new_y = snap_to_grid(
                        comps_sorted[0].position[1] + i * spacing,
                        board.grid_step,
                    )
                else:
                    new_y = comp.position[1]
                board.components[comp.reference].position = (new_x, new_y)

        elif axis == "y":
            # All share the same Y; sort by X; apply X spacing
            shared_y = snap_to_grid(
                sum(c.position[1] for c in comps) / len(comps),
                board.grid_step,
            )
            comps_sorted = sorted(comps, key=lambda c: c.position[0])
            for i, comp in enumerate(comps_sorted):
                new_y = shared_y
                if spacing is not None:
                    new_x = snap_to_grid(
                        comps_sorted[0].position[0] + i * spacing,
                        board.grid_step,
                    )
                else:
                    new_x = comp.position[0]
                board.components[comp.reference].position = (new_x, new_y)

    return board


# ---------------------------------------------------------------------------
# Single-component constraint application
# ---------------------------------------------------------------------------

def _apply_constraint(
    comp: Component,
    constraint_dict: dict,
    board: Board,
) -> Component:
    """Apply a single constraint dict to a component. Returns updated component."""
    pl = comp.placement
    grid = board.grid_step

    # Build updated Placement
    new_placement = Placement(
        constraint=constraint_dict.get("constraint", pl.constraint),
        edge=constraint_dict.get("edge", pl.edge),
        allowed_rotations=constraint_dict.get("allowed_rotations", pl.allowed_rotations),
        notes=constraint_dict.get("notes", pl.notes),
        align_group=constraint_dict.get("align_group", pl.align_group),
        align_axis=constraint_dict.get("align_axis", pl.align_axis),
        spacing_mm=constraint_dict.get("spacing_mm", pl.spacing_mm),
        offset_from_edge_mm=constraint_dict.get("offset_from_edge_mm", pl.offset_from_edge_mm),
    )

    new_pos = comp.position
    new_rot = comp.rotation

    # Snap to edge
    if new_placement.constraint == "edge" and new_placement.edge:
        offset = new_placement.offset_from_edge_mm or 0.0
        new_pos = _snap_to_edge(comp, new_placement.edge, offset, board)

    # Lock rotation to first allowed value if list is length 1
    if len(new_placement.allowed_rotations) == 1:
        new_rot = float(new_placement.allowed_rotations[0])

    return Component(
        reference=comp.reference,
        footprint=comp.footprint,
        description=comp.description,
        position=new_pos,
        rotation=new_rot,
        layer=comp.layer,
        bbox=comp.bbox,
        pads=comp.pads,
        placement=new_placement,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def apply_constraints(board: Board, constraints: dict) -> Board:
    """Apply a constraints dict to a board, returning updated Board.

    constraints: {reference: {constraint fields...}, ...}
    """
    import copy
    updated_comps = dict(board.components)

    for ref, constraint_dict in constraints.items():
        if ref not in updated_comps:
            print(f"Warning: component '{ref}' in constraints not found in board — skipping")
            continue
        updated_comps[ref] = _apply_constraint(updated_comps[ref], constraint_dict, board)

    updated = Board(
        board_outline=board.board_outline,
        grid_step=board.grid_step,
        rules=board.rules,
        components=updated_comps,
        nets=board.nets,
        keepouts=board.keepouts,
        routes=board.routes,
        vias=board.vias,
        pours=board.pours,
    )

    # Process alignment groups (needs all components updated first)
    updated = _apply_alignment_groups(updated)
    return updated


def load_constraints(path: str | Path) -> dict:
    """Load a constraints.json file."""
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Constraint violation check
# ---------------------------------------------------------------------------

def check_constraint_violations(board: Board) -> list[str]:
    """Return a list of human-readable constraint violation strings.

    Checks each constrained component's current position/rotation against
    its declared constraints.
    """
    violations: list[str] = []
    min_x, min_y, max_x, max_y = _board_extents(board)
    grid = board.grid_step
    tol = grid / 2  # half a grid step tolerance

    for ref, comp in board.components.items():
        pl = comp.placement
        x, y = comp.position

        # Edge constraint check
        if pl.constraint == "edge" and pl.edge:
            offset = pl.offset_from_edge_mm or 0.0
            expected_x: float | None = None
            expected_y: float | None = None
            if pl.edge == "left":
                expected_x = snap_to_grid(min_x + offset, grid)
            elif pl.edge == "right":
                expected_x = snap_to_grid(max_x - offset, grid)
            elif pl.edge == "top":
                expected_y = snap_to_grid(min_y + offset, grid)
            elif pl.edge == "bottom":
                expected_y = snap_to_grid(max_y - offset, grid)

            if expected_x is not None and abs(x - expected_x) > tol:
                violations.append(
                    f"{ref}: edge={pl.edge} constraint violated — "
                    f"x={x:.3f} but expected {expected_x:.3f}"
                )
            if expected_y is not None and abs(y - expected_y) > tol:
                violations.append(
                    f"{ref}: edge={pl.edge} constraint violated — "
                    f"y={y:.3f} but expected {expected_y:.3f}"
                )

        # Rotation constraint check
        if pl.allowed_rotations and len(pl.allowed_rotations) == 1:
            expected_rot = float(pl.allowed_rotations[0])
            if abs(comp.rotation - expected_rot) > 1.0:
                violations.append(
                    f"{ref}: rotation={comp.rotation}° but constraint requires {expected_rot}°"
                )

        # Fixed constraint check
        if pl.constraint == "fixed":
            # Fixed components should never be moved — we can't check original
            # position here, but we surface it for the sweeper to guard against
            pass

    # Alignment group check
    groups: dict[str, list[Component]] = {}
    for comp in board.components.values():
        if comp.placement.align_group:
            groups.setdefault(comp.placement.align_group, []).append(comp)

    for group_name, comps in groups.items():
        if len(comps) < 2:
            continue
        axis = comps[0].placement.align_axis
        if axis == "x":
            xs = [c.position[0] for c in comps]
            if max(xs) - min(xs) > tol:
                violations.append(
                    f"align_group='{group_name}': X positions not aligned "
                    f"(spread {max(xs)-min(xs):.3f}mm > tolerance {tol:.3f}mm)"
                )
        elif axis == "y":
            ys = [c.position[1] for c in comps]
            if max(ys) - min(ys) > tol:
                violations.append(
                    f"align_group='{group_name}': Y positions not aligned "
                    f"(spread {max(ys)-min(ys):.3f}mm > tolerance {tol:.3f}mm)"
                )

    return violations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply placement constraints to board.json"
    )
    parser.add_argument("board_json", help="Input board.json")
    parser.add_argument("constraints_json", help="Constraints JSON file")
    parser.add_argument("-o", "--output", default=None,
                        help="Output board.json (default: overwrite input)")
    parser.add_argument("--check-only", action="store_true",
                        help="Only report violations, do not apply")
    args = parser.parse_args()

    board = load_board(args.board_json)
    constraints = load_constraints(args.constraints_json)

    if args.check_only:
        violations = check_constraint_violations(board)
        if violations:
            print(f"{len(violations)} constraint violation(s):")
            for v in violations:
                print(f"  {v}")
        else:
            print("All constraints satisfied.")
        return

    updated = apply_constraints(board, constraints)

    # Report any violations that remain after applying
    violations = check_constraint_violations(updated)
    if violations:
        print(f"Warning: {len(violations)} constraint(s) could not be fully satisfied:")
        for v in violations:
            print(f"  {v}")

    n_constrained = sum(
        1 for c in updated.components.values()
        if c.placement.constraint != "free"
    )
    out_path = args.output or args.board_json
    save_board(updated, out_path)
    print(f"Applied constraints to {n_constrained} component(s) → {out_path}")


if __name__ == "__main__":
    main()
