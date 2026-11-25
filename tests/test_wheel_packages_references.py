"""Packaging regression for issue #39 and reproducibility probe for issue #144.

The loaders used to resolve references/*.yaml via checkout-relative Path(__file__).parents[N],
which silently broke once pokegent was installed as a wheel (site-packages): a fresh install
raised ProfileError before play (missing rom_profiles.yaml). The first wheel built here is
installed into an isolated venv WITHOUT the source tree or PYTHONPATH, and proves the bundled
resources resolve AND parse (load_profiles/load_scene_catalog/load). A stripped copy proves the
isolated probe fails when the YAMLs are absent.

Issue #144 adds one paired build: same SOURCE_DATE_EPOCH, filename, SHA-256 and member stamps,
with shipped package bytes equal to this checkout. See CONTRIBUTING.md for the scoped recipe.

Review requirements (issue #39 review, round 2):
- same interpreter: CI installs with `python -m pip` (setup-python), so the test drives pip/venv
  through sys.executable — no dependency on `uv`.
- parse: assert on the loaders (profiles/catalog/item classes), not file existence alone.
- independency: assert resource paths lie under sys.prefix and outside the source checkout.
- strict: a build/install failure FAILS the test (no pytest.skip); subprocesses are time-bounded.
- negative probe: keep a VALID wheel basename (real distribution name) in a separate directory so
  the install succeeds and resource-absence truly fails; no vacuously-passing broad except.
"""
import base64
import hashlib
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
#: Bound every child subprocess so a hung tool/network cannot stall the suite.
_TIMEOUT = 600  # seconds
#: 1980-01-01T00:00:00Z: the ZIP format's minimum timestamp and a fixed commit-epoch stand-in.
_EPOCH = 315532800
_EPOCH_TIME = time.gmtime(_EPOCH)[:6]
_OVERRIDES = (
    "FIRERED_ROM_PATH", "POKEGENT_ROM_PROFILES", "POKEGENT_SCENES",
    "POKEGENT_BATTLE_ITEM_CLASSES", "POKEGENT_LOCAL_DATA_DIR",
)
_REFERENCE_NAMES = ("rom_profiles.yaml", "scenes.yaml", "battle_item_classes.yaml")
_PACKAGE_PREFIXES = ("pokegent/", "addons/")


def _clean_env() -> dict:
    """Child env without PYTHONPATH or resource/data overrides, so only the wheel resolves."""
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", *_OVERRIDES}}
    env["PYTHONHOME"] = ""
    env["SOURCE_DATE_EPOCH"] = str(_EPOCH)
    return env


def _build_wheel(dist: Path, env: dict) -> Path:
    build = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(dist), str(REPO)],
        capture_output=True, text=True, cwd=str(REPO), env=env, timeout=_TIMEOUT,
    )
    assert build.returncode == 0, (
        "wheel build failed (required by issue #39 regression):\n"
        f"stdout:\n{build.stdout[-900:]}\nstderr:\n{build.stderr[-900:]}"
    )
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {[w.name for w in wheels]}"
    return wheels[0]


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory) -> Path:
    """The single shared first build; the paired build lives in the reproducibility test."""
    return _build_wheel(tmp_path_factory.mktemp("dist-first"), _clean_env())


def _venv_python(venv: Path, env: dict) -> Path:
    create = subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        capture_output=True, text=True, env=env, timeout=_TIMEOUT,
    )
    assert create.returncode == 0, (
        "venv creation failed (required by issue #39 regression):\n"
        f"{create.stderr[-600:]}"
    )
    return venv / ("bin/python" if sys.platform != "win32" else "Scripts/python.exe")


def _install(py: Path, wheel: Path, env: dict) -> None:
    install = subprocess.run(
        [str(py), "-m", "pip", "install", str(wheel)],
        capture_output=True, text=True, env=env, timeout=_TIMEOUT,
    )
    assert install.returncode == 0, (
        f"wheel install failed for {wheel.name}:\n{install.stderr[-900:]}"
    )


def _wheel_has_references(wheel: Path) -> bool:
    with zipfile.ZipFile(wheel) as zf:
        members = zf.namelist()
    return all(any(f"references/{n}" in m for m in members) for n in _REFERENCE_NAMES)


def _wheel_members(wheel: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(wheel) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def _assert_epoch_timestamps(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as zf:
        bad = [(i.filename, i.date_time) for i in zf.infolist() if i.date_time != _EPOCH_TIME]
    assert not bad, f"members not stamped with {_EPOCH_TIME}: {bad[:5]}"


def _assert_members_match_checkout(members: dict[str, bytes]) -> list[str]:
    """Compare each shipped package member with its checkout file; *.dist-info is generated."""
    checked = []
    for name, data in members.items():
        if not name.startswith(_PACKAGE_PREFIXES):
            continue
        source = REPO / name
        assert source.is_file(), f"wheel ships {name} with no owning checkout file"
        assert data == source.read_bytes(), f"wheel member {name} differs from checkout bytes"
        checked.append(name)
    assert "pokegent/resources.py" in checked
    assert "pokegent/references/rom_profiles.yaml" in checked
    return checked


def _probe() -> str:
    return (
        "import os, pathlib, sys\n"
        "import pokegent\n"
        "from pokegent.resources import reference_path as rp\n"
        "from pokegent.games.gen3.firered.rom import rom_data as rd\n"
        "from pokegent.state import catalog as cat\n"
        "from pokegent.actions.battle import item_classes as ic\n"
        "prefix = pathlib.Path(sys.prefix).resolve()\n"
        "checkout = pathlib.Path(os.environ['POKEGENT_CHECKOUT']).resolve()\n"
        "for name in ('rom_profiles.yaml', 'scenes.yaml', 'battle_item_classes.yaml'):\n"
        "    p = pathlib.Path(rp(name)).resolve()\n"
        "    assert p.is_relative_to(prefix), (name, 'not under sys.prefix', p)\n"
        "    assert not p.is_relative_to(checkout), (name, 'came from checkout', p)\n"
        "    assert p.is_file(), p\n"
        "profiles = rd.load_profiles()\n"
        "scenes = cat.load_scene_catalog()\n"
        "classes = ic.load()\n"
        "assert len(profiles) > 0\n"
        "assert len(scenes) > 0\n"
        "assert len(getattr(classes, 'classes', ())) > 0\n"
        "print('WHEEL_RESOURCES_OK')\n"
    )


def test_wheel_bundles_and_parses_reference_resources(built_wheel, tmp_path):
    """The built wheel must contain the reference YAMLs, and an ISOLATED install (same
    interpreter, pip) must import and parse them with paths under sys.prefix and outside the
    source checkout."""
    env = _clean_env()
    # Probe needs the checkout root to assert resources do NOT resolve there.
    env["POKEGENT_CHECKOUT"] = str(REPO)

    # Direct packaging gate: the wheel itself must ship the resources.
    assert _wheel_has_references(built_wheel), (
        f"wheel lacks {_REFERENCE_NAMES} in {built_wheel.name}"
    )

    venv = tmp_path / "iso"
    py = _venv_python(venv, env)
    _install(py, built_wheel, env)
    run = subprocess.run(
        [str(py), "-c", _probe()], capture_output=True, text=True,
        cwd=str(tmp_path), env=env, timeout=_TIMEOUT,
    )
    assert run.returncode == 0 and "WHEEL_RESOURCES_OK" in run.stdout, (
        f"isolated wheel import/parse failed:\n{run.stdout[-900:]}\n{run.stderr[-900:]}"
    )


def test_wheel_without_references_fails_isolated(built_wheel, tmp_path):
    """Negative probe (review): strip the reference YAMLs from a copy of the shared wheel that
    keeps a VALID distribution basename in its own directory; pip install must SUCCEED, and the
    isolated resolution must then FAIL — proving the regression is actually caught."""
    env = _clean_env()
    env["POKEGENT_CHECKOUT"] = str(REPO)

    # Build a resource-stripped wheel with a VALID name in a SEPARATE dir, fixing RECORD.
    stripped_dir = tmp_path / "stripped"
    stripped_dir.mkdir()
    stripped = stripped_dir / built_wheel.name  # same valid distribution name
    members = _wheel_members(built_wheel)
    keep = {n: d for n, d in members.items() if "references/" not in n}
    dist_info = next(n.split("/")[0] for n in members if n.endswith("RECORD"))
    record_lines = []
    for n in sorted(keep):
        data = keep[n]
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        record_lines.append(f"{n},sha256={digest},{len(data)}")
    record_lines.append(f"{dist_info}/RECORD,,")
    keep[f"{dist_info}/RECORD"] = "\n".join(record_lines).encode()
    with zipfile.ZipFile(stripped, "w", zipfile.ZIP_DEFLATED) as zout:
        for n, data in keep.items():
            zout.writestr(n, data)

    # Install must succeed (valid wheel, just missing resources) ...
    venv = tmp_path / "iso2"
    py = _venv_python(venv, env)
    _install(py, stripped, env)

    # ... then the isolated probe must FAIL to resolve the stripped resource.
    run = subprocess.run(
        [
            str(py), "-c",
            "from pokegent.resources import reference_path as rp\n"
            "print(rp('rom_profiles.yaml'))\n",
        ],
        capture_output=True, text=True, cwd=str(tmp_path), env=env, timeout=_TIMEOUT,
    )
    assert run.returncode != 0 or "FileNotFoundError" in run.stderr, (
        "stripped wheel still resolved references (regression not caught)"
    )


def test_wheel_build_is_reproducible_at_epoch(built_wheel, tmp_path):
    """The paired build must match the shared first wheel byte-for-byte on this toolchain, stamp
    every member with the fixed epoch, and ship the bytes that are actually in this checkout."""
    second = _build_wheel(tmp_path / "dist-second", _clean_env())

    assert second.name == built_wheel.name
    first_hash = hashlib.sha256(built_wheel.read_bytes()).hexdigest()
    second_hash = hashlib.sha256(second.read_bytes()).hexdigest()
    assert second_hash == first_hash, f"same-filename wheels differ: {first_hash} != {second_hash}"

    _assert_epoch_timestamps(built_wheel)

    checked = _assert_members_match_checkout(_wheel_members(built_wheel))
    assert len(checked) > 10, f"source comparison covered too few members: {checked}"


def test_source_member_comparison_rejects_one_byte_change(built_wheel):
    """A one-byte-different member map must be rejected, so the source check is not vacuous."""
    members = _wheel_members(built_wheel)
    target = "pokegent/resources.py"
    tampered = dict(members)
    tampered[target] = tampered[target][:-1] + bytes([tampered[target][-1] ^ 0x01])
    with pytest.raises(AssertionError, match=target):
        _assert_members_match_checkout(tampered)
