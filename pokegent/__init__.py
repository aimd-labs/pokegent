"""pokegent — AI-driven Pokemon gameplay agent."""

from .env import get_env  # noqa: F401

__version__ = "0.1.1"

# Keep legacy deployments working even for older code paths that read
# os.environ directly for one release. New code should call get_env instead.
