"""Freeze or evaluate the separate revision challenger using preserved artifacts only."""
import argparse
import json
from pathlib import Path

from backend import weather_revision_challenger as challenger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['freeze', 'evaluate'])
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--artifact', action='append', type=Path)
    args = parser.parse_args()
    if args.action == 'freeze': result = challenger.freeze(args.output)
    else:
        if not args.artifact: parser.error('evaluate requires preserved --artifact directories')
        result = challenger.evaluate(args.artifact, args.output)
    print(json.dumps({k:v for k,v in result.items() if k in ('status', 'protocol_id', 'evidence_hash',
        'artifact_decisions_verified', 'forecast_receipts_verified', 'official_settlements_verified',
        'coverage', 'excluded_or_pending', 'new_forward_predictions')}))


if __name__ == '__main__': main()
