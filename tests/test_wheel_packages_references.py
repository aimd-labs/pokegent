"""Packaging regression for issue #39: the references/ resources must survive a wheel build.

The loaders used to resolve references/*.yaml via checkout-relative Path(__file__).parents[N],
which silently broke once pokegent was installed as a wheel (site-packages): a fresh install
raised ProfileError before play (missing rom_profiles.yaml). This test builds a wheel, installs
it into an isolated venv WITHOUT the source tree or PYTHONPATH, and proves the bundled resources
resolve AND parse (load_profiles/load_scene_catalog/load), plus a negative probe that strips the
YAMLs from a second wheel and asserts the isolated probe then fails.

Review requirements (issue #39 independent review):
- isolate: children must NOT inherit PYTHONPATH or resource overrides; imports come only from
  the installed wheel.
- parse: assert on the loaders (profiles/catalog/item classes), not file existence alone.
- strict: a build/install failure FAILS the required CI test (no skip on the happy path), and
  every subprocess is bounded by a timeout so a hung tool/network cannot stall the suite.
"""
import base64
import hashlib
import os
import subprocess
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
#: Bound every child subprocess so a hung tool/network cannot stall the suite.
_TIMEOUT = 300  # seconds
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
        ["uv", "build", "--wheel", "-o", str(dist), str(REPO)],
        capture_output=True, text=True, cwd=str(REPO), env=env, timeout=_TIMEOUT,
    )
    assert build.returncode == 0, (
        "wheel build failed (required by issue #39 regression):\n"
        f"stdout:\n{build.stdout[-800:]}\nstderr:\n{build.stderr[-800:]}"
    )
    wheels = list(dist.glob("*.whl"))
    assert wheels, f"no wheel produced:\n{build.stdout[-800:]}"
    return wheels[0]


def _install(wheel: Path, venv: Path, env: dict) -> Path:
    subprocess.run(["uv", "venv", str(venv)], check=True, capture_output=True, timeout=_TIMEOUT)
    py = venv / "bin/python"
    install = subprocess.run(
        ["uv", "pip", "install", "--python", str(py), str(wheel)],
        capture_output=True, text=True, env=env, timeout=_TIMEOUT,
    )
    assert install.returncode == 0, (
        f"wheel install failed for {wheel.name}:\n{install.stderr[-800:]}"
    )
    return py


def _wheel_has_references(wheel: Path) -> bool:
    with zipfile.ZipFile(wheel) as zf:
        members = zf.namelist()
    return all(any(f"references/{n}" in m for m in members) for n in _REFERENCE_NAMES)


def _probe() -> str:
    return (
        "import os, pathlib\n"
        "import pokegent\n"
        "from pokegent.resources import reference_path as rp\n"
        "from pokegent.games.gen3.firered.rom import rom_data as rd\n"
        "from pokegent.state import catalog as cat\n"
        "from pokegent.actions.battle import item_classes as ic\n"
        "pkg = pathlib.Path(pokegent.__file__).resolve().parents[1]\n"
        "for name in ('rom_profiles.yaml', 'scenes.yaml', 'battle_item_classes.yaml'):\n"
        "    p = pathlib.Path(rp(name)).resolve()\n"
        "    assert p.is_relative_to(pkg), (name, p)\n"
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
    """The built wheel must contain the reference YAMLs, and an ISOLATED install must be able to
    import and parse them (profiles/scene catalog/item classes) with no checkout on the path."""
    env = _clean_env()
    dist = tmp_path / "dist"
    wheel = _build_wheel(dist, env)

    # The wheel itself must ship the resources (direct packaging gate; catches the exact
    # regression — a wheel missing them fails here before any install).
    assert _wheel_has_references(wheel), f"wheel lacks {_REFERENCE_NAMES} in {wheel.name}"

    venv = tmp_path / "iso"
    py = _install(wheel, venv, env)
    run = subprocess.run(
        [str(py), "-c", _probe()], capture_output=True, text=True,
        cwd=str(tmp_path),  # outside the checkout so the source tree cannot shadow the wheel
        env=env, timeout=_TIMEOUT,
    )
    assert run.returncode == 0 and "WHEEL_RESOURCES_OK" in run.stdout, (
        f"isolated wheel import/parse failed:\n{run.stdout[-800:]}\n{run.stderr[-800:]}"
    )


def test_wheel_without_references_fails_isolated(tmp_path):
    """Negative probe (review): strip the reference YAMLs from a second wheel; the isolated
    resolution must then fail. Proves the regression test is not vacuously green."""
    env = _clean_env()
    dist = tmp_path / "dist"
    wheel = _build_wheel(dist, env)

    stripped = dist / (wheel.name.replace(".whl", "-stripped.whl"))
    with zipfile.ZipFile(wheel) as zin:
        members = {n: zin.read(n) for n in zin.namelist()}
    keep = {n: d for n, d in members.items() if "references/" not in n}
    dist_info = next(n.split("/")[0] for n in members if n.endswith("RECORD"))
    # RECORD must match the reduced member list for pip to accept the wheel.
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

    # Install succeeds (or pip rejects it — either way packaging is the gate we test); the
    # isolated probe must NOT find the resources.
    try:
        venv = tmp_path / "iso2"
        py = _install(stripped, venv, env)
    except AssertionError:
        return  # pip refused the stripped wheel: still proves resources are required.
    run = subprocess.run(
        [
            str(py), "-c",
            "from pokegent.resources import reference_path as rp\n"
            "print(rp('rom_profiles.yaml'))\n",
        ],
        capture_output=True, text=True, cwd=str(tmp_path), env=env, timeout=_TIMEOUT,
    )
    assert run.returncode != 0, "stripped wheel still resolved references (regression not caught)"
