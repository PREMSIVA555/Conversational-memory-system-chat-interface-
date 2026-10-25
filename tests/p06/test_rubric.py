"""Synthetic protocol labels only; none constitute actual pilot ratings."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import pytest
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'evaluator/src'))
from banking_evaluator.rubric import analyze_reviews,review_view,case_view
from banking_evaluator.contracts import digest


def dataset():return json.loads((ROOT/'evaluator/review_sets/pilot_v1.json').read_text(encoding='utf-8'))


def ratings(d,annotator='fixture-rater-1'):
    return dict(schema_version='1',dataset_hash=digest(d),origin='synthetic_test',source='synthetic protocol fixture, not actual human labels',labels=[
        dict(case_id=c['id'],case_hash=digest(case_view(c)),annotator=annotator,support='supported',helpfulness='helpful',note='synthetic fixture') for c in d['cases']])


def predictions(d,order='forward',verbosity='base'):
    return dict(schema_version='1',dataset_hash=digest(d),origin='synthetic_test',source='synthetic predictions, no model call',
        run_id=order+'-'+verbosity,judge_id='fixture-judge',model_id='no-model',control_group='fixture-group',blind=True,order=order,verbosity=verbosity,
        view_hash=digest(review_view(d,order=order,verbosity=verbosity)),predictions=[dict(case_id=c['id'],support='supported',helpfulness='helpful') for c in d['cases']])


def test_empty_actual_pilot_and_fixtures_never_become_humans():
    d=dataset();r=analyze_reviews(d)
    assert r['status']=='awaiting_human_labels' and r['actual_human_annotators']==0 and r['judge']['status']=='not_run'
    assert r['agreement']['support']['agreement_rate'] is None and not r['judge']['volume_screen_met']
    r=analyze_reviews(d,[ratings(d)],[predictions(d)])
    assert r['included_rating_rows']==0 and r['actual_human_annotators']==0 and not r['comparisons']
    assert r['ignored_fixture_rating_rows']==8 and r['ignored_fixture_prediction_batches']==1


def test_synthetic_confusion_unknown_missing_disagreement_and_controls():
    d=dataset();a=ratings(d);b=ratings(d,'fixture-rater-2');b['labels'][0]['support']='unsupported';b['labels'][1]['helpfulness']='unclear'
    p=predictions(d);p['predictions'][2]['support']='unclear';p['predictions'].pop()
    reverse=predictions(d,'reverse');reverse['predictions'][0]['support']='unsupported'
    expanded=predictions(d,verbosity='expanded')
    report=analyze_reviews(d,[a,b],[p,reverse,expanded],test_mode=True)
    assert report['actual_human_annotators']==0 and report['analysis_annotators']==2 and report['judge']['calibrated'] is False
    assert report['disagreement_cases']['support']==1 and report['unknown_labels']['helpfulness']==1
    assert report['agreement']['support']['disagree_pairs']==1
    metric=report['comparisons'][0]['metrics']['calibration']['support']
    assert metric['known_reference_cases']==4 and metric['paired']==3 and metric['missing_predictions']==1 and metric['unclear_predictions']==1
    assert metric['accuracy_including_abstention']==2/3 and metric['accuracy_on_determinate']==1
    assert {c['kind'] for c in report['controls']}=={'order','verbosity'} and not report['judge']['missing_controls']
    assert report['status']=='synthetic_test_only'


def test_view_hides_gold_and_family_split_and_hashes_presentations():
    d=dataset();view=review_view(d);reverse=review_view(d,order='reverse')
    assert all(set(c)=={'id','language','evidence','question','answer'} for c in view['cases'])
    assert reverse['cases']==list(reversed(view['cases']))
    assert digest(view)!=digest(reverse) and digest(view)!=digest(review_view(d,verbosity='expanded'))
    assert 'labels' not in view and 'expected' not in view


@pytest.mark.parametrize('change',[
    lambda d:d['cases'][1].update(split='calibration'),
    lambda d:d['cases'][1].update(id=d['cases'][0]['id']),
    lambda d:d.update(rubric_version='unrecognized'),
    lambda d:d['thresholds'].update(min_annotators=True),
    lambda d:d.update(created_at='2026-01-01'),
])
def test_invalid_dataset_and_split_leakage(change):
    d=dataset();change(d)
    with pytest.raises(ValueError):analyze_reviews(d)


def test_label_prediction_identity_duplicate_and_mode_guards():
    d=dataset();a=ratings(d);p=predictions(d)
    for key,value in [('dataset_hash','wrong'),('origin','invented')]:
        bad=deepcopy(a);bad[key]=value
        with pytest.raises(ValueError):analyze_reviews(d,[bad])
    bad=deepcopy(a);bad['labels'][0]['case_hash']='wrong'
    with pytest.raises(ValueError):analyze_reviews(d,[bad])
    with pytest.raises(ValueError):analyze_reviews(d,[a,a])
    bad=deepcopy(p);bad['view_hash']='wrong'
    with pytest.raises(ValueError):analyze_reviews(d,predictions=[bad])
    with pytest.raises(ValueError):analyze_reviews(d,predictions=[p,p])
    bad=deepcopy(a);bad['origin']='human'
    with pytest.raises(ValueError):analyze_reviews(d,[bad],test_mode=True)


def test_detached_results_and_provisional_human_origin_counting_simulation():
    # Explicit simulation to verify import semantics, NOT a real human run.
    d=dataset();a=ratings(d);a['origin']='human';a['source']='UNIT TEST simulated human-origin record'
    before=deepcopy((d,a));r=analyze_reviews(d,[a])
    assert r['actual_human_annotators']==1 and r['rated_cases']==8 and r['agreement']['support']['agreement_rate'] is None
    assert r['status']=='pilot' and r['judge']['status']=='not_run' and not r['judge']['volume_screen_met']
    r['thresholds']['min_annotators']=99
    assert (d,a)==before


def test_assistance_provenance_simulation_and_excluded_fixtures():
    # Import simulation only; does not collect a real human rating.
    d=dataset();a=ratings(d);a.update(origin='human',source='UNIT TEST assistance simulation',assistance='ai_proposed')
    r=analyze_reviews(d,[a])
    assert r['human_assistance']['ai_proposed']==dict(label_rows=8,annotators=1)
    assert r['human_assistance']['none']==dict(label_rows=0,annotators=0)
    assert r['rating_sources'][0]['assistance']=='ai_proposed' and not r['judge']['calibrated']
    a.pop('assistance')
    assert analyze_reviews(d,[a])['human_assistance']['unknown']['label_rows']==8
    a['assistance']='invented'
    with pytest.raises(ValueError):analyze_reviews(d,[a])
    fixture=ratings(d);fixture['assistance']='ai_proposed'
    assert analyze_reviews(d,[fixture])['human_assistance']['ai_proposed']['label_rows']==0
    assert analyze_reviews(d,[fixture],test_mode=True)['human_assistance']['ai_proposed']['annotators']==0
