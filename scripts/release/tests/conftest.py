import sys
from pathlib import Path

# Release scripts are run as `python3 scripts/release/<name>.py` in CI, which puts
# scripts/release on sys.path[0] so `import _lib` works. Mirror that for tests that
# load the modules via importlib.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
