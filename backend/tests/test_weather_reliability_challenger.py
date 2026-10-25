import copy
import json
from datetime import date, timedelta

import numpy as np
import pytest
from scipy.special import ndtri

from backend import weather_reliability_challenger as model
from backend.ml.weather_learning.dataset import digest
from backend.tests.test_weather_station_distribution import history


def errors(raw=3., correction=3.):
    return [{'station': station, 'target_date': (date(2026,7,1)+timedelta(days=i)).isoformat(),
             'label_target_cutoff': model.parent.cutoff((date(2026,7,1)+timedelta(days=i)).isoformat()),
             'raw_error_f': raw+(i%3-1)*.2, 'correction_f': correction}
            for i in range(60) for station in ('KPHL','KNYC')]


def test_correction_must_earn_weight_and_overshoot_is_shrunk():
    good=model.fit_reliability(errors(),'2026-09-03',['KPHL','KNYC','KBOS'])
    assert good['stations']['KPHL']['weight']==1.
    assert good['stations']['KBOS']['weight']==0.
    harmful=model.fit_reliability(errors(raw=0,correction=-5),'2026-09-03',['KPHL'])
    assert harmful['stations']['KPHL']['weight']==0.
    overshoot=model.fit_reliability(errors(raw=3,correction=6),'2026-09-03',['KPHL'])
    assert overshoot['stations']['KPHL']['weight']==.5


def test_future_labels_duplicates_and_invalid_errors_are_rejected_or_excluded():
    rows=errors()
    fit=model.fit_reliability(rows,'2026-09-03',['KPHL'])
    late={**rows[0],'target_date':'2026-09-02','label_target_cutoff':'2026-08-30','raw_error_f':-999}
    assert fit==model.fit_reliability(rows+[late],'2026-09-03',['KPHL'])
    with pytest.raises(ValueError,match='DUPLICATE'):
        model.fit_reliability(rows*2,'2026-09-03',['KPHL'])
    bad=copy.deepcopy(rows); bad[-1]['label_target_cutoff']=bad[-1]['target_date']
    with pytest.raises(ValueError,match='LEAKAGE'):
        model.fit_reliability(bad,'2026-09-03',['KPHL'])
    bad=copy.deepcopy(rows); bad[-1]['raw_error_f']=float('nan')
    with pytest.raises(ValueError,match='INVALID_RELIABILITY'):
        model.fit_reliability(bad,'2026-09-03',['KPHL'])


def test_age_requires_actual_receipt_and_decision_times():
    row={'forecast_high_f':70,'forecast_low_f':55,'run_initialization_utc':'2026-10-24T00:00:00Z'}
    assert model.context(row)['age_band'] is None
    row.update(decision_at='2026-10-24T13:00:00Z',forecast_received_at='2026-10-24T12:59:00Z',forecast_timing_verified=True)
    assert model.context(row)['run_age_hours']==13
    assert model.context(row)['age_band']==1
    for change in ({'forecast_received_at':'2026-10-24T13:01:00Z'},
                   {'run_initialization_utc':'2026-10-24T14:00:00Z'},
                   {'forecast_timing_verified':False}):
        with pytest.raises(ValueError,match='TIMING|AGE'):
            model.context({**row,**change})


def shape_errors(use_age=False):
    rows=[]
    for i in range(60):
        target=(date(2026,7,1)+timedelta(days=i)).isoformat()
        kind=i%2
        z=float(ndtri(((i//2)+.5)/30))*(.6 if kind==0 else 1.4)
        for station in ('KPHL','KNYC','KBOS','KMIA'):
            rows.append({'station':station,'target_date':target,'label_target_cutoff':model.parent.cutoff(target),
                         'range_band':1 if use_age else kind,'age_band':kind*2 if use_age else None,'z':z})
    return rows


def test_conditioned_shape_narrows_and_widens_with_supported_earlier_errors():
    rows=shape_errors()
    a=model.fit_context_shape(rows,'2026-09-03','KPHL',{'range_band':0,'age_band':None})
    b=model.fit_context_shape(rows,'2026-09-03','KPHL',{'range_band':1,'age_band':None})
    assert a['range_condition_used'] and b['range_condition_used']
    assert a['knots'][1]<model.parent.NORMAL_KNOTS[1]<b['knots'][1]
    assert all(x<y for x,y in zip(a['knots'],a['knots'][1:]))
    assert a['age_condition_used'] is False


def test_verified_age_selects_only_supported_age_group_and_unknown_age_is_not_imputed():
    rows=shape_errors(use_age=True)
    a=model.fit_context_shape(rows,'2026-09-03','KPHL',{'range_band':1,'age_band':0})
    b=model.fit_context_shape(rows,'2026-09-03','KPHL',{'range_band':1,'age_band':2})
    assert a['age_condition_used'] and b['age_condition_used']
    assert a['knots'][1]<b['knots'][1]
    missing=model.fit_context_shape(rows,'2026-09-03','KPHL',{'range_band':1,'age_band':1})
    unknown=model.fit_context_shape(rows,'2026-09-03','KPHL',{'range_band':1,'age_band':None})
    assert missing['knots']==unknown['knots']
    assert missing['age_status']=='insufficient_verified_age_support'
    assert unknown['age_status']=='unknown_age'


def test_sparse_conditions_back_off_without_duplicating_evidence():
    rows=shape_errors()
    for r in rows: r['range_band']=0
    fit=model.fit_context_shape(rows,'2026-09-03','KPHL',{'range_band':2,'age_band':0})
    assert not fit['range_condition_used'] and not fit['age_condition_used']
    with pytest.raises(ValueError,match='DUPLICATE'):
        model.fit_context_shape(rows*2,'2026-09-03','KPHL',{'range_band':2,'age_band':None})
    late={**rows[0],'target_date':'2026-09-02','label_target_cutoff':'2026-08-30','z':999}
    assert fit==model.fit_context_shape(rows+[late],'2026-09-03','KPHL',{'range_band':2,'age_band':0})


def test_replay_uses_only_earlier_labels_and_does_not_mutate_inputs():
    rows=history('2026-04-03',130)
    saved=copy.deepcopy(rows)
    original=model.replay(rows)
    assert rows==saved
    changed=copy.deepcopy(rows)
    for r in changed:
        if r['target_date']>='2026-08-01': r['observed_high_f']+=100
    altered=model.replay(changed)
    before=lambda e:[p for p in e['predictions'] if p['target_date']<='2026-08-03']
    assert before(original)==before(altered)
    assert all(p['context']['age_band'] is None for p in original['predictions'])
    assert all(0<=p['correction_weight']<=1 for p in original['predictions'])
    assert [p for p in original['predictions'] if p['target_date']=='2026-08-08'] != [p for p in altered['predictions'] if p['target_date']=='2026-08-08']


def test_artifact_rejects_prediction_and_evidence_tampering():
    artifact={**model.replay(history('2026-04-03',100)),'identity':{'test':True},
              'historical_as_of_verified':False,'live_ready':False,'paper_execution_enabled':False}
    artifact['artifact_id']=digest(artifact)
    model.verify_artifact(artifact,{'test':True})
    for field,value in [('mean_f',999),('knots',[0,.3,.6,1.,2.])]:
        bad=copy.deepcopy(artifact); bad['predictions'][-1][field]=value
        bad['artifact_id']=digest({k:v for k,v in bad.items() if k!='artifact_id'})
        with pytest.raises(ValueError,match='PREDICTION'):
            model.verify_artifact(bad,{'test':True})
    bad=copy.deepcopy(artifact); bad['shape_errors'][0]['z']+=1
    bad['artifact_id']=digest({k:v for k,v in bad.items() if k!='artifact_id'})
    with pytest.raises(ValueError,match='ERROR_LINEAGE'):
        model.verify_artifact(bad,{'test':True})


def test_partial_archive_is_rejected_and_corrupt_cache_is_not_overwritten(tmp_path):
    source=tmp_path/'history'; source.mkdir()
    raw=b'[]'; status=b'{"status":"in_progress","model":"gfs_global"}'; market=b'[]'
    (source/'dataset.json').write_bytes(raw); (source/'latest.json').write_bytes(status)
    mp=tmp_path/'market.json'; mp.write_bytes(market)
    output=tmp_path/'output'
    with pytest.raises(ValueError,match='COMPLETED_GFS'):
        model.run(source,mp,output)
    status=b'{"status":"complete","model":"gfs_global"}'
    (source/'latest.json').write_bytes(status)
    identity={'spec_hash':digest(model.SPEC),'implementation_hash':model.implementation_hash(),
              'input_sha256':model.hashlib.sha256(raw).hexdigest(),'status_sha256':model.hashlib.sha256(status).hexdigest(),
              'market_input_sha256':model.hashlib.sha256(market).hexdigest(),'numpy':np.__version__,'scipy':model.scipy.__version__}
    output.mkdir(); p=output/(digest(identity)+'.json'); p.write_text('{"corrupt":true}')
    with pytest.raises(ValueError,match='EXISTING_RELIABILITY_REPORT'):
        model.run(source,mp,output)
    assert p.read_text()=='{"corrupt":true}'
