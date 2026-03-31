"""Run KiCad DRC via kicad-cli and parse results.

Locates kicad-cli in standard installation paths, runs DRC with zone
refill, and returns structured results for use in the routing loop.

CLI:
    python -m src.kicad_drc routed.kicad_pcb
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# kicad-cli discovery
# ---------------------------------------------------------------------------

_KICAD_CLI_PATHS = [
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",  # macOS
    "/usr/bin/kicad-cli",                                        # Linux
    "/usr/local/bin/kicad-cli",                                  # Linux alt
]


def find_kicad_cli() -> Optional[str]:
    """Return path to kicad-cli or None if not found."""
    # Check PATH first
    found = shutil.which("kicad-cli")
    if found:
        return found
    for path in _KICAD_CLI_PATHS:
        if Path(path).exists():
            return path
    return None


# ---------------------------------------------------------------------------
# DRC result
# ---------------------------------------------------------------------------

@dataclass
class KiCadDRCResult:
    violations: int
    unconnected: int
    violation_types: dict[str, int]  # type → count
    problem_nets: set[str]           # nets involved in violations/unconnected
    raw: dict                        # full JSON for detailed inspection


# ---------------------------------------------------------------------------
# Run DRC
# ---------------------------------------------------------------------------

def run_kicad_drc(kicad_pcb_path: str | Path) -> KiCadDRCResult:
    """Run KiCad DRC on a .kicad_pcb file and return parsed results."""
    cli = find_kicad_cli()
    if cli is None:
        raise RuntimeError("kicad-cli not found — install KiCad or add to PATH")

    kicad_pcb_path = Path(kicad_pcb_path)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        report_path = f.name

    try:
        subprocess.run(
            [cli, "pcb", "drc",
             "--format", "json",
             "--severity-all",
             "--refill-zones",
             "--units", "mm",
             "-o", report_path,
             str(kicad_pcb_path)],
            capture_output=True, text=True, timeout=60,
        )
        raw = json.loads(Path(report_path).read_text())
    finally:
        Path(report_path).unlink(missing_ok=True)

    # Parse results
    from collections import Counter
    violation_types = Counter(v["type"] for v in raw.get("violations", []))
    violations_total = sum(violation_types.values())
    unconnected_total = len(raw.get("unconnected_items", []))

    # Extract net names involved in problems
    problem_nets: set[str] = set()
    for v in raw.get("violations", []):
        for item in v.get("items", []):
            desc = item.get("description", "")
            # Extract net name from descriptions like "Track [+3.3V] on F.Cu"
            if "[" in desc and "]" in desc:
                net = desc[desc.index("[") + 1:desc.index("]")]
                problem_nets.add(net)
    for u in raw.get("unconnected_items", []):
        for item in u.get("items", []):
            desc = item.get("description", "")
            if "[" in desc and "]" in desc:
                net = desc[desc.index("[") + 1:desc.index("]")]
                problem_nets.add(net)

    return KiCadDRCResult(
        violations=violations_total,
        unconnected=unconnected_total,
        violation_types=dict(violation_types),
        problem_nets=problem_nets,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Run KiCad DRC via kicad-cli")
    parser.add_argument("input", help="Input .kicad_pcb file")
    args = parser.parse_args()

    result = run_kicad_drc(args.input)
    print(f"Violations: {result.violations}, Unconnected: {result.unconnected}")
    for t, c in sorted(result.violation_types.items()):
        print(f"  {t}: {c}")
    if result.problem_nets:
        print(f"Problem nets: {', '.join(sorted(result.problem_nets))}")


if __name__ == "__main__":
    main()
