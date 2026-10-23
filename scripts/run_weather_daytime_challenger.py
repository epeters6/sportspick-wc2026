"""Test a separate daytime challenger using completed local evidence only."""
import argparse
import json
from pathlib import Path

from backend.weather_daytime_challenger import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', type=Path, required=True)
    parser.add_argument('--market-history', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = run(args.history, args.market_history, args.output)
    print(json.dumps({'run_id': report['run_id'], 'status': report['status'],
                      'temperature': report['temperature']['overall'],
                      'market_status': report['market']['status'], 'market_scores': report['market'].get('scores'),
                      'market_test_dates': report['market'].get('test_dates'),
                      'guard_weight': (report['market'].get('guard') or {}).get('weight')}, indent=2))


if __name__ == '__main__':
    main()
