"""Tests for src/kicad_import.py."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.kicad_import import (
    _build_net_num_map, _extract_origin, _fmt_coord, _format_segment,
    _format_via, _strip_routing, import_routes,
)
from src.schema import Board, Net, Route, Rules, Segment, Via, load_board, save_board


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# _strip_routing
# ---------------------------------------------------------------------------

class TestStripRouting:
    def test_removes_segment(self):
        text = '(kicad_pcb\n  (segment (start 1 2) (end 3 4) (width 0.3) (layer "F.Cu") (net 1))\n)'
        stripped = _strip_routing(text)
        assert "(segment" not in stripped
        assert "(kicad_pcb" in stripped

    def test_removes_via(self):
        text = '(kicad_pcb\n  (via (at 5 6) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1))\n)'
        stripped = _strip_routing(text)
        assert "(via" not in stripped

    def test_preserves_non_routing(self):
        text = '(kicad_pcb\n  (net 1 "GND")\n  (footprint "R" (at 10 10))\n)'
        stripped = _strip_routing(text)
        assert '(net 1 "GND")' in stripped
        assert "(footprint" in stripped

    def test_removes_multiple(self):
        text = (
            '(kicad_pcb\n'
            '  (segment (start 1 2) (end 3 4) (width 0.3) (layer "F.Cu") (net 1))\n'
            '  (segment (start 5 6) (end 7 8) (width 0.3) (layer "B.Cu") (net 2))\n'
            '  (via (at 3 4) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1))\n'
            '  (net 1 "GND")\n'
            ')'
        )
        stripped = _strip_routing(text)
        assert "(segment" not in stripped
        assert "(via" not in stripped
        assert '(net 1 "GND")' in stripped

    def test_handles_empty_file(self):
        assert _strip_routing("") == ""

    def test_handles_no_routing(self):
        text = '(kicad_pcb (net 1 "GND"))'
        stripped = _strip_routing(text)
        assert stripped == text


# ---------------------------------------------------------------------------
# _build_net_num_map
# ---------------------------------------------------------------------------

class TestBuildNetNumMap:
    def test_extracts_nets(self):
        net_map = _build_net_num_map(FIXTURES_DIR / "synthetic_board.kicad_pcb")
        assert "GND" in net_map
        assert net_map["GND"] == 1
        assert "3V3" in net_map

    def test_empty_net_excluded(self):
        net_map = _build_net_num_map(FIXTURES_DIR / "synthetic_board.kicad_pcb")
        assert "" not in net_map


# ---------------------------------------------------------------------------
# _extract_origin
# ---------------------------------------------------------------------------

class TestExtractOrigin:
    def test_gr_line_origin(self):
        origin = _extract_origin(FIXTURES_DIR / "synthetic_board.kicad_pcb")
        # synthetic_board has outline at page (100,80)-(160,120) → origin (100,80)
        assert origin == pytest.approx((100.0, 80.0))

    def test_gr_poly_origin(self):
        origin = _extract_origin(FIXTURES_DIR / "poly_outline_board.kicad_pcb")
        # poly_outline has corners at (200,200),(230,200),(230,215),(200,215) → origin (200,200)
        assert origin == pytest.approx((200.0, 200.0))


# ---------------------------------------------------------------------------
# _fmt_coord
# ---------------------------------------------------------------------------

class TestFmtCoord:
    def test_integer(self):
        assert _fmt_coord(10.0) == "10"
        assert _fmt_coord(0.0) == "0"

    def test_decimal(self):
        assert _fmt_coord(0.3) == "0.3"
        assert _fmt_coord(1.5) == "1.5"

    def test_no_trailing_zeros(self):
        result = _fmt_coord(10.5000)
        assert result == "10.5"


# ---------------------------------------------------------------------------
# _format_segment / _format_via
# ---------------------------------------------------------------------------

class TestFormatNodes:
    def test_format_segment_structure(self):
        s = _format_segment((100.0, 80.0), (106.0, 80.0), 0.3, "F.Cu", "GND")
        assert "(segment" in s
        assert "start 100 80" in s
        assert "end 106 80" in s
        assert 'layer "F.Cu"' in s
        assert '(net "GND")' in s

    def test_format_via_structure(self):
        v = _format_via((136.0, 104.0), 0.6, 0.3, "GND")
        assert "(via" in v
        assert "at 136 104" in v
        assert '(net "GND")' in v
        assert 'layers "F.Cu" "B.Cu"' in v


# ---------------------------------------------------------------------------
# import_routes integration
# ---------------------------------------------------------------------------

class TestImportRoutes:
    def test_round_trip_segments_present(self, tmp_path, routed_kicad_path):
        """Export routed_board.kicad_pcb → import_routes → verify segment nodes in output."""
        from src.kicad_export import export_board
        board = export_board(routed_kicad_path)
        # board already has routes from the fixture — import them back
        out = tmp_path / "out.kicad_pcb"
        import_routes(board, routed_kicad_path, out)
        text = out.read_text()
        assert "(segment" in text
        assert "(via" in text

    def test_output_is_parseable(self, tmp_path, routed_kicad_path):
        """The output file must be a parseable s-expression."""
        from src.kicad_export import export_board
        from src.sexpr_parser import parse_file
        board = export_board(routed_kicad_path)
        out = tmp_path / "out.kicad_pcb"
        import_routes(board, routed_kicad_path, out)
        # Should not raise
        tree = parse_file(str(out))
        assert tree[0] == "kicad_pcb"

    def test_coordinates_converted(self, tmp_path, synthetic_kicad_path):
        """Segment coordinates should be in page space (offset by origin)."""
        from src.kicad_export import export_board
        board = export_board(synthetic_kicad_path)
        # Add a known route at board coordinate (30, 21) → page (130, 101)
        seg = Segment(start=(30.0, 21.0), end=(36.0, 21.0), layer="F.Cu")
        from src.schema import Route
        board_with_route = Board(
            board_outline=board.board_outline,
            grid_step=board.grid_step,
            rules=board.rules,
            components=board.components,
            nets=board.nets,
            keepouts=board.keepouts,
            routes=[Route(net="GND", width_mm=0.3, segments=[seg])],
            vias=board.vias,
            pours=board.pours,
        )
        out = tmp_path / "out.kicad_pcb"
        import_routes(board_with_route, synthetic_kicad_path, out)
        text = out.read_text()
        # Should contain page coordinates (130, 101) → (136, 101)
        assert "130" in text
        assert "101" in text

    def test_unknown_net_segments_skipped(self, tmp_path, synthetic_kicad_path):
        """Segments whose net isn't in the original file should be silently skipped."""
        from src.kicad_export import export_board
        board = export_board(synthetic_kicad_path)
        seg = Segment(start=(5.0, 5.0), end=(10.0, 5.0), layer="F.Cu")
        board_with_bad_route = Board(
            board_outline=board.board_outline,
            grid_step=board.grid_step,
            rules=board.rules,
            components=board.components,
            nets=board.nets,
            keepouts=board.keepouts,
            routes=[Route(net="NONEXISTENT_NET", width_mm=0.3, segments=[seg])],
            vias=board.vias,
            pours=board.pours,
        )
        out = tmp_path / "out.kicad_pcb"
        # Should not raise
        import_routes(board_with_bad_route, synthetic_kicad_path, out)

    def test_stripping_before_inject(self, tmp_path, routed_kicad_path):
        """Old routing nodes should be stripped before new ones are injected."""
        from src.kicad_export import export_board
        board = export_board(routed_kicad_path)
        out = tmp_path / "out.kicad_pcb"
        import_routes(board, routed_kicad_path, out)
        text = out.read_text()
        # Should not have duplicate segment nodes beyond what board.routes has
        n_segments = text.count("(segment")
        n_expected = sum(len(r.segments) for r in board.routes)
        assert n_segments == n_expected
