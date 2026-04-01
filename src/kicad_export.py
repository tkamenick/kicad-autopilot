"""KiCad .kicad_pcb → board.json exporter.

Parses a KiCad PCB file using the s-expression parser and produces a Board object.
Does not require KiCad Python bindings.

CLI:
    python src/kicad_export.py input.kicad_pcb -o board.json --grid 0.3
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Optional

from src.schema import (
    Board, Component, Keepout, Net, Pad, Placement, Pour, Route, Rules,
    Segment, Via, board_to_dict, snap_to_grid,
)
from src.sexpr_parser import (
    SExpr, find_all, find_one, get_at, get_float, get_str, get_strings,
    get_xy, parse_file,
)


# ---------------------------------------------------------------------------
# Net classification heuristic
# ---------------------------------------------------------------------------

def _classify_net(name: str) -> tuple[str, str, Optional[float], int]:
    """Return (class_, strategy, width_mm, priority) for a net name."""
    up = name.upper()
    if up in ("GND", "AGND", "DGND", "PGND", "EARTH"):
        return ("ground", "pour", None, 0)
    if any(x in up for x in ("VCC", "VDD", "3V3", "5V0", "5V", "12V", "3.3V",
                               "VBAT", "VMAIN", "VBUS", "PWR", "VIN", "VOUT")):
        return ("power", "route_wide", 0.5, 1)
    if any(x in up for x in ("CLK", "SCK", "XTAL", "OSC")):
        return ("constrained_signal", "route", 0.3, 2)
    if any(x in up for x in ("USB_D", "USB_P", "USB_N", "HDMI", "LVDS", "DP_", "DM_")):
        return ("constrained_signal", "route", 0.3, 2)
    return ("signal", "route", 0.3, 10)


# ---------------------------------------------------------------------------
# Board outline extraction
# ---------------------------------------------------------------------------

def _extract_outline(tree: SExpr) -> list[tuple[float, float]]:
    """Extract the board outline polygon from Edge.Cuts graphics."""
    # Try gr_line segments first
    lines = find_all(tree, "gr_line")
    edge_lines = [l for l in lines if get_str(l, "layer") == "Edge.Cuts"]

    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for line in edge_lines:
        start = get_xy(line, "start")
        end = get_xy(line, "end")
        if start and end:
            segments.append((start, end))

    if segments:
        return _connect_segments(segments)

    # Try gr_poly (polygon outline)
    for poly in find_all(tree, "gr_poly"):
        if get_str(poly, "layer") == "Edge.Cuts":
            pts_node = find_one(poly, "pts")
            if pts_node:
                pts = [
                    (float(pt[1]), float(pt[2]))
                    for pt in find_all(pts_node, "xy")
                    if len(pt) >= 3
                ]
                if pts:
                    return pts

    # Try gr_rect (rectangle outline — KiCad 7+ style)
    for rect in find_all(tree, "gr_rect"):
        if get_str(rect, "layer") == "Edge.Cuts":
            start = get_xy(rect, "start")
            end = get_xy(rect, "end")
            if start and end:
                x0, y0 = min(start[0], end[0]), min(start[1], end[1])
                x1, y1 = max(start[0], end[0]), max(start[1], end[1])
                return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]

    return []


def _round_pt(p: tuple[float, float], decimals: int = 4) -> tuple[float, float]:
    return (round(p[0], decimals), round(p[1], decimals))


def _connect_segments(
    segments: list[tuple[tuple[float, float], tuple[float, float]]]
) -> list[tuple[float, float]]:
    """Connect disconnected line segments into an ordered polygon vertex list."""
    remaining = list(segments)
    if not remaining:
        return []

    path = [_round_pt(remaining[0][0]), _round_pt(remaining[0][1])]
    remaining.pop(0)

    while remaining:
        last = path[-1]
        found = False
        for i, (s, e) in enumerate(remaining):
            rs, re = _round_pt(s), _round_pt(e)
            if rs == last:
                path.append(re)
                remaining.pop(i)
                found = True
                break
            elif re == last:
                path.append(rs)
                remaining.pop(i)
                found = True
                break
        if not found:
            break  # disconnected — just return what we have

    # Remove closing duplicate if polygon is closed
    if len(path) > 1 and _round_pt(path[-1]) == _round_pt(path[0]):
        path.pop()

    return path


# ---------------------------------------------------------------------------
# Footprint / pad extraction
# ---------------------------------------------------------------------------

def _resolve_net(net_node: Optional[SExpr], net_map: dict[int, str]) -> str:
    """Resolve a KiCad net node to a net name.

    Handles both formats:
    - KiCad 6-: (net 5 "GND")  → integer lookup in net_map
    - KiCad 7+: (net "GND")    → direct string
    """
    if not net_node or len(net_node) < 2:
        return ""
    try:
        net_num = int(net_node[1])
        return net_map.get(net_num, "")
    except (ValueError, TypeError):
        return str(net_node[1])


def _extract_pad(pad_node: SExpr, net_map: dict[int, str]) -> Pad:
    """Extract a Pad from a KiCad pad s-expression node."""
    number = pad_node[1] if len(pad_node) > 1 else "?"
    shape = pad_node[3] if len(pad_node) > 3 else "rect"

    at = get_xy(pad_node) or (0.0, 0.0)

    size_node = find_one(pad_node, "size")
    if size_node and len(size_node) >= 3:
        size = (float(size_node[1]), float(size_node[2]))
    else:
        size = (1.0, 1.0)

    # Determine layer
    layer_vals = get_strings(pad_node, "layers")
    if "*.Cu" in layer_vals:
        layer = "*.Cu"
    elif layer_vals:
        layer = layer_vals[0]
    else:
        layer = "F.Cu"

    # Resolve net
    net_node = find_one(pad_node, "net")
    net_name = _resolve_net(net_node, net_map)

    # Normalize shape
    shape_map = {"rect": "rect", "circle": "circle", "oval": "oval",
                 "roundrect": "roundrect", "trapezoid": "rect"}
    shape = shape_map.get(shape, "rect")

    return Pad(
        number=str(number),
        net=net_name,
        offset=(float(at[0]), float(at[1])),
        size=(float(size[0]), float(size[1])),
        shape=shape,
        layer=layer,
    )


def _compute_bbox(pads: list[Pad], fp_node: SExpr = None) -> tuple[float, float, float, float]:
    """Compute bounding box from courtyard outline (preferred) or pad offsets.

    The courtyard represents the actual physical footprint extent including
    the component body, which can be much larger than the pad area (e.g.,
    RJ45 connectors, through-hole headers).
    """
    # Try to extract courtyard lines from the footprint node
    if fp_node is not None:
        courtyard_pts: list[tuple[float, float]] = []
        for item in fp_node:
            if not isinstance(item, list):
                continue
            if item[0] not in ("fp_line", "fp_rect"):
                continue
            layer = get_str(item, "layer") or ""
            if "Courtyard" not in layer and "CrtYd" not in layer:
                continue
            start = get_xy(item, "start")
            end = get_xy(item, "end")
            if start:
                courtyard_pts.append(start)
            if end:
                courtyard_pts.append(end)
        if courtyard_pts:
            xs = [p[0] for p in courtyard_pts]
            ys = [p[1] for p in courtyard_pts]
            return (min(xs), min(ys), max(xs), max(ys))

    # Fallback: compute from pads with margin
    if not pads:
        return (-2.0, -2.0, 2.0, 2.0)
    xs = [p.offset[0] - p.size[0] / 2 for p in pads] + [p.offset[0] + p.size[0] / 2 for p in pads]
    ys = [p.offset[1] - p.size[1] / 2 for p in pads] + [p.offset[1] + p.size[1] / 2 for p in pads]
    margin = 0.5
    return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)


def _extract_footprint(fp_node: SExpr, net_map: dict[int, str],
                       origin: tuple[float, float], grid: float) -> Component:
    """Extract a Component from a KiCad footprint node."""
    footprint_name = fp_node[1] if len(fp_node) > 1 and isinstance(fp_node[1], str) else "Unknown"

    x, y, rotation = get_at(fp_node)
    bx = snap_to_grid(x - origin[0], grid)
    by = snap_to_grid(y - origin[1], grid)

    layer = get_str(fp_node, "layer") or "F.Cu"

    # Find reference: KiCad 7+ uses property nodes, older versions use fp_text
    reference = "?"
    for prop in find_all(fp_node, "property"):
        if len(prop) >= 3 and prop[1] == "Reference":
            val = str(prop[2])
            if val and val not in ("REF**", "${REFERENCE}"):
                reference = val
            break
    if reference == "?":
        for ft in find_all(fp_node, "fp_text"):
            if len(ft) > 1 and ft[1] == "reference" and len(ft) > 2:
                val = str(ft[2])
                if val and val not in ("REF**", "${REFERENCE}"):
                    reference = val
                break

    pads = [_extract_pad(p, net_map) for p in find_all(fp_node, "pad")]
    bbox = _compute_bbox(pads, fp_node)

    return Component(
        reference=reference,
        footprint=footprint_name,
        description="",
        position=(bx, by),
        rotation=rotation,
        layer=layer,
        bbox=bbox,
        pads=pads,
        placement=Placement(),
    )


# ---------------------------------------------------------------------------
# Track / via extraction
# ---------------------------------------------------------------------------

def _extract_routes(tree: SExpr, net_map: dict[int, str],
                    origin: tuple[float, float], grid: float) -> list[Route]:
    """Extract existing trace segments, grouped by net."""
    net_segments: dict[str, list[Segment]] = {}
    net_widths: dict[str, float] = {}

    for seg in find_all(tree, "segment"):
        start = get_xy(seg, "start")
        end = get_xy(seg, "end")
        layer = get_str(seg, "layer") or "F.Cu"
        width = get_float(seg, "width") or 0.3

        net_node = find_one(seg, "net")
        net_name = _resolve_net(net_node, net_map)

        if not net_name or not start or not end:
            continue

        sx = snap_to_grid(start[0] - origin[0], grid)
        sy = snap_to_grid(start[1] - origin[1], grid)
        ex = snap_to_grid(end[0] - origin[0], grid)
        ey = snap_to_grid(end[1] - origin[1], grid)

        s = Segment(start=(sx, sy), end=(ex, ey), layer=layer)
        net_segments.setdefault(net_name, []).append(s)
        net_widths[net_name] = width

    return [
        Route(net=name, width_mm=net_widths.get(name, 0.3), segments=segs)
        for name, segs in net_segments.items()
    ]


def _extract_vias(tree: SExpr, net_map: dict[int, str],
                  origin: tuple[float, float], grid: float) -> list[Via]:
    vias = []
    for via_node in find_all(tree, "via"):
        pos = get_xy(via_node, "at")
        if not pos:
            continue
        drill = get_float(via_node, "drill") or 0.3

        net_node = find_one(via_node, "net")
        net_name = _resolve_net(net_node, net_map)

        if not net_name:
            continue

        vx = snap_to_grid(pos[0] - origin[0], grid)
        vy = snap_to_grid(pos[1] - origin[1], grid)
        vias.append(Via(position=(vx, vy), net=net_name, drill_mm=drill))
    return vias


# ---------------------------------------------------------------------------
# Main export function
# ---------------------------------------------------------------------------

def export_board(kicad_path: str | Path, grid: float = 0.3) -> Board:
    """Parse a .kicad_pcb file and return a Board object."""
    tree = parse_file(str(kicad_path))

    # Build net number → name mapping
    net_map: dict[int, str] = {}
    for net_node in find_all(tree, "net"):
        if len(net_node) >= 3:
            try:
                num = int(net_node[1])
                name = str(net_node[2])
                net_map[num] = name
            except (ValueError, TypeError):
                pass

    # Extract board outline (page coordinates)
    outline_page = _extract_outline(tree)
    if not outline_page:
        outline_page = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]

    # Board origin = top-left of outline bounding box in page coordinates
    origin_x = min(pt[0] for pt in outline_page)
    origin_y = min(pt[1] for pt in outline_page)
    origin = (origin_x, origin_y)

    # Convert outline to board coordinates
    board_outline = [
        (snap_to_grid(pt[0] - origin[0], grid), snap_to_grid(pt[1] - origin[1], grid))
        for pt in outline_page
    ]

    # Extract footprints
    components: dict[str, Component] = {}
    for fp_node in find_all(tree, "footprint"):
        comp = _extract_footprint(fp_node, net_map, origin, grid)
        # Deduplicate references (shouldn't happen in valid files)
        ref = comp.reference
        if ref in components:
            i = 2
            while f"{ref}_{i}" in components:
                i += 1
            ref = f"{ref}_{i}"
            comp = Component(
                reference=ref, footprint=comp.footprint, description=comp.description,
                position=comp.position, rotation=comp.rotation, layer=comp.layer,
                bbox=comp.bbox, pads=comp.pads, placement=comp.placement,
            )
        components[ref] = comp

    # Build nets from net_map + classification
    # For KiCad 7+: no top-level net nodes — collect names from component pads
    all_net_names: set[str] = set(net_map.values())
    for comp in components.values():
        for pad in comp.pads:
            if pad.net:
                all_net_names.add(pad.net)

    nets: dict[str, Net] = {}
    for name in all_net_names:
        if not name:
            continue
        class_, strategy, width_mm, priority = _classify_net(name)
        nets[name] = Net(
            name=name,
            class_=class_,
            strategy=strategy,
            width_mm=width_mm,
            priority=priority,
        )

    # Extract existing routes and vias
    routes = _extract_routes(tree, net_map, origin, grid)
    vias = _extract_vias(tree, net_map, origin, grid)

    # Default ground pour on B.Cu
    pours: list[Pour] = []
    if "GND" in nets:
        pours.append(Pour(net="GND", layer="B.Cu", outline="board", priority=0))

    # Extract design rules (use defaults if not found in file)
    rules = Rules()

    return Board(
        board_outline=board_outline,
        grid_step=grid,
        rules=rules,
        components=components,
        nets=nets,
        keepouts=[],
        routes=routes,
        vias=vias,
        pours=pours,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Export a .kicad_pcb file to board.json")
    parser.add_argument("input", help="Input .kicad_pcb file")
    parser.add_argument("-o", "--output", default="board.json", help="Output board.json path")
    parser.add_argument("--grid", type=float, default=0.3, help="Grid step in mm (default 0.3)")
    args = parser.parse_args()

    board = export_board(args.input, grid=args.grid)
    out_path = Path(args.output)
    with open(out_path, "w") as f:
        json.dump(board_to_dict(board), f, indent=2)
    print(f"Exported {len(board.components)} components, {len(board.nets)} nets → {out_path}")


if __name__ == "__main__":
    main()
