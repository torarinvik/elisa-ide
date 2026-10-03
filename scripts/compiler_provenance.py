#!/usr/bin/env python3
"""Fingerprint the Elisa sources and verify compiler build provenance."""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys


SOURCE_SUFFIXES = {".elisa", ".elisai"}
SOURCE_ROOTS = ("src", "elisacore_std")


def source_fingerprint(compiler_root: pathlib.Path) -> str:
    """Hash every stage1 source input, including uncommitted and untracked files."""
    files: list[tuple[str, pathlib.Path]] = []
    for root_name in SOURCE_ROOTS:
        source_root = compiler_root / root_name
        if not source_root.is_dir():
            continue
        for path in source_root.rglob("*"):
            if path.is_file() and path.suffix in SOURCE_SUFFIXES:
                files.append((path.relative_to(compiler_root).as_posix(), path))

    digest = hashlib.sha256()
    for relative_path, path in sorted(files, key=lambda item: item[0]):
        encoded_path = relative_path.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fact_value(facts: list[str], name: str) -> str:
    prefix = f"{name}="
    for fact in facts:
        if fact.startswith(prefix):
            return fact[len(prefix) :]
    raise ValueError(f"provenance is missing {name}")


def verify(provenance_path: pathlib.Path, compiler_root: pathlib.Path) -> None:
    with provenance_path.open(encoding="utf-8") as source:
        provenance = json.load(source)
    facts = provenance.get("facts")
    if not isinstance(facts, list) or not all(isinstance(fact, str) for fact in facts):
        raise ValueError("provenance facts are missing or malformed")

    checks = (
        ("stage1_sources_sha256", source_fingerprint(compiler_root)),
        (
            "stage1_product_sha256",
            file_sha256(compiler_root / "bin" / "elisac-stage1"),
        ),
        (
            "stage1_runtime_sha256",
            file_sha256(compiler_root / "build" / "runtime" / "elisacore_runtime.o"),
        ),
    )
    for name, actual in checks:
        expected = fact_value(facts, name)
        if expected != actual:
            raise ValueError(
                f"{name} changed during the build (recorded {expected}, current {actual})"
            )


def main(argv: list[str]) -> int:
    valid_arguments = (
        len(argv) == 3 and argv[1] == "fingerprint"
    ) or (len(argv) == 4 and argv[1] == "verify")
    if not valid_arguments:
        print(
            "usage: compiler_provenance.py fingerprint <compiler-root> | "
            "verify <provenance.json> <compiler-root>",
            file=sys.stderr,
        )
        return 64

    try:
        if argv[1] == "fingerprint":
            print(source_fingerprint(pathlib.Path(argv[2]).resolve()))
        else:
            verify(pathlib.Path(argv[2]), pathlib.Path(argv[3]).resolve())
            print("compiler provenance matches the current source, stage1 product, and runtime")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"compiler provenance: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
