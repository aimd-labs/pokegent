"""Packaging regression for issue #39: the references/ resources must survive a wheel build.

The loaders used to resolve references/*.yaml via checkout-relative Path(__file__).parents[N],
which silently broke once pokegent was installed as a wheel (site-packages): a fresh install
raised ProfileError before play (missing rom_profiles.yaml). This test builds a wheel, installs
it into an isolated venv WITHOUT the source tree or PYTHONPATH, and proves the bundled resources
resolve AND parse (load_profiles/load_scene_catalog/load), plus a negative probe that strips the
YAMLs from a second wheel and asserts the isolated probe then fails.

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
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
#: Bound every child subprocess so a hung tool/network cannot stall the suite.
_TIMEOUT = 600  # seconds
_OVERRIDES = (
    "FIRERED_ROM_PATH", "POKEGENT_ROM_PROFILES", "POKEGENT_SCENES",
    "POKEGENT_BATTLE_ITEM_CLASSES", "POKEGENT_LOCAL_DATA_DIR",
)
_REFERENCE_NAMES = ("rom_profiles.yaml", "scenes.yaml", "battle_item_classes.yaml")


def _clean_env() -> dict:
    """Child env without PYTHONPATH or resource/data overrides, so only the wheel resolves."""
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", *_OVERRIDES}}
    env["PYTHONHOME"] = ""
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
    assert wheels, f"no wheel produced:\n{build.stdout[-900:]}"
    return wheels[0]


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


def test_wheel_bundles_and_parses_reference_resources(tmp_path):
    """The built wheel must contain the reference YAMLs, and an ISOLATED install (same
    interpreter, pip) must import and parse them with paths under sys.prefix and outside the
    source checkout."""
    env = _clean_env()
    # Probe needs the checkout root to assert resources do NOT resolve there.
    env["POKEGENT_CHECKOUT"] = str(REPO)
    dist = tmp_path / "dist"
    wheel = _build_wheel(dist, env)

    # Direct packaging gate: the wheel itself must ship the resources.
    assert _wheel_has_references(wheel), f"wheel lacks {_REFERENCE_NAMES} in {wheel.name}"

    venv = tmp_path / "iso"
    py = _venv_python(venv, env)
    _install(py, wheel, env)
    run = subprocess.run(
        [str(py), "-c", _probe()], capture_output=True, text=True,
        cwd=str(tmp_path), env=env, timeout=_TIMEOUT,
    )
    assert run.returncode == 0 and "WHEEL_RESOURCES_OK" in run.stdout, (
        f"isolated wheel import/parse failed:\n{run.stdout[-900:]}\n{run.stderr[-900:]}"
    )


def test_wheel_without_references_fails_isolated(tmp_path):
    """Negative probe (review): strip the reference YAMLs from a second wheel that keeps a
    VALID distribution basename in its own directory; pip install must SUCCEED, and the
    isolated resolution must then FAIL — proving the regression is actually caught."""
    env = _clean_env()
    env["POKEGENT_CHECKOUT"] = str(REPO)
    dist = tmp_path / "dist"
    wheel = _build_wheel(dist, env)

    # Build a resource-stripped wheel with a VALID name in a SEPARATE dir, fixing RECORD.
    stripped_dir = tmp_path / "stripped"
    stripped_dir.mkdir()
    stripped = stripped_dir / wheel.name  # same valid distribution name
    with zipfile.ZipFile(wheel) as zin:
        members = {n: zin.read(n) for n in zin.namelist()}
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
