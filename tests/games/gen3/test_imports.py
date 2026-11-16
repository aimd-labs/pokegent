"""Import every shipped Python module without ROM, emulator, or network access."""

import importlib
import pkgutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


for package_name in ("pokegent", "addons"):
    package = importlib.import_module(package_name)
    for module in pkgutil.walk_packages(package.__path__, f"{package_name}."):
        try:
            importlib.import_module(module.name)
        except ModuleNotFoundError as error:
            if error.name and (
                error.name == "pokegent"
                or error.name.startswith("pokegent.")
                or error.name == "addons"
                or error.name.startswith("addons.")
            ):
                raise
            print(f"Skipped {module.name}: optional dependency {error.name}")

print("All pokegent and addons modules imported")
