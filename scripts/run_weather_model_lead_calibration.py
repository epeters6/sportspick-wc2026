"""Freeze, fit from a completed archive, or audit preserved shadow artifacts offline."""
import argparse
import json
from pathlib import Path

from backend import weather_model_lead_calibration as calibration
from backend.weather_fresh_run_evaluation import evaluate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['freeze', 'fit', 'evaluate'])
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--completion-dir', type=Path)
    parser.add_argument('--artifact', action='append', type=Path)
    parser.add_argument('--fit-evidence', type=Path)
    args = parser.parse_args()
    if args.action == 'freeze':
        result = calibration.freeze(args.output)
    elif args.action == 'fit':
        if not args.archive or not args.completion_dir: parser.error('fit needs --archive and --completion-dir')
        result = calibration.run_fit(args.archive, args.completion_dir, args.output)
    else:
        if not args.artifact: parser.error('evaluate needs one or more --artifact roots')
        result = evaluate(args.artifact, calibration.read(args.output/'protocol.json'), args.output,
                          calibration.read(args.fit_evidence) if args.fit_evidence else None)
    print(json.dumps({k:v for k,v in result.items() if k in ('status', 'protocol_id', 'evidence_hash', 'fitted',
        'artifact_decisions_verified', 'included_decisions', 'excluded_decisions', 'new_calibrated_forward_predictions')}))


if __name__ == '__main__':
    main()
