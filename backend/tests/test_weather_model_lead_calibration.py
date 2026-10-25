import base64
import copy
import gzip
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend import weather_model_lead_calibration as c
from backend import weather_fresh_run_evaluation as e
from backend import weather_verified_shadow as shadow
from backend import weather_verified_runs as runs
from backend import weather_daytime_paper as paper
from backend.ml.weather_learning.dataset import digest
from backend.weather_decision_archive import make_plan
from backend.tests.test_weather_archive_retest import example
from backend.tests.test_weather_verified_shadow import event, receipt, NOW, RUN
from backend.tests.test_weather_daytime_paper import official_receipts


@pytest.fixture(scope='module')
def data():
    plan = make_plan(date(2026,4,3), 200)
    rows = [example(d) for d in plan['decisions']]
    for row in rows:
        for i,name in enumerate(c.SPEC['models']):
            f = row['forecasts'][name]['current']
            band = c.lead_band(f['run_initialization_utc'], f['standard_day_start'])
            f['high_f'] = 74-(i+1)-band
            f['low_f'] = 52+(i+1)+band
            # Different uncertainty by model and lead, without test-driven fitting.
            if c.SPEC['calibration_start'] <= row['target_date'] <= c.SPEC['calibration_end']:
                noise = math.sin(date.fromisoformat(row['target_date']).toordinal())*(i+1)
                f['high_f'] += noise; f['low_f'] += noise
    return rows, plan


@pytest.fixture(scope='module')
def fitted(data):
    return c.fit(data[0])[0]


def test_models_metrics_and_leads_have_distinct_bias_and_width(data, fitted):
    assert fitted['historical_as_of_verified'] is False
    for metric in c.SPEC['metrics']:
        seen = []
        for model in c.SPEC['models']:
            supported = [v for v in fitted['cells'][metric][model].values() if v['supported']]
            assert supported
            seen.append(supported[0]['stations']['KPHL']['sigma_f'])
            for band,cell in fitted['cells'][metric][model].items():
                if cell['supported']:
                    expected = c.SPEC['models'].index(model)+1+int(band)
                    assert cell['stations']['KPHL']['bias_f'] == pytest.approx(expected if metric=='high' else -expected)
        assert len(set(round(s,2) for s in seen)) == 3


def test_future_test_outcomes_do_not_change_fitted_parameters(data, fitted):
    changed = copy.deepcopy(data[0])
    for row in changed:
        if row['target_date'] >= c.SPEC['test_start']:
            row['observed_high_f'] += 100; row['observed_low_f'] -= 100
    other, _, _ = c.fit(changed)
    assert other == fitted


def test_identical_runs_across_slots_do_not_create_extra_fit_weight(data):
    rows = [r for r in data[0] if '2026-04-03' <= r['target_date'] <= '2026-04-15']
    first = c.records(rows, 'gfs_global', 'high')
    assert c.records(rows+rows[:30]*5, 'gfs_global', 'high') == first
    changed = copy.deepcopy(rows[0]); changed['forecasts']['gfs_global']['current']['high_f'] += 1
    with pytest.raises(ValueError, match='CONFLICTING'):
        c.records(rows+[changed], 'gfs_global', 'high')


def test_sparse_model_lead_never_borrows_other_model_cells(data):
    rows = data[0][:100]
    cells = c.fit_cells(rows, rows, 'gfs_hrrr', 'high')
    assert not any(v['supported'] for v in cells.values())
    with pytest.raises(ValueError, match='LEAD'):
        c.lead_band('2026-10-27T12:00Z','2026-10-27T06:00Z')


def test_mixture_distribution_score_and_paired_test(data, fitted):
    components = [{'mean_f': 70., 'sigma_f': 2.}]*3
    assert c.mixture_crps(72.,components) == pytest.approx(c.normal_crps(72.,70.,2.))
    result = c.weather_evaluation([r for r in data[0] if r['target_date'] >= c.SPEC['test_start']], fitted)
    for metric in c.SPEC['metrics']:
        assert result[metric]['paired_coverage']['stations'] == 21
        assert result[metric]['scores']['calibrated_mixture']['mae_f'] < 1e-8
        assert 0 <= result[metric]['scores']['calibrated_mixture']['coverage_80'] <= 1
    assert c.interval_scores(70.,components)['width_80_f'] == pytest.approx(c.interval_scores(70.,components[:1])['width_80_f'])


def test_protocol_immutable_and_late_freeze_refused(tmp_path):
    now = datetime(2026,10,25,18,tzinfo=timezone.utc)
    p = c.freeze(tmp_path, now)
    assert c.freeze(tmp_path, now+timedelta(days=1)) == p
    changed = copy.deepcopy(p); changed['evaluation']['minimum_quote_edge'] = .01
    with pytest.raises(ValueError, match='HASH'): c.verify_protocol(changed)
    with pytest.raises(ValueError, match='FROZEN'):
        c.freeze(tmp_path/'late', datetime(2026,10,26,tzinfo=timezone.utc))


def test_no_completion_never_opens_partial_dataset_or_fits(tmp_path):
    c.freeze(tmp_path/'out', datetime(2026,10,25,tzinfo=timezone.utc))
    result = c.run_fit(tmp_path/'nonexistent-archive',tmp_path/'no-completion',tmp_path/'out')
    assert result['fitted'] is False and result['downloads'] == 0
    assert not (tmp_path/'nonexistent-archive').exists()


def test_fit_reuses_verified_result_and_fails_on_corruption(tmp_path,data,monkeypatch):
    c.freeze(tmp_path,datetime(2026,10,25,tzinfo=timezone.utc))
    monkeypatch.setattr(c,'complete_input',lambda *args:(data[0],{},digest(data[0])))
    first=c.run_fit('unused','unused',tmp_path)
    monkeypatch.setattr(c,'fit',lambda *args:pytest.fail('verified result must not retrain'))
    assert c.run_fit('unused','unused',tmp_path)==first
    path=tmp_path/(first['identity']+'_fit.json')
    bad=copy.deepcopy(first); bad['model']['label_target_cutoff']='2026-10-26'
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError,match='HASH'): c.run_fit('unused','unused',tmp_path)


def test_final_input_requires_matching_hashes_and_full_requests(tmp_path, data):
    rows, plan = data
    arc, comp = tmp_path/'archive',tmp_path/'retest'; arc.mkdir(); comp.mkdir()
    h = digest(rows)
    report = {'status':'offline_retest_complete','dataset_hash':h}
    rh = digest(report)
    status = {'plan_id':plan['plan_id'],'status':'complete','requests_available':len(plan['requests']),
              'dataset_hash':h,'dataset_file':h+'_dataset.json'}
    for p,v in ((arc/'plan.json',plan),(arc/'latest.json',status),(arc/(h+'_dataset.json'),rows),
                (comp/(rh+'_report.json'),report),(comp/'completion.json',{'report_file':rh+'_report.json','report_hash':rh,'dataset_hash':h})):
        p.write_text(json.dumps(v))
    accepted, rejected, actual = c.complete_input(arc,comp)
    assert actual == h and len(accepted) == len(rows) and not rejected
    status['requests_available'] -= 1; (arc/'latest.json').write_text(json.dumps(status))
    with pytest.raises(ValueError, match='INCOMPLETE'): c.complete_input(arc,comp)
    status['requests_available'] += 1; status['dataset_file'] = '../other.json'
    (arc/'latest.json').write_text(json.dumps(status))
    with pytest.raises(ValueError, match='PATH'): c.complete_input(arc,comp)


@pytest.fixture
def bundle(tmp_path):
    model = json.loads((Path(__file__).resolve().parents[2]/'research/weather_daytime_paper/model.json').read_text())
    ev = event(model); root = tmp_path/'artifact'; base = root/'reports/weather_verified_shadow'
    values = [(root/'research/weather_daytime_paper/model.json',model), (base/'decision/d.json',ev)]
    for i,r in enumerate([receipt(model), receipt(model,RUN-timedelta(hours=6),high=75.)]):
        values.append((base/f'receipt/{i}.json',{'receipt_id':runs.receipt_identity(r),
            'gzip_base64':base64.b64encode(gzip.compress(json.dumps(r).encode())).decode()}))
    for path,value in values: path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(value))
    return root,model,ev


def test_real_shaped_receipts_reproduce_decisions_and_refuse_corruption(bundle):
    root,model,ev = bundle
    events,_,receipts = e.load_artifacts([root,root])
    assert len(events)==1 and len(receipts)==2
    bad = copy.deepcopy(ev); bad['vectors']['fresh_gfs_ablation'] = [.9,.1]
    bad = paper.signed(bad,'fingerprint')
    (root/'reports/weather_verified_shadow/decision/d.json').write_text(json.dumps(bad))
    with pytest.raises(ValueError, match='REPRODUCTION'): e.load_artifacts([root])


def test_official_settlement_validated_and_recomputed(bundle):
    root,model,ev = bundle
    state = shadow.reserve_event(shadow.initial_state(model,NOW-timedelta(minutes=10)),ev)
    state['pending']=None; state=paper.signed(state,'state_hash')
    now=NOW+timedelta(days=2)
    after=shadow.settle(state,ev,[0,1],official_receipts(now),now)
    record=after['pending']['record']; path=root/'reports/weather_verified_shadow/settlement/s.json'
    path.parent.mkdir(); path.write_text(json.dumps(record))
    events,settled,_=e.load_artifacts([root]); assert len(settled)==1
    bad=copy.deepcopy(record); bad['official_receipts'][0]['content_hash']='bad'
    path.write_text(json.dumps(paper.signed(bad,'fingerprint')))
    with pytest.raises(ValueError, match='RECEIPT'): e.load_artifacts([root])


def test_fixed_date_exclusion_and_no_outcome_means_unknown(bundle,tmp_path):
    root,_,_=bundle
    protocol=c.freeze(tmp_path/'out',datetime(2026,10,25,tzinfo=timezone.utc))
    result=e.evaluate([root],protocol,tmp_path/'out')
    assert result['artifact_decisions_verified']==1 and result['included_decisions']==0
    assert result['excluded_decisions']=={'OUTSIDE_FIXED_EVALUATION_DATES':1}
    assert result['forward_saved_arms']['market']['scores']['brier'] is None
    assert result['new_calibrated_forward_predictions']==0
    assert e.evaluate([root],protocol,tmp_path/'out')==result


def test_new_calibration_replay_does_not_invent_missing_models(bundle,fitted):
    _,_,ev=bundle
    values=e.calibrated_vectors(ev,fitted)
    assert 'gfs_global' in values and 'calibrated_mixture' not in values
    assert sum(values['gfs_global'])==pytest.approx(1)
    bad=copy.deepcopy(fitted); bad['label_target_cutoff']='2026-10-27'
    bad=c.signed(bad,'model_id')
    with pytest.raises(ValueError,match='SCOPE'): e.calibrated_vectors(ev,bad)


def test_shared_station_date_weight_paired_baseline_and_venue_costs():
    rows=[{'id':'a','station':'KPHL','target_date':'2026-10-27','venue':'kalshi','costs':[.1,None],'depths':[1,0],
           'vectors':{'market':[.2,.8]}},
          {'id':'b','station':'KPHL','target_date':'2026-10-27','venue':'polymarket','costs':[.1,.9],'depths':[1,1],
           'vectors':{'market':[.2,.8]}},
          {'id':'c','station':'KDEN','target_date':'2026-10-27','venue':'kalshi','costs':[.1,.9],'depths':[1,1],
           'vectors':{'market':[.2,.8]}}]
    assert e.station_mean(rows,[0,0,1])==.5
    labels={r['id']:{'outcomes':[0,1]} for r in rows}
    summary=e.summarize(rows,labels,{'candidate':{r['id']:[.4,.6] for r in rows[:2]}})['candidate']
    assert summary['paired_station_dates']==1
    assert summary['same_rows_market']['brier']==pytest.approx(.08)
    assert summary['quote_only_cost_sensitivity']['kalshi']['0.0']['selected_contracts']==1
    assert summary['quote_only_cost_sensitivity']['polymarket']['0.03']['hypothetical_unit_net']==pytest.approx(-.13)
    assert summary['minus_market']['brier']['interval_95'] is None


def test_dates_not_snapshots_control_confidence():
    rows=[{'station':'KPHL','target_date':'2026-10-27'}]*1000
    assert e.difference(rows,[.1]*1000)['interval_95'] is None
    with pytest.raises(ValueError, match='VECTOR'): e.score([.3,.7],[1,1])
