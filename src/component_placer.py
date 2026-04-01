"""Initial component placement using EE best practices.

Places components from an unplaced state (piled at origin after schematic
import) using industry-standard rules:
1. Constrained components first (edge connectors, mounting holes)
2. Main ICs in center, oriented toward connected connectors
3. Decoupling caps within 1-2mm of IC power pins
4. Supporting passives near their connected IC pins
5. Remaining components to minimize wirelength

Respects placement keepout zones (areas where components can't go but
traces are OK — e.g., mating board clearance areas).

CLI:
    python -m src.component_placer board.json -o placed.json
    python -m src.component_placer board.json --constraints constraints.json -o placed.json
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.schema import (
    Board, Component, Keepout, Net, Pad, Placement, snap_to_grid,
    load_board, save_board,
)


# ---------------------------------------------------------------------------
# Component classification
# ---------------------------------------------------------------------------

_IC_FOOTPRINT_PATTERNS = (
    "SOP", "SOIC", "SSOP", "TSSOP", "QFP", "LQFP", "TQFP",
    "QFN", "DFN", "BGA", "SOT-23", "SOT-223", "TO-252", "TO-263",
    "Package_SO", "Package_QFP", "Package_BGA", "Package_DFN_QFN",
)

_PASSIVE_FOOTPRINT_PATTERNS = (
    "Resistor", "Capacitor", "Inductor", "R_", "C_", "L_",
)

_CONNECTOR_FOOTPRINT_PATTERNS = (
    "Connector", "PinSocket", "PinHeader", "RJ45", "USB", "JST",
    "Molex", "TE_", "Amphenol",
)

def _rotated_bbox(
    bbox: tuple[float, float, float, float], rotation: float,
) -> tuple[float, float, float, float]:
    """Rotate bbox corners by component rotation and return axis-aligned result.

    The bbox in board.json is in LOCAL footprint space (pre-rotation).
    This applies the KiCad clockwise rotation to get the actual extent
    relative to the anchor in board space.
    """
    if rotation == 0.0:
        return bbox
    rad = math.radians(-rotation)  # KiCad CW convention
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    corners = [
        (bbox[0], bbox[1]), (bbox[2], bbox[1]),
        (bbox[2], bbox[3]), (bbox[0], bbox[3]),
    ]
    rotated = [(x * cos_r - y * sin_r, x * sin_r + y * cos_r) for x, y in corners]
    rxs = [c[0] for c in rotated]
    rys = [c[1] for c in rotated]
    return (min(rxs), min(rys), max(rxs), max(rys))


_MOUNTING_FOOTPRINT_PATTERNS = (
    "MountingHole", "Mounting_Hole", "TestPoint",
)


def _classify_component(comp: Component) -> str:
    """Classify a component as 'ic', 'decoupling_cap', 'passive', 'connector',
    'mounting', or 'other'."""
    fp = comp.footprint
    if any(p in fp for p in _MOUNTING_FOOTPRINT_PATTERNS):
        return "mounting"
    if any(p in fp for p in _CONNECTOR_FOOTPRINT_PATTERNS):
        return "connector"
    if any(p in fp for p in _IC_FOOTPRINT_PATTERNS):
        return "ic"
    if any(p in fp for p in _PASSIVE_FOOTPRINT_PATTERNS):
        return "passive"
    # Jumpers, solder bridges, etc.
    if "Jumper" in fp or "SolderJumper" in fp:
        return "passive"
    return "other"


def _is_decoupling_cap(comp: Component, board: Board) -> bool:
    """True if this cap connects to both a power net and GND on an IC."""
    if _classify_component(comp) != "passive":
        return False
    if "C" not in comp.reference and "Cap" not in comp.footprint:
        return False
    pad_nets = {p.net for p in comp.pads if p.net}
    has_gnd = any(n in pad_nets for n in ("GND", "AGND", "DGND", "PGND"))
    has_power = any(
        board.nets.get(n, Net("", "signal", "route", None, 10)).class_ == "power"
        for n in pad_nets
    )
    return has_gnd and has_power


def _find_associated_ic(
    comp: Component, board: Board,
) -> Optional[tuple[str, str]]:
    """Find which IC this decoupling cap is associated with.

    Returns (ic_ref, power_net_name) or None.
    Looks for the IC that shares a power net with this cap.
    """
    cap_power_nets = set()
    for pad in comp.pads:
        if not pad.net:
            continue
        net = board.nets.get(pad.net)
        if net and net.class_ == "power":
            cap_power_nets.add(pad.net)

    for ref, other in board.components.items():
        if _classify_component(other) != "ic":
            continue
        for pad in other.pads:
            if pad.net in cap_power_nets:
                return (ref, pad.net)
    return None


# ---------------------------------------------------------------------------
# Keepout zone checking
# ---------------------------------------------------------------------------

def _component_overlaps_keepout(
    position: tuple[float, float],
    bbox: tuple[float, float, float, float],
    keepouts: list[Keepout],
) -> bool:
    """Check if a component at position with bbox overlaps any placement keepout."""
    cx, cy = position
    x0 = cx + bbox[0]
    y0 = cy + bbox[1]
    x1 = cx + bbox[2]
    y1 = cy + bbox[3]

    for ko in keepouts:
        if not ko.placement_only:
            continue
        if ko.type == "rect" and ko.rect is not None:
            kx, ky, kw, kh = ko.rect
            if x0 < kx + kw and x1 > kx and y0 < ky + kh and y1 > ky:
                return True
        elif ko.type == "circle" and ko.center is not None and ko.radius is not None:
            # Check if bbox corners are within circle
            kcx, kcy = ko.center
            kr = ko.radius
            for px, py in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]:
                if math.sqrt((px - kcx) ** 2 + (py - kcy) ** 2) < kr:
                    return True
    return False


def _component_overlaps_other(
    position: tuple[float, float],
    bbox: tuple[float, float, float, float],
    placed: dict[str, Component],
    min_gap_mm: float = 0.5,
) -> bool:
    """Check if a component at position overlaps any already-placed component."""
    cx, cy = position
    x0 = cx + bbox[0] - min_gap_mm
    y0 = cy + bbox[1] - min_gap_mm
    x1 = cx + bbox[2] + min_gap_mm
    y1 = cy + bbox[3] + min_gap_mm

    for ref, other in placed.items():
        ox, oy = other.position
        ob = _rotated_bbox(other.bbox, other.rotation)
        ox0 = ox + ob[0]
        oy0 = oy + ob[1]
        ox1 = ox + ob[2]
        oy1 = oy + ob[3]
        if x0 < ox1 and x1 > ox0 and y0 < oy1 and y1 > oy0:
            return True
    return False


def _is_inside_board(
    position: tuple[float, float],
    bbox: tuple[float, float, float, float],
    board: Board,
    margin_mm: float = 0.5,
) -> bool:
    """Check if component fits inside the board outline with margin."""
    outline_xs = [p[0] for p in board.board_outline]
    outline_ys = [p[1] for p in board.board_outline]
    bx0 = min(outline_xs) + margin_mm
    by0 = min(outline_ys) + margin_mm
    bx1 = max(outline_xs) - margin_mm
    by1 = max(outline_ys) - margin_mm

    cx, cy = position
    x0 = cx + bbox[0]
    y0 = cy + bbox[1]
    x1 = cx + bbox[2]
    y1 = cy + bbox[3]

    return x0 >= bx0 and y0 >= by0 and x1 <= bx1 and y1 <= by1


# ---------------------------------------------------------------------------
# Position finding
# ---------------------------------------------------------------------------

def _board_center(board: Board) -> tuple[float, float]:
    xs = [p[0] for p in board.board_outline]
    ys = [p[1] for p in board.board_outline]
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)


def _gravity_position(
    comp: Component,
    board: Board,
    placed: dict[str, Component],
) -> tuple[float, float]:
    """Find position that minimizes total wirelength to already-placed connected pads.

    Uses center-of-gravity of connected pad positions.
    """
    connected_positions: list[tuple[float, float]] = []
    comp_nets = {p.net for p in comp.pads if p.net}

    for ref, other in placed.items():
        for pad in other.pads:
            if pad.net in comp_nets:
                pos = other.pad_abs_position(pad)
                connected_positions.append(pos)

    if not connected_positions:
        return _board_center(board)

    avg_x = sum(p[0] for p in connected_positions) / len(connected_positions)
    avg_y = sum(p[1] for p in connected_positions) / len(connected_positions)

    # Clamp to inside the board (in case connected pads are off-board)
    if board.board_outline:
        oxs = [p[0] for p in board.board_outline]
        oys = [p[1] for p in board.board_outline]
        avg_x = max(min(oxs) + 2, min(max(oxs) - 2, avg_x))
        avg_y = max(min(oys) + 2, min(max(oys) - 2, avg_y))

    return (avg_x, avg_y)


def _find_valid_position(
    comp: Component,
    target: tuple[float, float],
    board: Board,
    placed: dict[str, Component],
    keepouts: list[Keepout],
    grid: float,
) -> tuple[float, float]:
    """Find a valid grid-snapped position near target that doesn't overlap anything."""
    # Use rotated bbox for all checks
    rbbox = _rotated_bbox(comp.bbox, comp.rotation)

    # Clamp target inside the board first (components often start off-board)
    if board.board_outline:
        oxs = [p[0] for p in board.board_outline]
        oys = [p[1] for p in board.board_outline]
        target = (
            max(min(oxs) + 2, min(max(oxs) - 2, target[0])),
            max(min(oys) + 2, min(max(oys) - 2, target[1])),
        )
    tx, ty = snap_to_grid(target[0], grid), snap_to_grid(target[1], grid)

    # Try target first
    if (_is_inside_board((tx, ty), rbbox, board)
            and not _component_overlaps_other((tx, ty), rbbox, placed)
            and not _component_overlaps_keepout((tx, ty), rbbox, keepouts)):
        return (tx, ty)

    # Spiral outward from target
    for radius_steps in range(1, 100):
        r = radius_steps * grid
        for angle_steps in range(8 * radius_steps):
            angle = (2 * math.pi * angle_steps) / (8 * radius_steps)
            cx = snap_to_grid(target[0] + r * math.cos(angle), grid)
            cy = snap_to_grid(target[1] + r * math.sin(angle), grid)
            if (_is_inside_board((cx, cy), rbbox, board)
                    and not _component_overlaps_other((cx, cy), rbbox, placed)
                    and not _component_overlaps_keepout((cx, cy), rbbox, keepouts)):
                return (cx, cy)

    # Fallback: board center
    return _board_center(board)


def _place_near_ic_pin(
    comp: Component,
    ic: Component,
    power_net: str,
    board: Board,
    placed: dict[str, Component],
    keepouts: list[Keepout],
    grid: float,
    offset_mm: float = 1.5,
) -> tuple[float, float]:
    """Place a decoupling cap near the IC's power pin."""
    # Find the IC's power pin position
    for pad in ic.pads:
        if pad.net == power_net:
            pin_pos = ic.pad_abs_position(pad)
            # Try placing offset from the pin in each direction
            candidates = [
                (pin_pos[0] + offset_mm, pin_pos[1]),
                (pin_pos[0] - offset_mm, pin_pos[1]),
                (pin_pos[0], pin_pos[1] + offset_mm),
                (pin_pos[0], pin_pos[1] - offset_mm),
            ]
            rbbox = _rotated_bbox(comp.bbox, comp.rotation)
            for cx, cy in candidates:
                pos = (snap_to_grid(cx, grid), snap_to_grid(cy, grid))
                if (_is_inside_board(pos, rbbox, board)
                        and not _component_overlaps_other(pos, rbbox, placed)
                        and not _component_overlaps_keepout(pos, rbbox, keepouts)):
                    return pos
            # Fall back to gravity position
            return _find_valid_position(comp, pin_pos, board, placed, keepouts, grid)

    return _find_valid_position(comp, ic.position, board, placed, keepouts, grid)


# ---------------------------------------------------------------------------
# Main placement function
# ---------------------------------------------------------------------------

def place_components(
    board: Board,
    constraints: Optional[dict] = None,
) -> Board:
    """Place components using EE rules. Respects constraints and keepout zones.

    Placement order:
    1. Constrained components (from constraints dict)
    2. Mounting holes (fixed position or near corners)
    3. Main ICs (center of available space, near connected connectors)
    4. Decoupling caps (within 1-2mm of IC power pins)
    5. Supporting passives (near connected IC pins)
    6. Everything else (minimize wirelength)
    """
    if constraints is None:
        constraints = {}

    grid = board.grid_step
    keepouts = [k for k in board.keepouts if k.placement_only]

    # Add placement keepouts from constraints
    for ko_def in constraints.get("placement_keepouts", []):
        if "rect" in ko_def:
            r = ko_def["rect"]
            keepouts.append(Keepout(
                type="rect", layers=["*.Cu"],
                rect=(r[0], r[1], r[2] - r[0], r[3] - r[1]),
                notes=ko_def.get("notes", ""),
                placement_only=True,
            ))

    # Track which components are placed
    locked_refs: set[str] = set()
    placed: dict[str, Component] = {}

    # --- Phase 1: Apply constraints (edge, fixed, alignment) ---
    for ref, constraint in constraints.items():
        if ref == "placement_keepouts":
            continue
        if ref not in board.components:
            continue
        comp = board.components[ref]
        c_type = constraint.get("constraint", "free")
        if c_type in ("edge", "fixed"):
            locked_refs.add(ref)
            placed[ref] = comp
            # Apply edge constraint positioning
            if c_type == "edge" and "edge" in constraint:
                edge = constraint["edge"]
                offset = constraint.get("offset_from_edge_mm", 0.0)
                rotation = constraint.get("allowed_rotations", [comp.rotation])
                if rotation:
                    comp.rotation = rotation[0]
                _snap_to_edge(comp, edge, offset, board, grid)
            comp.placement.constraint = c_type
            # Store alignment info on the placement
            if "align_group" in constraint:
                comp.placement.align_group = constraint["align_group"]
                comp.placement.align_axis = constraint.get("align_axis")
                comp.placement.spacing_mm = constraint.get("spacing_mm")

    # --- Phase 1b: Apply alignment groups ---
    # Group constrained components by align_group and apply spacing
    align_groups: dict[str, list[str]] = {}
    for ref, constraint in constraints.items():
        if ref == "placement_keepouts" or ref not in board.components:
            continue
        ag = constraint.get("align_group")
        if ag:
            align_groups.setdefault(ag, []).append(ref)

    for group_name, refs in align_groups.items():
        if len(refs) < 2:
            continue
        # Find the group's alignment axis and spacing
        first_constraint = constraints[refs[0]]
        axis = first_constraint.get("align_axis", "y")
        spacing = first_constraint.get("spacing_mm")

        # Get the shared coordinate from the first member
        comps = [board.components[r] for r in refs]
        if axis == "y":
            # Same Y, spread in X with spacing
            shared_y = comps[0].position[1]
            if spacing:
                center_x = sum(c.position[0] for c in comps) / len(comps)
                total_width = spacing * (len(refs) - 1)
                start_x = center_x - total_width / 2
                for i, comp in enumerate(comps):
                    comp.position = (
                        snap_to_grid(start_x + i * spacing, grid),
                        shared_y,
                    )
        elif axis == "x":
            # Same X, spread in Y with spacing
            shared_x = comps[0].position[0]
            if spacing:
                center_y = sum(c.position[1] for c in comps) / len(comps)
                total_height = spacing * (len(refs) - 1)
                start_y = center_y - total_height / 2
                for i, comp in enumerate(comps):
                    comp.position = (
                        shared_x,
                        snap_to_grid(start_y + i * spacing, grid),
                    )

    # --- Phase 2: Classify remaining components ---
    remaining = {ref: comp for ref, comp in board.components.items()
                 if ref not in locked_refs}

    ics = {ref: comp for ref, comp in remaining.items()
           if _classify_component(comp) == "ic"}
    decoupling = {ref: comp for ref, comp in remaining.items()
                  if _is_decoupling_cap(comp, board)}
    mounting = {ref: comp for ref, comp in remaining.items()
                if _classify_component(comp) == "mounting"}
    passives = {ref: comp for ref, comp in remaining.items()
                if ref not in ics and ref not in decoupling and ref not in mounting
                and _classify_component(comp) in ("passive", "other")}
    # Catch-all: unconstrained connectors and anything else
    rest = {ref: comp for ref, comp in remaining.items()
            if ref not in ics and ref not in decoupling and ref not in mounting
            and ref not in passives}

    # --- Phase 2b: Move ALL unplaced components inside the board first ---
    # Components from schematic import are often completely off-board.
    board_cx, board_cy = _board_center(board)
    for ref, comp in board.components.items():
        if ref in locked_refs:
            continue
        if not _is_inside_board(comp.position, _rotated_bbox(comp.bbox, comp.rotation), board, margin_mm=0):
            comp.position = (snap_to_grid(board_cx, grid), snap_to_grid(board_cy, grid))

    # --- Phase 3: Place mounting holes near corners ---
    corners = _board_corners(board, margin_mm=3.0)
    for i, (ref, comp) in enumerate(mounting.items()):
        target = corners[i % len(corners)]
        pos = _find_valid_position(comp, target, board, placed, keepouts, grid)
        comp.position = pos
        placed[ref] = comp

    # --- Phase 4: Place ICs ---
    for ref, comp in ics.items():
        # Gravity toward connected connectors
        target = _gravity_position(comp, board, placed)
        pos = _find_valid_position(comp, target, board, placed, keepouts, grid)
        comp.position = pos
        placed[ref] = comp

    # --- Phase 5: Place decoupling caps near IC power pins ---
    for ref, comp in decoupling.items():
        assoc = _find_associated_ic(comp, board)
        if assoc and assoc[0] in placed:
            ic_ref, power_net = assoc
            pos = _place_near_ic_pin(
                comp, placed[ic_ref], power_net, board, placed, keepouts, grid,
            )
        else:
            target = _gravity_position(comp, board, placed)
            pos = _find_valid_position(comp, target, board, placed, keepouts, grid)
        comp.position = pos
        placed[ref] = comp

    # --- Phase 6: Place remaining passives ---
    for ref, comp in passives.items():
        target = _gravity_position(comp, board, placed)
        pos = _find_valid_position(comp, target, board, placed, keepouts, grid)
        comp.position = pos
        placed[ref] = comp

    # --- Phase 7: Place unconstrained connectors and anything else ---
    for ref, comp in rest.items():
        target = _gravity_position(comp, board, placed)
        pos = _find_valid_position(comp, target, board, placed, keepouts, grid)
        comp.position = pos
        placed[ref] = comp

    return board


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snap_to_edge(
    comp: Component, edge: str, offset_mm: float,
    board: Board, grid: float,
) -> None:
    """Move component to the specified board edge, centered on the perpendicular axis.

    Accounts for the component's bbox so the entire footprint stays inside
    the board. The offset is from the board edge to the nearest edge of the
    component's footprint, not to its anchor point.
    """
    outline_xs = [p[0] for p in board.board_outline]
    outline_ys = [p[1] for p in board.board_outline]
    bx0 = min(outline_xs)
    by0 = min(outline_ys)
    bx1 = max(outline_xs)
    by1 = max(outline_ys)
    center_x = (bx0 + bx1) / 2
    center_y = (by0 + by1) / 2

    # bbox is in LOCAL (pre-rotation) space — must rotate to get actual extent
    bb = _rotated_bbox(comp.bbox, comp.rotation)

    # Offset from anchor to bbox center (for centering on the non-edge axis)
    bbox_center_offset_x = (bb[0] + bb[2]) / 2
    bbox_center_offset_y = (bb[1] + bb[3]) / 2

    if edge == "top":
        cy = by0 + offset_mm - bb[1]
        cx = center_x - bbox_center_offset_x
    elif edge == "bottom":
        cy = by1 - offset_mm - bb[3]
        cx = center_x - bbox_center_offset_x
    elif edge == "left":
        cx = bx0 + offset_mm - bb[0]
        cy = center_y - bbox_center_offset_y
    elif edge == "right":
        cx = bx1 - offset_mm - bb[2]
        cy = center_y - bbox_center_offset_y
    else:
        return

    comp.position = (snap_to_grid(cx, grid), snap_to_grid(cy, grid))


def _board_corners(
    board: Board, margin_mm: float = 3.0,
) -> list[tuple[float, float]]:
    """Return 4 corner positions with margin from board edges."""
    xs = [p[0] for p in board.board_outline]
    ys = [p[1] for p in board.board_outline]
    x0, x1 = min(xs) + margin_mm, max(xs) - margin_mm
    y0, y1 = min(ys) + margin_mm, max(ys) - margin_mm
    return [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Place components using EE rules")
    parser.add_argument("board_json", help="Input board.json")
    parser.add_argument("-o", "--output", required=True, help="Output board.json")
    parser.add_argument("--constraints", default=None,
                        help="Constraints JSON file")
    args = parser.parse_args()

    board = load_board(args.board_json)

    constraints = {}
    if args.constraints:
        with open(args.constraints) as f:
            constraints = json.load(f)

    board = place_components(board, constraints)
    save_board(board, args.output)

    n_placed = len(board.components)
    print(f"Placed {n_placed} components → {args.output}")


if __name__ == "__main__":
    main()
