"""Tests for src/sexpr_parser.py."""
import pytest

from src.sexpr_parser import (
    find_all, find_one, get_at, get_float, get_str, get_strings, get_xy,
    parse, tokenize,
)


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_simple_expression(self):
        assert tokenize("(foo bar)") == ["(", "foo", "bar", ")"]

    def test_nested(self):
        assert tokenize("(a (b c))") == ["(", "a", "(", "b", "c", ")", ")"]

    def test_quoted_string(self):
        assert tokenize('(layer "F.Cu")') == ["(", "layer", "F.Cu", ")"]

    def test_quoted_string_with_spaces(self):
        assert tokenize('(net 1 "My Net")') == ["(", "net", "1", "My Net", ")"]

    def test_quoted_string_with_parens(self):
        tokens = tokenize('(net 1 "My Net (1)")')
        assert tokens == ["(", "net", "1", "My Net (1)", ")"]

    def test_escaped_quote_in_string(self):
        tokens = tokenize(r'(ref "R\"1\"")')
        assert tokens[2] == 'R"1"'

    def test_numbers(self):
        assert tokenize("(at 25.4 -1.27)") == ["(", "at", "25.4", "-1.27", ")"]

    def test_whitespace_variants(self):
        assert tokenize("( at\t25.4\n-1.27 )") == ["(", "at", "25.4", "-1.27", ")"]

    def test_empty_string(self):
        assert tokenize("") == []

    def test_multiple_top_level(self):
        tokens = tokenize("(a 1) (b 2)")
        assert tokens == ["(", "a", "1", ")", "(", "b", "2", ")"]

    def test_bare_word_no_quotes(self):
        tokens = tokenize("(pad 1 smd rect)")
        assert tokens == ["(", "pad", "1", "smd", "rect", ")"]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class TestParse:
    def test_simple_atom(self):
        result = parse("(foo bar)")
        assert result == ["foo", "bar"]

    def test_nested(self):
        result = parse("(a (b c) (d e))")
        assert result == ["a", ["b", "c"], ["d", "e"]]

    def test_quoted_string(self):
        result = parse('(layer "F.Cu")')
        assert result == ["layer", "F.Cu"]

    def test_number_stays_string(self):
        result = parse("(at 25.4 30.0)")
        assert result[1] == "25.4"  # strings, not floats
        assert result[2] == "30.0"

    def test_deeply_nested(self):
        result = parse("(a (b (c (d e))))")
        assert result[1][1][1] == ["d", "e"]

    def test_multiple_children(self):
        result = parse("(kicad_pcb (net 1 GND) (net 2 VCC))")
        assert len(result) == 3
        assert result[1] == ["net", "1", "GND"]
        assert result[2] == ["net", "2", "VCC"]

    def test_unmatched_close_raises(self):
        with pytest.raises(ValueError, match="Unmatched"):
            parse("(foo bar))")

    def test_unclosed_raises(self):
        with pytest.raises(ValueError, match="Unclosed"):
            parse("(foo (bar)")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="No top-level"):
            parse("")

    def test_only_returns_first_top_level(self):
        result = parse("(a 1) (b 2)")
        assert result == ["a", "1"]

    def test_kicad_pcb_snippet(self, minimal_sexpr):
        result = parse(minimal_sexpr)
        assert result[0] == "kicad_pcb"
        assert result[1] == ["version", "20221018"]
        assert result[2] == ["generator", "pcbnew"]

    def test_footprint_snippet(self, footprint_sexpr):
        result = parse(footprint_sexpr)
        assert result[0] == "footprint"
        assert result[1] == "MCU:SOIC-10"


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------

class TestFindAll:
    def test_finds_multiple(self):
        node = ["kicad_pcb", ["net", "1", "GND"], ["net", "2", "VCC"], ["version", "20221018"]]
        nets = find_all(node, "net")
        assert len(nets) == 2
        assert nets[0] == ["net", "1", "GND"]

    def test_returns_empty_when_none(self):
        node = ["kicad_pcb", ["version", "20221018"]]
        assert find_all(node, "net") == []

    def test_ignores_string_children(self):
        node = ["foo", "bar", ["baz", "qux"]]
        assert find_all(node, "bar") == []


class TestFindOne:
    def test_finds_first(self):
        node = ["kicad_pcb", ["net", "1", "GND"], ["net", "2", "VCC"]]
        result = find_one(node, "net")
        assert result == ["net", "1", "GND"]

    def test_returns_none_when_missing(self):
        node = ["kicad_pcb", ["version", "20221018"]]
        assert find_one(node, "net") is None


class TestGetXY:
    def test_basic(self):
        node = ["footprint", ["at", "130", "100"]]
        assert get_xy(node) == (130.0, 100.0)

    def test_negative(self):
        node = ["pad", ["at", "-3.5", "-4"]]
        assert get_xy(node) == (-3.5, -4.0)

    def test_custom_token(self):
        node = ["segment", ["start", "10", "20"], ["end", "30", "40"]]
        assert get_xy(node, "start") == (10.0, 20.0)
        assert get_xy(node, "end") == (30.0, 40.0)

    def test_missing_returns_none(self):
        node = ["footprint", ["layer", "F.Cu"]]
        assert get_xy(node) is None


class TestGetAt:
    def test_two_values(self):
        node = ["footprint", ["at", "130", "100"]]
        assert get_at(node) == (130.0, 100.0, 0.0)

    def test_three_values_with_rotation(self):
        node = ["footprint", ["at", "130", "100", "90"]]
        assert get_at(node) == (130.0, 100.0, 90.0)

    def test_missing_returns_zeros(self):
        node = ["footprint", ["layer", "F.Cu"]]
        assert get_at(node) == (0.0, 0.0, 0.0)


class TestGetFloat:
    def test_basic(self):
        node = ["segment", ["width", "0.3"]]
        assert get_float(node, "width") == 0.3

    def test_missing_returns_none(self):
        node = ["segment", ["layer", "F.Cu"]]
        assert get_float(node, "width") is None


class TestGetStr:
    def test_basic(self):
        node = ["pad", ["layer", "F.Cu"]]
        assert get_str(node, "layer") == "F.Cu"

    def test_missing_returns_none(self):
        node = ["pad", ["size", "0.6", "1.2"]]
        assert get_str(node, "layer") is None


class TestGetStrings:
    def test_single(self):
        node = ["pad", ["layers", "F.Cu"]]
        assert get_strings(node, "layers") == ["F.Cu"]

    def test_multiple(self):
        node = ["pad", ["layers", "F.Cu", "F.Paste", "F.Mask"]]
        assert get_strings(node, "layers") == ["F.Cu", "F.Paste", "F.Mask"]

    def test_missing_returns_empty(self):
        node = ["pad", ["size", "0.6", "1.2"]]
        assert get_strings(node, "layers") == []


# ---------------------------------------------------------------------------
# Real fixture parsing
# ---------------------------------------------------------------------------

class TestRealFixture:
    def test_parses_synthetic_board(self, synthetic_kicad_path):
        from src.sexpr_parser import parse_file
        tree = parse_file(synthetic_kicad_path)
        assert tree[0] == "kicad_pcb"

    def test_finds_nets(self, synthetic_kicad_path):
        from src.sexpr_parser import parse_file
        tree = parse_file(synthetic_kicad_path)
        nets = find_all(tree, "net")
        # net 0 ("") plus 8 named nets
        assert len(nets) == 9

    def test_finds_footprints(self, synthetic_kicad_path):
        from src.sexpr_parser import parse_file
        tree = parse_file(synthetic_kicad_path)
        fps = find_all(tree, "footprint")
        assert len(fps) == 5

    def test_finds_edge_cuts(self, synthetic_kicad_path):
        from src.sexpr_parser import parse_file
        tree = parse_file(synthetic_kicad_path)
        lines = find_all(tree, "gr_line")
        edge_lines = [l for l in lines if get_str(l, "layer") == "Edge.Cuts"]
        assert len(edge_lines) == 4
