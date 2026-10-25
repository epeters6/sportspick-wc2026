import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from scipy.optimize import check_grad

from backend import weather_revision_challenger as c
from backend import weather_model_lead_calibration as e
from backend import weather_fresh_run_evaluation as audit
from backend.ml.weather_learning.dataset import digest
from backend.tests.test_weather_model_lead_calibration import bundle


NOW = datetime(2026, 10, 25, 19, tzinfo=timezone.utc)


def event(target='2026-10-27', station='KPHL', venue='kalshi', index=0):
    return {'id': f'{target}-{station}-{venue}-{index}', 'station': station, 'target_date': target,
        'venue': venue, 'decision_at': (date.fromisoformat(target)-timedelta(days=1)).isoformat()+'T12:00Z',
        'vectors': {'market': [.5,.5]}, 'contracts': [{'low_f':None,'high_f':70.}, {'low_f':70.,'high_f':None}],
        'costs': [.51,.51], 'depths': [10,10], 'verified_runs': {name: {
            'current': {'high_f':72.}, 'previous': {'high_f':68.}, 'high_revision_f':4.}
            for name in c.SPEC['models']}}


def rows(first, days, signal=True):
    result = []
    for d in range(days):
        day = (date.fromisoformat(first)+timedelta(days=d)).isoformat()
        for station in range(20):
            ev = event(day, f'S{station:02}')
            # Each date has both directions; synthetic example intentionally
            # plants a learnable signal, NOT evidence of market-beating accuracy.
            sign = 1 if station % 2 else -1
            x = np.array([[sign,0.,0.,0.],[-sign,0.,0.,0.]])
            y = [int(sign>0), int(sign<0)] if signal else [int(station%4<2), int(station%4>=2)]
            result.append({'event':ev, 'market':np.array([.5,.5]), 'x':x, 'outcomes':y,
                           'settlement_fingerprint':digest(y)})
    return result


def test_revision_direction_consensus_and_disagreement_features():
    ev = event(); x,reason = c.features(ev)
    assert reason is None and x.shape == (2,4)
    assert np.all(x[0,:3]<0) and np.all(x[1,:3]>0)
    assert x[0,3]<0 and x[1,3]>0
    # Auxiliary disagreement discounts the same GFS revision feature.
    ev['verified_runs']['gfs_hrrr']['current']['high_f'] += 12
    ev['verified_runs']['gfs_hrrr']['previous']['high_f'] += 12
    other,_ = c.features(ev)
    assert abs(other[0,0]) < abs(x[0,0])


def test_unchanged_runs_zero_revision_and_missing_pair_market_fallback():
    ev=event()
    for p in ev['verified_runs'].values(): p['previous']['high_f']=72.; p['high_revision_f']=0.
    x,_=c.features(ev); assert np.all(x[:,:3]==0)
    ev['verified_runs'].pop('gfs_hrrr'); x,reason=c.features(ev)
    assert reason=='MISSING_VERIFIED_MODEL_PAIR'
    assert np.array_equal(c.probability(np.array([.01,.99]),x,[1.,1.,1.,1.]),[.01,.99])


@pytest.mark.parametrize('bad', ['nan','revision','partition','market'])
def test_bad_feature_inputs_fail_closed(bad):
    ev=event()
    if bad=='nan': ev['verified_runs']['gfs_global']['current']['high_f']=float('nan')
    if bad=='revision': ev['verified_runs']['gfs_global']['high_revision_f']=7
    if bad=='partition': ev['contracts'][0]['high_f']=60
    if bad=='market': ev['vectors']['market']=[.6,.6]
    with pytest.raises(ValueError): c.features(ev)


def test_probability_conservation_cap_and_exact_zero_weight():
    rng=np.random.default_rng(44)
    for n in range(2,16):
        m=rng.dirichlet(np.ones(n)); x=rng.uniform(-1,1,(n,4))
        p=c.probability(m,x,np.ones(4)*2)
        assert np.all(p>=0) and p.sum()==pytest.approx(1)
        assert max(abs(p-m))<=.025+1e-12
        assert np.array_equal(c.probability(m,x,np.zeros(4)),m)
    m=np.array([0.,.01,.99]); x=np.ones((3,4))
    assert c.probability(m,x,[1,1,1,1])[0]==0
    with pytest.raises(ValueError,match='PARAMETERS'): c.probability(m,x,[3,0,0,0])


def test_regularized_likelihood_gradient():
    data=rows('2026-10-27',2); w=c.weights(data); beta=np.array([.1,-.1,.2,.05])
    assert check_grad(lambda b:c.objective(b,data,w)[0],lambda b:c.objective(b,data,w)[1],beta)<1e-6


def test_synthetic_signal_can_fit_and_pass_later_validation():
    fit=c.fit_and_validate(rows('2026-10-27',14),rows('2026-11-12',7))
    assert fit['fitted'] and fit['selected']=='revision_challenger' and fit['gate']['passed']
    assert fit['candidate_coefficients'][0]>0
    assert fit['gate']['comparison']['minus_market']['brier']['interval_95'][1]<0


def test_null_signal_and_reversed_later_signal_fall_back_exactly():
    training=rows('2026-10-27',14)
    null=c.fit_and_validate(rows('2026-10-27',14,False),rows('2026-11-12',7,False))
    assert null['selected']=='market' and null['selected_coefficients']==[0.]*4
    validation=rows('2026-11-12',7)
    for r in validation:r['outcomes'].reverse()
    bad=c.fit_and_validate(training,validation)
    assert bad['fitted'] and bad['selected']=='market'


def test_duplicate_venues_do_not_change_fit_or_evidence_counts():
    training=rows('2026-10-27',14)
    duplicated=training+copy.deepcopy(training[:20])*15
    assert np.allclose(c.fit_coefficients(training),c.fit_coefficients(duplicated),atol=1e-10)
    assert c.coverage(training)=={**c.coverage(duplicated),'rows':len(training)}
    small=training[:20]*100
    assert c.fit_and_validate(small,rows('2026-11-12',7))['fitted'] is False


def test_one_favorable_date_or_missing_models_cannot_pass_gate():
    data=rows('2026-11-12',1)*50
    assert c.validation_gate(data,np.array([1.,0,0,0]))['passed'] is False
    data=rows('2026-11-12',7)
    for r in data:r['x']=None
    result=c.validation_gate(data,np.array([1.,0,0,0]))
    assert result['passed'] is False and result['comparison']['minus_market']['brier']['mean']==0


def test_protocol_immutable_and_late_freeze_rejected(tmp_path):
    protocol=c.freeze(tmp_path,NOW)
    assert c.freeze(tmp_path,NOW+timedelta(days=3))==protocol
    protocol=copy.deepcopy(protocol); protocol['spec']['ridge']=99
    with pytest.raises(ValueError,match='HASH'):c.verify_protocol(protocol)


def test_late_protocol_rejected(tmp_path):
    with pytest.raises(ValueError,match='TOO_LATE'):
        c.freeze(tmp_path,datetime(2026,10,26,tzinfo=timezone.utc))


def partition_input(target,settled_at,venue='kalshi'):
    ev=event(target,venue=venue); ev['fingerprint']=digest(ev)
    label={'outcomes':[0,1],'settled_at':settled_at,'fingerprint':'official-'+ev['id']}
    return ev,label


def test_label_cutoffs_embargo_future_and_protocol_boundaries(tmp_path):
    p=c.freeze(tmp_path,NOW); events={}; labels={}
    cases=[('2026-10-27','2026-10-28T12:00Z'), # valid training
           ('2026-10-28','2026-11-11T00:00Z'), # training label too late
           ('2026-11-10','2026-11-11T12:00Z'), # embargo
           ('2026-11-12','2026-11-20T00:00Z'), # validation label too late
           ('2026-11-21','2026-11-22T12:00Z')]
    for target,at in cases:
        ev,l=partition_input(target,at); events[ev['id']]=ev; labels[ev['id']]=l
    parts,gaps,_=c.partition(events,labels,p,datetime(2026,12,1,tzinfo=timezone.utc))
    assert len(parts['train'])==1 and not parts['validation'] and len(parts['test'])==1
    assert gaps['OUTSIDE_FIXED_WINDOWS_OR_EMBARGO']==1
    assert gaps['LABEL_NOT_AVAILABLE_BEFORE_TRAIN_CUTOFF']==1
    assert gaps['LABEL_NOT_AVAILABLE_BEFORE_VALIDATION_CUTOFF']==1
    parts,_,_=c.partition(events,labels,p,NOW)
    assert not any(parts.values())


def test_test_missing_features_and_zero_market_support_are_not_hidden(tmp_path):
    p=c.freeze(tmp_path,NOW); ev,l=partition_input('2026-11-21','2026-11-22T12:00Z')
    ev['verified_runs'].pop('gfs_hrrr'); ev['vectors']['market']=[1.,0.]
    parts,gaps,_=c.partition({ev['id']:ev},{ev['id']:l},p,datetime(2026,12,1,tzinfo=timezone.utc))
    assert len(parts['test'])==1 and parts['test'][0]['x'] is None
    result=c.summary(parts['test'],np.ones(4))
    assert result['scores']==result['same_rows_market'] and result['scores']['brier']==2


def test_real_artifact_loader_reused_and_no_fit_on_current_pilot(bundle,tmp_path):
    root,_,_=bundle; output=tmp_path/'out'; c.freeze(output,NOW)
    before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    result=c.evaluate([root],output,NOW)
    assert result['artifact_decisions_verified']==1 and result['forecast_receipts_verified']==2
    assert result['status']=='awaiting_verified_revision_training_data'
    assert result['fit']['fitted'] is False and result['new_forward_predictions']==0
    assert result['retrospective_test']['scores']['brier'] is None
    assert before=={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert c.evaluate([root],output,NOW)==result
    # The same production reader must refuse re-signed, altered decisions.
    path=root/'reports/weather_verified_shadow/decision/d.json'; bad=e.read(path)
    bad['vectors']['fresh_gfs_ablation']=[.9,.1]; path.write_text(json.dumps(e.signed(bad,'fingerprint')))
    with pytest.raises(ValueError,match='REPRODUCTION'):c.evaluate([root],output,NOW)


def test_test_labels_do_not_change_fit_and_corrupt_fit_is_not_reset(tmp_path,monkeypatch):
    c.freeze(tmp_path,NOW)
    parts={'train':rows('2026-10-27',14),'validation':rows('2026-11-12',7),'test':rows('2026-11-21',7)}
    for part in parts.values():
        for r in part:r['event']['fingerprint']=digest(r['event'])
    monkeypatch.setattr(audit,'load_artifacts',lambda roots:({},{},{}))
    monkeypatch.setattr(c,'partition',lambda *a:(parts,{},{}))
    now=datetime(2026,12,1,tzinfo=timezone.utc)
    first=c.evaluate([],tmp_path,now)
    for row in parts['test']:row['outcomes'].reverse()
    monkeypatch.setattr(c,'fit_coefficients',lambda *a:pytest.fail('must reuse immutable earlier fit'))
    second=c.evaluate([],tmp_path,now)
    assert first['fit']==second['fit'] and first['replayed_test_vectors']==second['replayed_test_vectors']
    assert first['retrospective_test']['scores']!=second['retrospective_test']['scores']
    fitpath=next(tmp_path.glob('*_fit.json')); bad=e.read(fitpath); bad['selected_coefficients']=[2]*4
    fitpath.write_text(json.dumps(bad))
    with pytest.raises(ValueError,match='HASH'):c.evaluate([],tmp_path,now)


def test_venue_costs_separate_and_unsupported_selection_is_not_profit():
    data=rows('2026-11-21',7)
    for r in data:r['event']['costs']=[.1,.1]
    other=copy.deepcopy(data)
    for r in other:r['event']['venue']='polymarket'; r['event']['id']+='poly'
    result=c.summary(data+other,np.array([1.,0,0,0]))
    costs=result['quote_only_cost_sensitivity']
    assert set(costs)=={'kalshi','polymarket'}
    assert costs['kalshi']['0.0']['actual_fills_verified'] is False
    assert costs['kalshi']['0.03']['hypothetical_unit_net']<costs['kalshi']['0.0']['hypothetical_unit_net']
