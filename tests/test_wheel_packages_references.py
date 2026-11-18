"""Packaging regression for issue #39: the references/ resources must survive a wheel build.

The loaders used to resolve references/*.yaml via checkout-relative Path(__file__).parents[N],
which silently broke once pokegent was installed as a wheel (site-packages): a fresh install
raised ProfileError before play (missing rom_profiles.yaml). This test builds a wheel, installs
it into an isolated venv WITHOUT the source tree on the path, and proves the bundled resources
load — catching the packaging gap in CI.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    not shutil.which("python3") and not Path(sys.executable).exists(),
    reason="need a python to build/install wheels",
)
def test_wheel_bundles_reference_resources(tmp_path):
    """Build the wheel, install it isolated, and load profiles + scene catalog from it.

    The isolated venv deliberately does NOT get the repo `pokegent` importable (we install
    only the built wheel), so any checkout-relative parent-path bug would surface as a
    missing resource instead of silently finding the source tree.
    """
    venv = tmp_path / "iso"
    dist = tmp_path / "dist"

    # 1. Build a wheel from the repo source (uv build preferred; pip wheel fallback).
    build = None
    if shutil.which("uv"):
        build = subprocess.run(
            ["uv", "build", "--wheel", "-o", str(dist), str(REPO)],
            capture_output=True, text=True, cwd=str(REPO),
        )
    if build is None or build.returncode != 0:
        build = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(dist), str(REPO)],
            capture_output=True, text=True, cwd=str(REPO),
        )
    if build.returncode != 0:
        pytest.skip(f"wheel build unavailable: {build.stderr[-200:]}")
    wheels = list(dist.glob("*.whl"))
    assert wheels, f"no wheel produced: {build.stdout[-300:]}"

    # 2. Install the wheel (with its runtime deps) into an isolated venv, so the repo
    #    source tree on PYTHONPATH cannot shadow the installed package.
    if shutil.which("uv"):
        subprocess.run(["uv", "venv", str(venv)], check=True, capture_output=True)
        py = venv / "bin/python"
        install = subprocess.run(
            ["uv", "pip", "install", "--python", str(py), str(wheels[0])],
            capture_output=True, text=True,
        )
    else:
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
        py = venv / ("bin/python" if sys.platform != "win32" else "Scripts/python.exe")
        install = subprocess.run(
            [str(py), "-m", "pip", "install", str(wheels[0])],
            capture_output=True, text=True,
        )
    if install.returncode != 0:
        pytest.skip(f"wheel deps install failed (no network?): {install.stderr[-200:]}")

    # 3. Prove the bundled resources resolve and parse inside the wheel (not source tree).
    probe = (
        "from pokegent.resources import reference_path as rp\n"
        "import pokegent.games.gen3.firered.rom.rom_data as rd\n"
        "import pokegent.state.catalog as cat\n"
        "import pokegent.actions.battle.item_classes as ic\n"
        "for name in ('rom_profiles.yaml', 'scenes.yaml', 'battle_item_classes.yaml'):\n"
        "    assert rp(name).is_file(), rp(name)\n"
        "assert rd.default_profiles_path().is_file()\n"
        "assert cat.default_catalog_path().is_file()\n"
        "assert ic.default_path().is_file()\n"
        "print('WHEEL_RESOURCES_OK')\n"
    )
    run = subprocess.run(
        [str(py), "-c", probe], capture_output=True, text=True,
        cwd=str(tmp_path),  # not the repo, so the source tree cannot shadow the wheel
    )
    assert run.returncode == 0, f"wheel resource load failed:\n{run.stdout}\n{run.stderr}"
    assert "WHEEL_RESOURCES_OK" in run.stdout
