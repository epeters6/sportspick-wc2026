"""Refresh a saved segment audit using new forecasts and newly official labels."""
import argparse
import gzip
import json
import os
from pathlib import Path
import httpx
from dotenv import dotenv_values
from backend.weather_history_cache import refresh_history

SOURCE = 'weather_station_learning_v3'
COLUMNS = 'id,source,prob,created_at,resolved_at,is_correct,outcome,metadata'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path('reports/weather_segment_input.json'))
    args = parser.parse_args()
    cache = args.input.with_suffix('.history.json.gz')
    if not cache.exists() and args.input.exists():
        saved = json.loads(args.input.read_text(encoding='utf8'))
        rows = saved['v3']
        if rows:
            # Replay from the latest saved creation time, rather than assuming the
            # old export captured outcomes up to today's clock.
            watermark = max(r['created_at'] for r in rows).replace('Z', '+00:00')
            with gzip.open(cache, 'wt', encoding='utf8') as stream:
                json.dump({'version': 1, 'scope': {'sources': [SOURCE], 'since': None},
                           'watermark': watermark, 'rows': rows}, stream)
    cfg = dotenv_values('.env')
    url = cfg.get('SUPABASE_URL') or os.environ.get('SUPABASE_URL')
    key = cfg.get('SUPABASE_SERVICE_ROLE_KEY') or os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key or 'vflrqcjovhyntphumnsm' not in url:
        raise RuntimeError('Expected project credentials unavailable')
    with httpx.Client(base_url=url+'/rest/v1/', headers={'apikey': key, 'Authorization': 'Bearer '+key}, timeout=45) as client:
        def fetch(source, field, cursor, since):
            offset = 0
            while True:
                params = {'select': COLUMNS, 'source': 'eq.'+source,
                          'order': 'created_at,id', 'offset': offset, 'limit': 500}
                if field:
                    params[field] = 'gte.'+cursor
                response = client.get('model_predictions', params=params)
                if response.status_code != 200:
                    # Never print headers, credential values or response bodies.
                    raise RuntimeError('Database read failed: '+str(response.status_code))
                page = response.json()
                yield from page
                if len(page) < 500:
                    break
                offset += 500
        rows, stats = refresh_history(fetch, cache, [SOURCE])
    temporary = args.input.with_suffix('.tmp')
    temporary.write_text(json.dumps({'v3': rows, 'refresh': stats}), encoding='utf8')
    temporary.replace(args.input)
    print(json.dumps(stats))


if __name__ == '__main__':
    main()
