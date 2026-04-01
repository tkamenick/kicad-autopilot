"""Board data model for board.json.

All coordinates are in millimeters, snapped to grid.
Origin is the board top-left corner (not the KiCad page origin).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def snap_to_grid(value: float, grid: float = 0.3) -> float:
    """Snap a coordinate to the nearest grid point."""
    return round(round(value / grid) * grid, 4)


# ---------------------------------------------------------------------------
# Core data classes
# ---------------------------------------------------------------------------

@dataclass
class Rules:
    min_clearance_mm: float = 0.2
    default_trace_width_mm: float = 0.3
    power_trace_width_mm: float = 0.5
    via_drill_mm: float = 0.3
    via_annular_ring_mm: float = 0.15
    layers: list[str] = field(default_factory=lambda: ["F.Cu", "B.Cu"])


@dataclass
class Pad:
    number: str
    net: str
    offset: tuple[float, float]   # relative to component anchor, footprint-local space
    size: tuple[float, float]
    shape: str                    # "rect" | "circle" | "oval" | "roundrect"
    layer: str                    # "F.Cu" | "B.Cu" | "*.Cu"


@dataclass
class Placement:
    constraint: str = "free"      # "fixed" | "edge" | "free"
    edge: Optional[str] = None    # "top" | "bottom" | "left" | "right"
    allowed_rotations: list[int] = field(default_factory=lambda: [0, 90, 180, 270])
    notes: str = ""
    # Alignment group: all components sharing the same group name get aligned.
    # align_axis="x" → all share the same X coordinate (vertically stacked).
    # align_axis="y" → all share the same Y coordinate (horizontally lined up).
    align_group: Optional[str] = None
    align_axis: Optional[str] = None   # "x" | "y"
    # Spacing between consecutive group members (center-to-center) along the
    # axis perpendicular to align_axis (i.e. if align_axis="x", spacing is in Y).
    spacing_mm: Optional[float] = None
    # Distance from the component anchor to the snapped board edge (mm).
    # 0.0 = center exactly at edge; positive = inset from edge.
    offset_from_edge_mm: Optional[float] = None


@dataclass
class Component:
    reference: str
    footprint: str
    description: str
    position: tuple[float, float]                   # board-origin absolute position
    rotation: float                                 # degrees, CCW
    layer: str
    bbox: tuple[float, float, float, float]         # x_min, y_min, x_max, y_max rel to anchor
    pads: list[Pad]
    placement: Placement = field(default_factory=Placement)

    def pad_abs_position(self, pad: Pad) -> tuple[float, float]:
        """Return absolute board position of a pad (applies component rotation).

        KiCad uses clockwise-positive rotation in a Y-down coordinate system,
        so we negate the angle for the standard rotation matrix.
        """
        rad = math.radians(-self.rotation)
        ox, oy = pad.offset
        rx = ox * math.cos(rad) - oy * math.sin(rad)
        ry = ox * math.sin(rad) + oy * math.cos(rad)
        return (self.position[0] + rx, self.position[1] + ry)


@dataclass
class Net:
    name: str
    class_: str      # "ground" | "power" | "constrained_signal" | "signal"
    strategy: str    # "pour" | "route_wide" | "route" | "differential_pair"
    width_mm: Optional[float]
    priority: int
    notes: str = ""


@dataclass
class Segment:
    start: tuple[float, float]
    end: tuple[float, float]
    layer: str


@dataclass
class Route:
    net: str
    width_mm: float
    segments: list[Segment]


@dataclass
class Via:
    position: tuple[float, float]
    net: str
    drill_mm: float


@dataclass
class Keepout:
    type: str        # "circle" | "rect" | "polygon"
    layers: list[str]
    notes: str = ""
    center: Optional[tuple[float, float]] = None
    radius: Optional[float] = None
    rect: Optional[tuple[float, float, float, float]] = None  # x, y, w, h
    polygon: Optional[list[tuple[float, float]]] = None
    placement_only: bool = False  # True = blocks component placement but allows traces/vias


@dataclass
class Pour:
    net: str
    layer: str
    outline: str    # "board" or future polygon spec
    priority: int = 0


@dataclass
class Board:
    board_outline: list[tuple[float, float]]
    grid_step: float
    rules: Rules
    components: dict[str, Component]
    nets: dict[str, Net]
    keepouts: list[Keepout]
    routes: list[Route]
    vias: list[Via]
    pours: list[Pour]


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _rules_to_dict(r: Rules) -> dict:
    return {
        "min_clearance_mm": r.min_clearance_mm,
        "default_trace_width_mm": r.default_trace_width_mm,
        "power_trace_width_mm": r.power_trace_width_mm,
        "via_drill_mm": r.via_drill_mm,
        "via_annular_ring_mm": r.via_annular_ring_mm,
        "layers": r.layers,
    }


def _rules_from_dict(d: dict) -> Rules:
    return Rules(
        min_clearance_mm=d.get("min_clearance_mm", 0.2),
        default_trace_width_mm=d.get("default_trace_width_mm", 0.3),
        power_trace_width_mm=d.get("power_trace_width_mm", 0.5),
        via_drill_mm=d.get("via_drill_mm", 0.3),
        via_annular_ring_mm=d.get("via_annular_ring_mm", 0.15),
        layers=d.get("layers", ["F.Cu", "B.Cu"]),
    )


def _pad_to_dict(p: Pad) -> dict:
    return {
        "number": p.number,
        "net": p.net,
        "offset": list(p.offset),
        "size": list(p.size),
        "shape": p.shape,
        "layer": p.layer,
    }


def _pad_from_dict(d: dict) -> Pad:
    return Pad(
        number=d["number"],
        net=d["net"],
        offset=tuple(d["offset"]),
        size=tuple(d["size"]),
        shape=d["shape"],
        layer=d["layer"],
    )


def _placement_to_dict(p: Placement) -> dict:
    return {
        "constraint": p.constraint,
        "edge": p.edge,
        "allowed_rotations": p.allowed_rotations,
        "notes": p.notes,
        "align_group": p.align_group,
        "align_axis": p.align_axis,
        "spacing_mm": p.spacing_mm,
        "offset_from_edge_mm": p.offset_from_edge_mm,
    }


def _placement_from_dict(d: dict) -> Placement:
    return Placement(
        constraint=d.get("constraint", "free"),
        edge=d.get("edge"),
        allowed_rotations=d.get("allowed_rotations", [0, 90, 180, 270]),
        notes=d.get("notes", ""),
        align_group=d.get("align_group"),
        align_axis=d.get("align_axis"),
        spacing_mm=d.get("spacing_mm"),
        offset_from_edge_mm=d.get("offset_from_edge_mm"),
    )


def _component_to_dict(c: Component) -> dict:
    return {
        "reference": c.reference,
        "footprint": c.footprint,
        "description": c.description,
        "position": list(c.position),
        "rotation": c.rotation,
        "layer": c.layer,
        "bbox": list(c.bbox),
        "pads": [_pad_to_dict(p) for p in c.pads],
        "placement": _placement_to_dict(c.placement),
    }


def _component_from_dict(d: dict) -> Component:
    return Component(
        reference=d["reference"],
        footprint=d["footprint"],
        description=d.get("description", ""),
        position=tuple(d["position"]),
        rotation=d.get("rotation", 0.0),
        layer=d.get("layer", "F.Cu"),
        bbox=tuple(d["bbox"]),
        pads=[_pad_from_dict(p) for p in d.get("pads", [])],
        placement=_placement_from_dict(d.get("placement", {})),
    )


def _net_to_dict(n: Net) -> dict:
    return {
        "name": n.name,
        "class": n.class_,
        "strategy": n.strategy,
        "width_mm": n.width_mm,
        "priority": n.priority,
        "notes": n.notes,
    }


def _net_from_dict(d: dict) -> Net:
    return Net(
        name=d["name"],
        class_=d["class"],
        strategy=d.get("strategy", "route"),
        width_mm=d.get("width_mm"),
        priority=d.get("priority", 10),
        notes=d.get("notes", ""),
    )


def _segment_to_dict(s: Segment) -> dict:
    return {"start": list(s.start), "end": list(s.end), "layer": s.layer}


def _segment_from_dict(d: dict) -> Segment:
    return Segment(start=tuple(d["start"]), end=tuple(d["end"]), layer=d["layer"])


def _route_to_dict(r: Route) -> dict:
    return {
        "net": r.net,
        "width_mm": r.width_mm,
        "segments": [_segment_to_dict(s) for s in r.segments],
    }


def _route_from_dict(d: dict) -> Route:
    return Route(
        net=d["net"],
        width_mm=d["width_mm"],
        segments=[_segment_from_dict(s) for s in d.get("segments", [])],
    )


def _via_to_dict(v: Via) -> dict:
    return {"position": list(v.position), "net": v.net, "drill_mm": v.drill_mm}


def _via_from_dict(d: dict) -> Via:
    return Via(position=tuple(d["position"]), net=d["net"], drill_mm=d["drill_mm"])


def _keepout_to_dict(k: Keepout) -> dict:
    d: dict = {"type": k.type, "layers": k.layers, "notes": k.notes}
    if k.center is not None:
        d["center"] = list(k.center)
    if k.radius is not None:
        d["radius"] = k.radius
    if k.rect is not None:
        d["rect"] = list(k.rect)
    if k.polygon is not None:
        d["polygon"] = [list(pt) for pt in k.polygon]
    if k.placement_only:
        d["placement_only"] = True
    return d


def _keepout_from_dict(d: dict) -> Keepout:
    return Keepout(
        type=d["type"],
        layers=d["layers"],
        notes=d.get("notes", ""),
        center=tuple(d["center"]) if "center" in d else None,
        radius=d.get("radius"),
        rect=tuple(d["rect"]) if "rect" in d else None,
        polygon=[tuple(pt) for pt in d["polygon"]] if "polygon" in d else None,
        placement_only=d.get("placement_only", False),
    )


def _pour_to_dict(p: Pour) -> dict:
    return {"net": p.net, "layer": p.layer, "outline": p.outline, "priority": p.priority}


def _pour_from_dict(d: dict) -> Pour:
    return Pour(net=d["net"], layer=d["layer"], outline=d["outline"], priority=d.get("priority", 0))


def board_to_dict(board: Board) -> dict:
    return {
        "board_outline": [list(pt) for pt in board.board_outline],
        "grid_step": board.grid_step,
        "rules": _rules_to_dict(board.rules),
        "components": {ref: _component_to_dict(c) for ref, c in board.components.items()},
        "nets": {name: _net_to_dict(n) for name, n in board.nets.items()},
        "keepouts": [_keepout_to_dict(k) for k in board.keepouts],
        "routes": [_route_to_dict(r) for r in board.routes],
        "vias": [_via_to_dict(v) for v in board.vias],
        "pours": [_pour_to_dict(p) for p in board.pours],
    }


def board_from_dict(d: dict) -> Board:
    return Board(
        board_outline=[tuple(pt) for pt in d["board_outline"]],
        grid_step=d.get("grid_step", 0.3),
        rules=_rules_from_dict(d.get("rules", {})),
        components={ref: _component_from_dict(c) for ref, c in d.get("components", {}).items()},
        nets={name: _net_from_dict(n) for name, n in d.get("nets", {}).items()},
        keepouts=[_keepout_from_dict(k) for k in d.get("keepouts", [])],
        routes=[_route_from_dict(r) for r in d.get("routes", [])],
        vias=[_via_from_dict(v) for v in d.get("vias", [])],
        pours=[_pour_from_dict(p) for p in d.get("pours", [])],
    )


def load_board(path: str | Path) -> Board:
    with open(path) as f:
        return board_from_dict(json.load(f))


def save_board(board: Board, path: str | Path) -> None:
    with open(path, "w") as f:
        json.dump(board_to_dict(board), f, indent=2)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_board(board: Board) -> list[str]:
    """Return a list of error strings. Empty list means valid."""
    errors: list[str] = []

    if len(board.board_outline) < 3:
        errors.append("board_outline must have at least 3 vertices")

    if board.grid_step <= 0:
        errors.append(f"grid_step must be positive, got {board.grid_step}")

    for ref, comp in board.components.items():
        if comp.reference != ref:
            errors.append(f"Component key {ref!r} doesn't match reference {comp.reference!r}")
        if comp.layer not in ("F.Cu", "B.Cu"):
            errors.append(f"{ref}: layer must be F.Cu or B.Cu, got {comp.layer!r}")
        for pad in comp.pads:
            if pad.net and pad.net not in board.nets:
                errors.append(f"{ref} pad {pad.number}: net {pad.net!r} not in nets dict")

    for name, net in board.nets.items():
        if net.name != name:
            errors.append(f"Net key {name!r} doesn't match name {net.name!r}")
        if net.class_ not in ("ground", "power", "constrained_signal", "signal"):
            errors.append(f"Net {name}: unknown class {net.class_!r}")
        if net.strategy not in ("pour", "route_wide", "route", "differential_pair"):
            errors.append(f"Net {name}: unknown strategy {net.strategy!r}")

    for route in board.routes:
        if route.net not in board.nets:
            errors.append(f"Route references unknown net {route.net!r}")

    for via in board.vias:
        if via.net not in board.nets:
            errors.append(f"Via references unknown net {via.net!r}")

    return errors


def is_board_unplaced(board: Board) -> bool:
    """Detect if components are piled together (unplaced from schematic import).

    Returns True if all components occupy less than 15% of the board area,
    suggesting they haven't been placed yet.
    """
    positions = [comp.position for comp in board.components.values()]
    if len(positions) < 2:
        return False
    xs = [p[0] for p in positions]
    ys = [p[1] for p in positions]
    spread = max(max(xs) - min(xs), max(ys) - min(ys))
    if not board.board_outline:
        return False
    outline_xs = [p[0] for p in board.board_outline]
    outline_ys = [p[1] for p in board.board_outline]
    board_size = max(
        max(outline_xs) - min(outline_xs),
        max(outline_ys) - min(outline_ys),
    )
    if board_size <= 0:
        return False
    return spread < board_size * 0.15
