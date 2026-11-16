"""Optional local operator dashboard addon.

Only the small HTML/CSS/JavaScript dashboard in ``static/`` is packaged.
No historical frontend assets or game artwork are bundled.
"""

from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"

__all__ = ["STATIC_DIR"]
