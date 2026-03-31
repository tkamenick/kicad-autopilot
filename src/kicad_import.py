"""Merge routed board.json back into a .kicad_pcb file.

Strategy: text processing — no s-expression serializer needed.
1. Parse original .kicad_pcb to extract net name→number map and board origin.
2. Strip existing (segment ...) and (via ...) nodes.
3. Load board.json → routes and vias.
4. Convert board coordinates back to page coordinates.
5. Inject new s-expression nodes before the file's closing ')'.

CLI:
    python src/kicad_import.py board.json --base original.kicad_pcb -o routed.kicad_pcb
"""
from __future__ import annotations

import argparse
import re
import uuid
from pathlib import Path

from src.schema import Board, Route, Via, load_board
from src.sexpr_parser import find_all, find_one, get_at, get_str, get_xy, parse_file


# ---------------------------------------------------------------------------
# Strip existing routing from .kicad_pcb text
# ---------------------------------------------------------------------------

def _strip_routing(text: str) -> str:
    """Remove all (segment ...), (via ...), and (filled_polygon ...) nodes.

    Stripping filled_polygon forces KiCad to recalculate zone fills,
    which is necessary because our new traces/vias need clearance gaps
    in the ground pour.
    """
    result: list[str] = []
    i = 0
    n = len(text)

    while i < n:
        # Scan for a top-level (segment or (via token
        m = re.search(r'\(\s*(segment|via|filled_polygon)\s', text[i:])
        if m is None:
            result.append(text[i:])
            break

        # Append everything up to this match
        start = i + m.start()
        result.append(text[i:start])

        # Walk forward counting parens to find matching close
        depth = 0
        j = start
        while j < n:
            if text[j] == '(':
                depth += 1
            elif text[j] == ')':
                depth -= 1
                if depth == 0:
                    j += 1  # skip the closing paren
                    break
            j += 1
        i = j

    return "".join(result)


# ---------------------------------------------------------------------------
# Extract net name → number map from .kicad_pcb
# ---------------------------------------------------------------------------

def _build_net_num_map(kicad_path: str | Path) -> dict[str, int]:
    """Return {net_name: net_number} from .kicad_pcb net declarations."""
    tree = parse_file(str(kicad_path))
    net_map: dict[str, int] = {}
    for net_node in find_all(tree, "net"):
        if len(net_node) >= 3:
            try:
                num = int(net_node[1])
                name = str(net_node[2])
                if name:
                    net_map[name] = num
            except (ValueError, TypeError):
                pass
    return net_map


# ---------------------------------------------------------------------------
# Extract board origin from .kicad_pcb
# ---------------------------------------------------------------------------

def _extract_origin(kicad_path: str | Path) -> tuple[float, float]:
    """Return (origin_x, origin_y) — top-left of Edge.Cuts outline in page coords."""
    tree = parse_file(str(kicad_path))

    # Try gr_line Edge.Cuts
    points: list[tuple[float, float]] = []
    for line in find_all(tree, "gr_line"):
        if get_str(line, "layer") == "Edge.Cuts":
            s = get_xy(line, "start")
            e = get_xy(line, "end")
            if s:
                points.append(s)
            if e:
                points.append(e)

    # Try gr_poly Edge.Cuts
    if not points:
        for poly in find_all(tree, "gr_poly"):
            if get_str(poly, "layer") == "Edge.Cuts":
                pts_node = find_one(poly, "pts")
                if pts_node:
                    for pt in find_all(pts_node, "xy"):
                        if len(pt) >= 3:
                            points.append((float(pt[1]), float(pt[2])))

    # Try gr_rect Edge.Cuts (KiCad 7+)
    if not points:
        for rect in find_all(tree, "gr_rect"):
            if get_str(rect, "layer") == "Edge.Cuts":
                start = get_xy(rect, "start")
                end = get_xy(rect, "end")
                if start and end:
                    points.extend([start, end])

    if points:
        return (min(p[0] for p in points), min(p[1] for p in points))
    return (0.0, 0.0)


# ---------------------------------------------------------------------------
# Format s-expressions for new routing
# ---------------------------------------------------------------------------

def _fmt_coord(v: float) -> str:
    """Format a coordinate value — drop trailing zeros."""
    if v == int(v):
        return str(int(v))
    return f"{v:.4f}".rstrip("0")


def _format_segment(
    start: tuple[float, float],
    end: tuple[float, float],
    width: float,
    layer: str,
    net_ref: str,
) -> str:
    sx, sy = _fmt_coord(start[0]), _fmt_coord(start[1])
    ex, ey = _fmt_coord(end[0]), _fmt_coord(end[1])
    w = _fmt_coord(width)
    uid = str(uuid.uuid4())
    return (f'  (segment (start {sx} {sy}) (end {ex} {ey}) (width {w}) '
            f'(layer "{layer}") (net "{net_ref}") (uuid "{uid}"))')


def _format_via(
    pos: tuple[float, float],
    size: float,
    drill: float,
    net_ref: str,
) -> str:
    px, py = _fmt_coord(pos[0]), _fmt_coord(pos[1])
    s = _fmt_coord(size)
    d = _fmt_coord(drill)
    uid = str(uuid.uuid4())
    return (f'  (via (at {px} {py}) (size {s}) (drill {d}) '
            f'(layers "F.Cu" "B.Cu") (net "{net_ref}") (uuid "{uid}"))')


# ---------------------------------------------------------------------------
# Main import function
# ---------------------------------------------------------------------------

def _build_pad_position_map(
    kicad_path: str | Path,
) -> dict[str, list[tuple[float, float]]]:
    """Return {net_name: [(page_x, page_y), ...]} for every pad in the KiCad file.

    Uses actual KiCad pad positions (not grid-snapped) so traces can be
    connected precisely to pads during import.
    """
    import math
    tree = parse_file(str(kicad_path))
    result: dict[str, list[tuple[float, float]]] = {}
    for fp in find_all(tree, "footprint"):
        fx, fy, frot = get_at(fp)
        rad = math.radians(frot)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        for pad in find_all(fp, "pad"):
            net_node = find_one(pad, "net")
            if not net_node or len(net_node) < 2:
                continue
            net_name = str(net_node[1]) if not str(net_node[1]).isdigit() else ""
            if len(net_node) >= 3 and str(net_node[1]).isdigit():
                net_name = str(net_node[2])
            if not net_name:
                continue
            pad_at = get_xy(pad)
            if not pad_at:
                continue
            # Compute absolute page position with footprint rotation
            ax = fx + pad_at[0] * cos_r - pad_at[1] * sin_r
            ay = fy + pad_at[0] * sin_r + pad_at[1] * cos_r
            result.setdefault(net_name, []).append((ax, ay))
    return result


def _snap_to_pad(
    x: float, y: float, net_name: str,
    pad_map: dict[str, list[tuple[float, float]]],
    threshold: float = 0.5,
) -> tuple[float, float]:
    """If (x, y) is within threshold of a pad on the same net, return the
    exact pad position.  Otherwise return (x, y) unchanged."""
    import math
    for px, py in pad_map.get(net_name, []):
        if math.sqrt((x - px) ** 2 + (y - py) ** 2) <= threshold:
            return (px, py)
    return (x, y)


def import_routes(
    board: Board,
    kicad_base: str | Path,
    output_path: str | Path,
) -> None:
    """Merge routes and vias from board into kicad_base, write to output_path."""
    kicad_base = Path(kicad_base)
    output_path = Path(output_path)

    # Read original file
    original_text = kicad_base.read_text(encoding="utf-8")

    # Strip existing routing
    stripped = _strip_routing(original_text)

    # Detect KiCad format: if top-level (net N "name") declarations exist,
    # use net numbers (KiCad 6-); otherwise use net names directly (KiCad 7+).
    net_num_map = _build_net_num_map(kicad_base)
    use_net_numbers = len(net_num_map) > 0

    # Board origin in page coordinates
    origin_x, origin_y = _extract_origin(kicad_base)

    # Build actual pad positions from KiCad file for endpoint snapping
    pad_map = _build_pad_position_map(kicad_base)

    # Via size = drill + 2 * annular ring
    via_drill = board.rules.via_drill_mm
    via_size = via_drill + 2 * board.rules.via_annular_ring_mm

    # Format new routing nodes
    new_nodes: list[str] = []

    for route in board.routes:
        if use_net_numbers:
            net_ref = net_num_map.get(route.net)
            if net_ref is None:
                continue
            net_ref = str(net_ref)
        else:
            net_ref = route.net
        for seg in route.segments:
            page_start = (seg.start[0] + origin_x, seg.start[1] + origin_y)
            page_end = (seg.end[0] + origin_x, seg.end[1] + origin_y)
            # Snap endpoints to actual KiCad pad positions if close
            page_start = _snap_to_pad(page_start[0], page_start[1], route.net, pad_map)
            page_end = _snap_to_pad(page_end[0], page_end[1], route.net, pad_map)
            new_nodes.append(_format_segment(
                page_start, page_end, route.width_mm, seg.layer, net_ref
            ))

    for via in board.vias:
        if use_net_numbers:
            net_ref = net_num_map.get(via.net)
            if net_ref is None:
                continue
            net_ref = str(net_ref)
        else:
            net_ref = via.net
        page_pos = (via.position[0] + origin_x, via.position[1] + origin_y)
        # Snap GND vias to actual pad positions (via-in-pad)
        page_pos = _snap_to_pad(page_pos[0], page_pos[1], via.net, pad_map)
        new_nodes.append(_format_via(page_pos, via_size, via.drill_mm, net_ref))

    # Inject before the final closing paren
    injection = "\n".join(new_nodes)
    if injection:
        # Find the last ')' in the file
        last_paren = stripped.rfind(")")
        if last_paren != -1:
            result = stripped[:last_paren] + "\n" + injection + "\n" + stripped[last_paren:]
        else:
            result = stripped + "\n" + injection
    else:
        result = stripped

    output_path.write_text(result, encoding="utf-8")
    print(f"Wrote {len(board.routes)} routes, {len(board.vias)} vias → {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge routes from board.json back into .kicad_pcb"
    )
    parser.add_argument("board_json", help="Input board.json (with routes)")
    parser.add_argument("--base", required=True,
                        help="Original .kicad_pcb file (used for net numbers and origin)")
    parser.add_argument("-o", "--output", required=True,
                        help="Output .kicad_pcb file")
    args = parser.parse_args()

    board = load_board(args.board_json)
    import_routes(board, args.base, args.output)


if __name__ == "__main__":
    main()
