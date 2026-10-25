"""Evaluate a separate reliability challenger on completed local GFS evidence."""
import argparse
import json
from pathlib import Path

from backend.weather_reliability_challenger import run


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history',type=Path,required=True)
    parser.add_argument('--market-history',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=run(args.history,args.market_history,args.output)
    print(json.dumps({'run_id':report['run_id'],'status':report['status'],
                      'temperature':report['temperature']['overall'],
                      'correction_weights':report['temperature']['correction_weights'],
                      'market_scores':report['market'].get('scores'),
                      'guard_weight':(report['market'].get('guard') or {}).get('weight'),
                      'age_effect_status':report['age_effect_status']},indent=2))


if __name__=='__main__':
    main()
