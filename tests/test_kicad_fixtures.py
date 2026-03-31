"""Integration tests for the additional .kicad_pcb fixtures.

Covers three code paths that synthetic_board.kicad_pcb does not exercise:

  rotated_board     — component rotations (90 / 180 / 270°): verifies that
                      pad_abs_position applies the rotation matrix correctly
                      end-to-end through kicad_export.

  routed_board      — existing trace segments and a via: verifies
                      _extract_routes and _extract_vias.

  poly_outline_board — board outline expressed as a gr_poly node instead of
                       individual gr_line segments: verifies the fallback path
                       in _extract_outline.
"""
from __future__ import annotations

import math

import pytest

from src.kicad_export import export_board


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pad(board, ref, number):
    comp = board.components[ref]
    return next(p for p in comp.pads if p.number == number)


def _abs_pos(board, ref, number):
    comp = board.components[ref]
    p = _pad(board, ref, number)
    return comp.pad_abs_position(p)


# ---------------------------------------------------------------------------
# rotated_board — rotation math through the full export pipeline
# ---------------------------------------------------------------------------

class TestRotatedBoard:
    @pytest.fixture(autouse=True)
    def board(self, rotated_kicad_path):
        self._board = export_board(rotated_kicad_path)

    def test_four_components(self):
        assert len(self._board.components) == 4
        for ref in ("R1", "R2", "R3", "R4"):
            assert ref in self._board.components

    def test_two_nets(self):
        assert set(self._board.nets.keys()) == {"GND", "VCC"}

    def test_vcc_classified_as_power(self):
        assert self._board.nets["VCC"].class_ == "power"

    def test_component_positions(self):
        b = self._board
        # Origin = (50, 50); all positions are multiples of 0.3 mm
        assert b.components["R1"].position == pytest.approx((9.0,  9.0))
        assert b.components["R2"].position == pytest.approx((18.0, 9.0))
        assert b.components["R3"].position == pytest.approx((27.0, 9.0))
        assert b.components["R4"].position == pytest.approx((9.0, 18.0))

    def test_rotations_extracted(self):
        b = self._board
        assert b.components["R1"].rotation == pytest.approx(0.0)
        assert b.components["R2"].rotation == pytest.approx(90.0)
        assert b.components["R3"].rotation == pytest.approx(180.0)
        assert b.components["R4"].rotation == pytest.approx(270.0)

    def test_r1_pads_horizontal(self):
        # R1 at 0°: pads at local (−1.5, 0) and (1.5, 0) → unchanged
        p1 = _abs_pos(self._board, "R1", "1")
        p2 = _abs_pos(self._board, "R1", "2")
        assert p1 == pytest.approx((7.5, 9.0))
        assert p2 == pytest.approx((10.5, 9.0))

    def test_r2_pads_rotated_90(self):
        # R2 at 90°: local (−1.5, 0) → (0, −1.5), local (1.5, 0) → (0, 1.5)
        # Abs: R2 at (18, 9) → pad1 (18, 7.5), pad2 (18, 10.5)
        p1 = _abs_pos(self._board, "R2", "1")
        p2 = _abs_pos(self._board, "R2", "2")
        assert p1 == pytest.approx((18.0, 7.5))
        assert p2 == pytest.approx((18.0, 10.5))

    def test_r3_pads_rotated_180(self):
        # R3 at 180°: local (−1.5, 0) → (1.5, 0), local (1.5, 0) → (−1.5, 0)
        # Abs: R3 at (27, 9) → pad1 (28.5, 9), pad2 (25.5, 9)
        p1 = _abs_pos(self._board, "R3", "1")
        p2 = _abs_pos(self._board, "R3", "2")
        assert p1 == pytest.approx((28.5, 9.0))
        assert p2 == pytest.approx((25.5, 9.0))

    def test_r4_pads_rotated_270(self):
        # R4 at 270°: local (−1.5, 0) → (0, 1.5), local (1.5, 0) → (0, −1.5)
        # Abs: R4 at (9, 18) → pad1 (9, 19.5), pad2 (9, 16.5)
        p1 = _abs_pos(self._board, "R4", "1")
        p2 = _abs_pos(self._board, "R4", "2")
        assert p1 == pytest.approx((9.0, 19.5))
        assert p2 == pytest.approx((9.0, 16.5))

    def test_all_pads_have_correct_net(self):
        for ref in ("R1", "R2", "R3", "R4"):
            comp = self._board.components[ref]
            pad1 = next(p for p in comp.pads if p.number == "1")
            pad2 = next(p for p in comp.pads if p.number == "2")
            assert pad1.net == "GND"
            assert pad2.net == "VCC"

    def test_board_dimensions(self):
        xs = [pt[0] for pt in self._board.board_outline]
        ys = [pt[1] for pt in self._board.board_outline]
        assert min(xs) == pytest.approx(0.0)
        assert min(ys) == pytest.approx(0.0)
        assert max(xs) == pytest.approx(40.0, abs=0.15)  # 40mm not a 0.3mm multiple
        assert max(ys) == pytest.approx(30.0, abs=0.15)  # 30mm not a 0.3mm multiple

    def test_scorable(self):
        from src.placement_scorer import score_placement
        score = score_placement(self._board)
        assert 0 <= score.composite_score <= 100


# ---------------------------------------------------------------------------
# routed_board — existing traces and vias
# ---------------------------------------------------------------------------

class TestRoutedBoard:
    @pytest.fixture(autouse=True)
    def board(self, routed_kicad_path):
        self._board = export_board(routed_kicad_path)

    def test_same_components_as_synthetic(self):
        for ref in ("U1", "J1", "J2", "C1", "C2"):
            assert ref in self._board.components

    def test_routes_extracted(self):
        assert len(self._board.routes) > 0

    def test_gnd_route_present(self):
        nets = {r.net for r in self._board.routes}
        assert "GND" in nets

    def test_3v3_route_present(self):
        nets = {r.net for r in self._board.routes}
        assert "3V3" in nets

    def test_gnd_route_segment_count(self):
        gnd = next(r for r in self._board.routes if r.net == "GND")
        # Two GND segments in the fixture
        assert len(gnd.segments) == 2

    def test_gnd_segment_layers(self):
        gnd = next(r for r in self._board.routes if r.net == "GND")
        layers = {s.layer for s in gnd.segments}
        assert "F.Cu" in layers

    def test_gnd_segment_endpoints(self):
        gnd = next(r for r in self._board.routes if r.net == "GND")
        starts = {s.start for s in gnd.segments}
        ends   = {s.end   for s in gnd.segments}
        all_pts = starts | ends
        # Segment 1: (130,101)→(136,101) = board (30,21)→(36,21)
        # Segment 2: (136,101)→(136,104) = board (36,21)→(36,24)
        assert (30.0, 21.0) in all_pts
        assert (36.0, 21.0) in all_pts
        assert (36.0, 24.0) in all_pts

    def test_3v3_route_on_b_cu(self):
        v33 = next(r for r in self._board.routes if r.net == "3V3")
        assert all(s.layer == "B.Cu" for s in v33.segments)

    def test_via_extracted(self):
        assert len(self._board.vias) == 1

    def test_via_net(self):
        assert self._board.vias[0].net == "GND"

    def test_via_position(self):
        # Via at page (136, 104) → board (36, 24)
        assert self._board.vias[0].position == pytest.approx((36.0, 24.0))

    def test_via_drill(self):
        assert self._board.vias[0].drill_mm == pytest.approx(0.4)

    def test_scorable(self):
        from src.placement_scorer import score_placement
        score = score_placement(self._board)
        assert 0 <= score.composite_score <= 100


# ---------------------------------------------------------------------------
# poly_outline_board — gr_poly outline fallback
# ---------------------------------------------------------------------------

class TestPolyOutlineBoard:
    @pytest.fixture(autouse=True)
    def board(self, poly_outline_kicad_path):
        self._board = export_board(poly_outline_kicad_path)

    def test_outline_extracted(self):
        # gr_poly path — should produce 4 vertices for the rectangle
        assert len(self._board.board_outline) == 4

    def test_outline_starts_at_origin(self):
        xs = [pt[0] for pt in self._board.board_outline]
        ys = [pt[1] for pt in self._board.board_outline]
        assert min(xs) == pytest.approx(0.0, abs=0.01)
        assert min(ys) == pytest.approx(0.0, abs=0.01)

    def test_board_dimensions(self):
        # Board: page (200,200)→(230,215) = 30×15 mm
        xs = [pt[0] for pt in self._board.board_outline]
        ys = [pt[1] for pt in self._board.board_outline]
        assert max(xs) == pytest.approx(30.0, abs=0.01)
        assert max(ys) == pytest.approx(15.0, abs=0.15)

    def test_two_components(self):
        assert len(self._board.components) == 2
        assert "R1" in self._board.components
        assert "R2" in self._board.components

    def test_component_positions(self):
        b = self._board
        # R1 at page (209, 209), origin (200, 200) → board (9, 9) — on 0.3mm grid
        assert b.components["R1"].position == pytest.approx((9.0, 9.0))
        # R2 at page (218, 209) → board (18, 9) — on 0.3mm grid
        assert b.components["R2"].position == pytest.approx((18.0, 9.0))

    def test_nets_classified(self):
        assert self._board.nets["GND"].class_ == "ground"
        assert self._board.nets["3V3"].class_ == "power"

    def test_no_routes_or_vias(self):
        assert self._board.routes == []
        assert self._board.vias == []

    def test_scorable(self):
        from src.placement_scorer import score_placement
        score = score_placement(self._board)
        assert 0 <= score.composite_score <= 100
