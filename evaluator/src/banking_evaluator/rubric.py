"""Human rubric pilot and imported-judge diagnostics. Never calls a model.

Human/model origins are operator attestations, not authenticated identities.
Synthetic fixtures never count as actual annotators in an operational report.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime
from .contracts import canonical,digest,versioned

RUBRIC_VERSION='support-helpfulness/1'
AXES={'support':('supported','unsupported','unclear'),'helpfulness':('helpful','unhelpful','unclear')}
RUBRIC={
    'version':RUBRIC_VERSION,
    'support':'supported: every material claim follows from supplied evidence; unsupported: contradiction or unsupported material claim; unclear: insufficient evidence/language to decide.',
    'helpfulness':'helpful: clear, relevant answer or appropriate limit/next step; unhelpful: misleading, irrelevant or unhelpful; unclear: cannot decide.',
    'independence':'Rate support and helpfulness separately. Exact money/ownership controls remain authoritative; this rubric cannot replace them.'}


def text(v):return isinstance(v,str) and bool(v.strip()) and len(v)<=20000


def exact(v,keys):
    if not isinstance(v,dict) or set(v)!=set(keys):raise ValueError('Invalid rubric fields')


def validate_dataset(dataset):
    versioned(dataset,('id','version','rubric_version','created_at','source','thresholds','cases'))
    if not all(text(dataset[k]) for k in ('id','version','created_at','source')) or dataset['rubric_version']!=RUBRIC_VERSION:raise ValueError('Unknown rubric/dataset')
    if datetime.fromisoformat(dataset['created_at'].replace('Z','+00:00')).tzinfo is None:raise ValueError('Freeze timestamp requires timezone')
    exact(dataset['thresholds'],('min_known_per_split','min_families_per_split','min_annotators'))
    if any(type(v) is not int or v<1 for v in dataset['thresholds'].values()):raise ValueError('Positive screening thresholds required')
    cases=dataset['cases'];families={};ids=set()
    if not isinstance(cases,list) or not 2<=len(cases)<=1000:raise ValueError('Bounded dataset required')
    for c in cases:
        exact(c,('id','family','split','language','evidence','question','answer','expanded_answer'))
        if not all(text(c[k]) for k in ('id','family','question','answer','expanded_answer')) or c['language'] not in ('en','hi') or c['split'] not in ('calibration','holdout'):raise ValueError('Invalid review case')
        if not isinstance(c['evidence'],list) or not c['evidence'] or not all(text(s) for s in c['evidence']):raise ValueError('Evidence required')
        if c['id'] in ids:raise ValueError('Duplicate case')
        ids.add(c['id'])
        if c['family'] in families and families[c['family']]!=c['split']:raise ValueError('Family split leakage')
        families[c['family']]=c['split']
    if set(families.values())!={'calibration','holdout'}:raise ValueError('Both family partitions required')


def case_view(case,verbosity='base'):
    return dict(id=case['id'],language=case['language'],evidence=deepcopy(case['evidence']),question=case['question'],
                answer=case['answer'] if verbosity=='base' else case['expanded_answer'])


def review_view(dataset,*,order='forward',verbosity='base'):
    validate_dataset(dataset)
    if order not in ('forward','reverse') or verbosity not in ('base','expanded'):raise ValueError('Unknown presentation')
    cases=[case_view(c,verbosity) for c in dataset['cases']]
    if order=='reverse':cases.reverse()
    return dict(schema_version='1',dataset_hash=digest(dataset),rubric=deepcopy(RUBRIC),cases=cases)


def _ratings(dataset,artifacts,test_mode):
    records=[];ignored=0;seen=set();sources=[];cases={c['id']:c for c in dataset['cases']}
    for artifact in artifacts:
        versioned(artifact,('dataset_hash','origin','source','labels'),('assistance',))
        assistance=artifact.get('assistance','unknown')
        if assistance not in ('none','ai_proposed','unknown'):raise ValueError('Unknown rating assistance')
        if artifact['dataset_hash']!=digest(dataset) or artifact['origin'] not in ('human','synthetic_test') or not text(artifact['source']):raise ValueError('Rating provenance mismatch')
        if not isinstance(artifact['labels'],list) or len(artifact['labels'])>20000:raise ValueError('Invalid labels')
        if test_mode and artifact['origin']!='synthetic_test':raise ValueError('Test mode requires synthetic origin')
        for row in artifact['labels']:
            exact(row,('case_id','case_hash','annotator','support','helpfulness','note'))
            if not text(row['case_id']) or row['case_id'] not in cases or row['case_hash']!=digest(case_view(cases[row['case_id']])) or not text(row['annotator']) or not isinstance(row['note'],str):raise ValueError('Label case identity mismatch')
            if any(row[axis] not in values for axis,values in AXES.items()):raise ValueError('Unknown rating')
            identity=(artifact['origin'],row['annotator'],row['case_id'])
            if identity in seen:raise ValueError('Duplicate annotator/case; never inflate counts')
            seen.add(identity)
            if not test_mode and artifact['origin']=='synthetic_test':ignored+=1
            else:records.append(dict(deepcopy(row),assistance=assistance))
        sources.append(dict(origin=artifact['origin'],source=artifact['source'],assistance=assistance,artifact_hash=digest(artifact)))
    return records,ignored,sources


def _predictions(dataset,artifacts,test_mode):
    result=[];ignored=0;seen=set();runs=set();cases={c['id'] for c in dataset['cases']}
    for artifact in artifacts:
        versioned(artifact,('dataset_hash','origin','source','run_id','judge_id','model_id','control_group','blind','order','verbosity','view_hash','predictions'))
        if artifact['dataset_hash']!=digest(dataset) or artifact['origin'] not in ('external_model','synthetic_test') or type(artifact['blind']) is not bool:raise ValueError('Judge provenance mismatch')
        if not all(text(artifact[k]) for k in ('source','run_id','judge_id','model_id','control_group')):raise ValueError('Judge identity required')
        if test_mode and artifact['origin']!='synthetic_test':raise ValueError('Test mode requires synthetic judge')
        if artifact['view_hash']!=digest(review_view(dataset,order=artifact['order'],verbosity=artifact['verbosity'])):raise ValueError('Judge input hash mismatch')
        key=(artifact['origin'],artifact['judge_id'],artifact['model_id'],artifact['control_group'],artifact['order'],artifact['verbosity'])
        run=(artifact['origin'],artifact['run_id'])
        if key in seen or run in runs:raise ValueError('Duplicate judge condition/run')
        seen.add(key);runs.add(run)
        if not isinstance(artifact['predictions'],list) or len(artifact['predictions'])>len(cases):raise ValueError('Invalid predictions')
        ids=set()
        for row in artifact['predictions']:
            exact(row,('case_id','support','helpfulness'))
            if not text(row['case_id']) or row['case_id'] not in cases or row['case_id'] in ids or any(row[axis] not in values for axis,values in AXES.items()):raise ValueError('Invalid/duplicate predicted case')
            ids.add(row['case_id'])
        if not test_mode and artifact['origin']=='synthetic_test':ignored+=1
        else:result.append(deepcopy(artifact))
    return result,ignored


def analyze_reviews(dataset,ratings=None,predictions=None,*,test_mode=False):
    dataset=deepcopy(dataset);validate_dataset(dataset)
    if type(test_mode) is not bool or (ratings is not None and not isinstance(ratings,list)) or (predictions is not None and not isinstance(predictions,list)):raise ValueError('Explicit artifact lists/mode required')
    records,ignored_rating_rows,sources=_ratings(dataset,ratings or [],test_mode)
    batches,ignored_prediction_batches=_predictions(dataset,predictions or [],test_mode)
    annotators={r['annotator'] for r in records};case_rows={c['id']:[r for r in records if r['case_id']==c['id']] for c in dataset['cases']}
    reference={};agreement={};unknown_labels={};disputed={}
    for axis,values in AXES.items():
        reference[axis]={};agree=disagree=unknown_pairs=0;unknown_labels[axis]=sum(r[axis]=='unclear' for r in records);disputed[axis]=0
        for c in dataset['cases']:
            rows=case_rows[c['id']];counts=Counter(r[axis] for r in rows);known=sum(n for k,n in counts.items() if k!='unclear')
            pairs=known*(known-1)//2;same=sum(n*(n-1)//2 for k,n in counts.items() if k!='unclear')
            agree+=same;disagree+=pairs-same;unknown_pairs+=len(rows)*(len(rows)-1)//2-pairs
            if len({r[axis] for r in rows if r[axis]!='unclear'})>1:disputed[axis]+=1
            label=next(iter(counts)) if rows and len(counts)==1 and 'unclear' not in counts else None
            reference[axis][c['id']]=dict(label=label,annotators=len(rows))
        agreement[axis]=dict(known_pairs=agree+disagree,agree_pairs=agree,disagree_pairs=disagree,unknown_pairs=unknown_pairs,
            agreement_rate=agree/(agree+disagree) if agree+disagree else None)
    split_summary={};reference_summary={};volume_met=True
    for split in ('calibration','holdout'):
        selected=[c for c in dataset['cases'] if c['split']==split]
        split_summary[split]=dict(cases=len(selected),families=len({c['family'] for c in selected}))
        reference_summary[split]={}
        for axis in AXES:
            known=[c for c in selected if reference[axis][c['id']]['label'] is not None]
            qualified=[c for c in known if reference[axis][c['id']]['annotators']>=dataset['thresholds']['min_annotators']]
            met=len(qualified)>=dataset['thresholds']['min_known_per_split'] and len({c['family'] for c in qualified})>=dataset['thresholds']['min_families_per_split']
            volume_met=volume_met and met
            reference_summary[split][axis]=dict(known_reference_cases=len(known),unknown_or_unrated_cases=len(selected)-len(known),
                cases_with_min_annotators=len(qualified),qualified_families=len({c['family'] for c in qualified}),volume_screen_met=met)
    comparisons=[]
    for batch in batches:
        by_id={p['case_id']:p for p in batch['predictions']};metrics={}
        for split in ('calibration','holdout'):
            metrics[split]={}
            for axis,values in AXES.items():
                ids=[c['id'] for c in dataset['cases'] if c['split']==split and reference[axis][c['id']]['label'] is not None]
                matrix={known:{p:0 for p in values} for known in values if known!='unclear'}
                paired=correct=abstain=0
                for cid in ids:
                    if cid not in by_id:continue
                    human=reference[axis][cid]['label'];pred=by_id[cid][axis];matrix[human][pred]+=1;paired+=1
                    correct+=human==pred;abstain+=pred=='unclear'
                metrics[split][axis]=dict(confusion=matrix,known_reference_cases=len(ids),paired=paired,missing_predictions=len(ids)-paired,
                    unclear_predictions=abstain,determinate_pairs=paired-abstain,
                    accuracy_on_determinate=correct/(paired-abstain) if paired>abstain else None,
                    accuracy_including_abstention=correct/paired if paired else None,
                    determinate_coverage=(paired-abstain)/len(ids) if ids else None)
        comparisons.append(dict(run_id=batch['run_id'],judge_id=batch['judge_id'],model_id=batch['model_id'],origin=batch['origin'],
            declared_blind=batch['blind'],order=batch['order'],verbosity=batch['verbosity'],input_hash=batch['view_hash'],
            prediction_hash=digest(batch),source=batch['source'],metrics=metrics))
    controls=[]
    for i,left in enumerate(batches):
        for right in batches[i+1:]:
            if any(left[k]!=right[k] for k in ('judge_id','model_id','control_group')):continue
            kind='order' if left['verbosity']==right['verbosity'] and left['order']!=right['order'] else 'verbosity' if left['order']==right['order'] and left['verbosity']!=right['verbosity'] else None
            if kind is None:continue
            a={p['case_id']:p for p in left['predictions']};b={p['case_id']:p for p in right['predictions']};ids=a.keys()&b.keys()
            controls.append(dict(kind=kind,control_group=left['control_group'],runs=[left['run_id'],right['run_id']],paired_cases=len(ids),
                unmatched_cases=len(a.keys()^b.keys()),flips={axis:sum(a[c][axis]!=b[c][axis] for c in ids) for axis in AXES},
                interpretation='Descriptive differences; verbosity can legitimately affect helpfulness. No causal bias proof.'))
    rated=sum(bool(case_rows[c['id']]) for c in dataset['cases'])
    return dict(schema_version='1',rubric_version=RUBRIC_VERSION,dataset_hash=digest(dataset),dataset_version=dataset['version'],
        status='synthetic_test_only' if test_mode else 'awaiting_human_labels' if not records else 'pilot',test_mode=test_mode,
        actual_human_annotators=0 if test_mode else len(annotators),analysis_annotators=len(annotators),included_rating_rows=len(records),
        human_assistance={kind:dict(label_rows=0 if test_mode else sum(r['assistance']==kind for r in records),
            annotators=0 if test_mode else len({r['annotator'] for r in records if r['assistance']==kind})) for kind in ('none','ai_proposed','unknown')},
        ignored_fixture_rating_rows=ignored_rating_rows,ignored_fixture_prediction_batches=ignored_prediction_batches,
        rated_cases=rated,unrated_cases=len(dataset['cases'])-rated,unknown_labels=unknown_labels,disagreement_cases=disputed,agreement=agreement,
        split_summary=split_summary,reference_summary=reference_summary,thresholds=deepcopy(dataset['thresholds']),rating_sources=sources,
        judge=dict(status='synthetic_test_only' if test_mode else 'not_run' if not batches else 'exploratory',calibrated=False,
            imported_batches=len(batches),model_requests_made_by_tool=0,volume_screen_met=volume_met,
            nonblind_batches=sum(not b['blind'] for b in batches),missing_controls=[k for k in ('order','verbosity') if not any(c['kind']==k for c in controls)]),
        comparisons=comparisons,controls=controls,limitations=[
            'Origins, annotator identities and blindness are operator attestations; input hashes do not authenticate provenance.',
            'Assistance is declared provenance, not an independence proof; annotators can appear in multiple assistance groups. AI-proposed confirmation is not unaided annotation.',
            'Single-rater references are provisional; disagreement/unclear cases excluded with explicit denominators.',
            'Volume thresholds are a declared screening rule, not a statistical guarantee; no automatic calibrated/release status.',
            'This tool imports predictions only and cannot issue provider/model requests.',
            'Exact banking amounts, ownership and mandatory safety remain the deterministic grader authority.'])
