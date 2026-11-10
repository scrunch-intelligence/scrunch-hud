"""Print the Mixing demo readiness report, with an optional read-only Live check."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import mixing_readiness

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true')
    args=parser.parse_args();adapter=None
    if args.live:
        import daw_adapter
        adapter=daw_adapter.DEFAULT
    result=mixing_readiness.check(adapter=adapter)
    print(json.dumps(result,indent=2,allow_nan=False))
    sys.exit(0 if result['localAnalysisReady'] else 1)
