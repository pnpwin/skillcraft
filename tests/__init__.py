"""Repository test paths for the bundled skill and development fixtures."""
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / 'plugins/skillcraft/skills/skillcraft'
FIXTURES = REPO / 'tests/fixtures'
sys.path.insert(0, str(SKILL))
