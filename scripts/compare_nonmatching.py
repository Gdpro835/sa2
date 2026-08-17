#!/usr/bin/env python3
"""Compile one NONMATCH function both ways and compare its machine code.

The reference build uses the checked-in assembly include. The candidate build
uses the C body while preserving matching-only register hints guarded by
``#ifndef NON_MATCHING``.
"""

from __future__ import annotations

import argparse
import difflib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SYMBOL_RE = re.compile(
    r"^([0-9a-fA-F]+)\s+\w\s+F\s+(\S+)\s+([0-9a-fA-F]+)\s+(\S+)$"
)
INSTRUCTION_RE = re.compile(
    r"^\s*([0-9a-fA-F]+):\s+((?:[0-9a-fA-F]{4})(?:\s+[0-9a-fA-F]{4})?|[0-9a-fA-F]{8})\s"
)
RELOCATION_RE = re.compile(r"^\s*([0-9a-fA-F]+):\s+(R_ARM_\S+)\s+(\S+)")


def run(command: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout if capture else ""


def find_symbol(objdump: str, object_file: Path, symbol: str) -> tuple[int, int]:
    table = run([objdump, "-t", str(object_file)], capture=True)
    for line in table.splitlines():
        match = SYMBOL_RE.match(line)
        if match and match.group(4) == symbol:
            start = int(match.group(1), 16)
            size = int(match.group(3), 16)
            if size == 0:
                raise RuntimeError(f"symbol {symbol!r} has no recorded size")
            return start, size
    raise RuntimeError(f"symbol {symbol!r} was not found in {object_file}")


def canonical_disassembly(objdump: str, object_file: Path, symbol: str) -> list[str]:
    start, symbol_size = find_symbol(objdump, object_file, symbol)
    # Handwritten NONMATCH assembly commonly includes its final `.align 2, 0`
    # bytes in the symbol size, while agbcc excludes equivalent inter-function
    # padding from a C symbol. Compare through the next word boundary so both
    # representations cover the same ROM bytes.
    size = (symbol_size + 3) & ~3
    output = run(
        [
            objdump,
            "-dr",
            "-z",
            f"--start-address={start}",
            f"--stop-address={start + size}",
            str(object_file),
        ],
        capture=True,
    )

    lines: list[str] = [f"size={size:#x}"]
    for line in output.splitlines():
        instruction = INSTRUCTION_RE.match(line)
        if instruction:
            offset = int(instruction.group(1), 16) - start
            encoding = instruction.group(2).replace(" ", "").lower()
            lines.append(f"{offset:04x}: {encoding}")
            continue

        relocation = RELOCATION_RE.match(line)
        if relocation:
            offset = int(relocation.group(1), 16) - start
            lines.append(f"{offset:04x}: {relocation.group(2)} {relocation.group(3)}")

    return lines


def object_path(source: Path, game: str) -> Path:
    try:
        relative = source.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError("source must be inside the repository") from exc

    if relative.suffix != ".c" or relative.parts[0] != "src":
        raise ValueError("source must be a .c file below src/")

    return ROOT / "build" / "gba" / game / relative.with_suffix(".o")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="C translation unit containing NONMATCH")
    parser.add_argument("symbol", help="function symbol to compare")
    parser.add_argument("--game", choices=("sa1", "sa2"), default="sa2")
    parser.add_argument(
        "--object-source",
        type=Path,
        help="translation unit that includes source (for example camera.c for an .inc.c file)",
    )
    parser.add_argument("--objdump", default="arm-none-eabi-objdump")
    args = parser.parse_args()

    source = (ROOT / args.source).resolve() if not args.source.is_absolute() else args.source.resolve()
    compile_source = args.object_source or source
    compile_source = (
        (ROOT / compile_source).resolve() if not compile_source.is_absolute() else compile_source.resolve()
    )
    obj = object_path(compile_source, args.game)
    make_target = str(obj.relative_to(ROOT))

    if shutil.which(args.objdump) is None:
        parser.error(f"{args.objdump!r} is not available on PATH")

    make_base = [
        "make",
        "-B",
        "NODEP=1",
        f"GAME_NAME={args.game}",
        make_target,
    ]

    with tempfile.TemporaryDirectory(prefix="sa2-nonmatch-") as directory:
        reference = Path(directory) / "reference.o"
        candidate = Path(directory) / "candidate.o"

        print(f"Building ASM reference: {make_target}")
        run(make_base)
        shutil.copy2(obj, reference)

        try:
            print(f"Building C candidate:   {args.symbol}")
            run([*make_base, "FORCE_C_NONMATCH=1"])
            shutil.copy2(obj, candidate)

            reference_lines = canonical_disassembly(args.objdump, reference, args.symbol)
            candidate_lines = canonical_disassembly(args.objdump, candidate, args.symbol)
        finally:
            # A make variable is not part of timestamp dependency tracking. Restore the
            # regular object so a later incremental ROM build cannot accidentally link
            # the FORCE_C_NONMATCH candidate.
            shutil.copy2(reference, obj)

    if reference_lines == candidate_lines:
        print(f"MATCH: {args.symbol}")
        return 0

    print(f"NONMATCH: {args.symbol}")
    for line in difflib.unified_diff(
        reference_lines,
        candidate_lines,
        fromfile="ASM reference",
        tofile="C candidate",
        lineterm="",
    ):
        print(line)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
