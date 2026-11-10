"""Astra's warm GP (Brooklyn Rose Part 6 winner: 60/60 reachable matches, no false claims), as tested.

The modules here are byte-identical copies of the research candidate (hashes in manifest.json; the README is
CANDIDATE-README.md), so the replay evidence still describes this code. They import each other by bare name,
hence this folder on the path. Nothing here touches Live: warm_gp_session_candidate.run_session drives a host
object that the product supplies (reference_loop, when the warm GP engine is switched on).
"""
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from warm_gp_candidate import METRIC, WarmGP  # noqa: E402,F401
from warm_gp_session_candidate import CaptureBudget, GuardFailure, SessionPolicy, run_session  # noqa: E402,F401
from reference_descriptor_candidate import BANDS, descriptors, report_descriptors  # noqa: E402,F401
