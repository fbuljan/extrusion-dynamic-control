import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# The app runs as package `server` from inside app/ (Dockerfile WORKDIR); tests and tools import it the same way.
sys.path[:0] = [str(REPO / "app"), str(REPO)]
CAPTURES = REPO / "data" / "captures"


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: live end-to-end runs (3-26 min); opt in with E2E_RUN=1")
