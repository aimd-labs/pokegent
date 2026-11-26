"""Check a caller-supplied wheel against a caller-supplied wheel manifest.

The reference and expected source commit come from the caller. Hashes check bytes only;
no trusted-source, build or attestation guarantee is made.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from tools import wheel_manifest as manifest

MAX_MANIFEST_BYTES = 4 << 20
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class VerifyError(Exception):
    """A rejected reference, argument or wheel; record content is never echoed."""


def _invalid(field: str) -> VerifyError:
    return VerifyError(f"invalid reference manifest field {field}")


def _rows(value: object) -> list[dict]:
    if not isinstance(value, list) or len(value) > manifest.MAX_MEMBERS:
        raise _invalid("files")
    names = []
    for index, row in enumerate(value):
        if (not isinstance(row, dict)
                or not isinstance(row.get("name"), str) or not row["name"]
                or not isinstance(row.get("sha256"), str)
                or not SHA256_RE.fullmatch(row["sha256"])
                or type(row.get("size")) is not int or row["size"] < 0):
            raise _invalid(f"files[{index}]")
        names.append(row["name"])
    if len(set(names)) != len(names):
        raise VerifyError("duplicate member name in reference")
    return value


def _reference(path: Path) -> dict:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
    except OSError:
        raise VerifyError(f"{path}: cannot read reference manifest") from None
    if len(raw) > MAX_MANIFEST_BYTES:
        raise VerifyError(f"{path}: manifest exceeds {MAX_MANIFEST_BYTES} byte limit")
    try:
        root = json.loads(raw)
    except (ValueError, RecursionError):
        raise VerifyError(f"{path}: reference manifest is not valid JSON") from None
    if not isinstance(root, dict):
        raise VerifyError(f"{path}: reference manifest root must be an object")

    required = {"tool", "version", "wheel", "files", "dist_info",
                "supplied_source_commit", "source_commit_verified",
                "manifest_environment"}
    for field in required:
        if field not in root:
            raise _invalid(field)
    if (root["tool"] != "wheel_manifest" or type(root["version"]) is not int
            or root["version"] != 1):
        raise VerifyError("unsupported reference manifest tool or version")
    wheel = root["wheel"]
    if (not isinstance(wheel, dict)
            or not isinstance(wheel.get("sha256"), str)
            or not SHA256_RE.fullmatch(wheel["sha256"])
            or type(wheel.get("size")) is not int or wheel["size"] < 0):
        raise _invalid("wheel")
    _rows(root["files"])
    info = root["dist_info"]
    if (not isinstance(info, dict)
            or not isinstance(info.get("name"), str) or not info["name"]
            or not isinstance(info.get("version"), str) or not info["version"]
            or "generator" not in info
            or not (info["generator"] is None
                    or isinstance(info["generator"], str) and info["generator"])):
        raise _invalid("dist_info")
    commit = root["supplied_source_commit"]
    if not isinstance(commit, str) or not manifest.COMMIT_RE.fullmatch(commit):
        raise _invalid("supplied_source_commit")
    if root["source_commit_verified"] is not False:
        raise VerifyError("reference claims source_commit_verified")
    environment = root["manifest_environment"]
    if (not isinstance(environment, dict)
            or not isinstance(environment.get("python"), str)
            or not isinstance(environment.get("zlib"), str)
            or environment.get("build_environment_proof") is not False):
        raise _invalid("manifest_environment")
    return root


def verify_wheel_manifest(wheel: str | Path, reference: str | Path, *,
                          source_commit: str) -> dict:
    if not isinstance(source_commit, str) or not manifest.COMMIT_RE.fullmatch(source_commit):
        raise VerifyError("source commit must be 40 lowercase hex characters")
    root = _reference(Path(reference))
    try:
        expected = manifest.build_manifest(wheel, source_commit=source_commit)
    except (manifest.ManifestError, OSError) as exc:
        raise VerifyError(str(exc)) from None
    info = root["dist_info"]
    comparisons = (
        ("wheel.sha256", root["wheel"]["sha256"], expected["wheel"]["sha256"]),
        ("wheel.size", root["wheel"]["size"], expected["wheel"]["size"]),
        ("files", root["files"], expected["files"]),
        ("dist_info.name", info["name"], expected["dist_info"]["name"]),
        ("dist_info.version", info["version"], expected["dist_info"]["version"]),
        ("dist_info.generator", info["generator"],
         expected["dist_info"]["generator"]),
        ("supplied_source_commit", root["supplied_source_commit"], source_commit),
    )
    mismatches = [name for name, actual, wanted in comparisons if actual != wanted]
    return {"matches": not mismatches, "mismatches": mismatches,
            "scope": "caller-supplied inputs; byte hashes only; "
                     "no trusted-source, build or attestation guarantee"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wheel", metavar="WHEEL")
    parser.add_argument("manifest", metavar="MANIFEST_JSON")
    parser.add_argument("--source-commit", required=True, metavar="FORTY_HEX")
    args = parser.parse_args(argv)
    try:
        result = verify_wheel_manifest(args.wheel, args.manifest,
                                       source_commit=args.source_commit)
    except (VerifyError, OSError) as exc:
        print(f"verify_wheel_manifest: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["matches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
