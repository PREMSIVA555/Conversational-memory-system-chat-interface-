"""Privileged cutover operations. No HTTP mapping/write-control surface.

Plans contain identifiers and hashes, never legacy content. An owner link is an
explicit operator attestation, not a relationship inferred by this module.
"""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

KEYS={'preferred_response_language':{'en','hi'},'preferred_response_format':{'concise','detailed'}}

def digest(value):
    return sha256(json.dumps(value,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()

def normalize(value):
    return value.strip().lower().replace(' ','_').replace('-','_') if isinstance(value,str) else None

def inventory(legacy):
    """Read only. No owner, value or accepted order is guessed from legacy text."""
    if legacy.execute("SELECT to_regclass('public.memories')").fetchone()[0] is None:
        raise ValueError('Legacy table unavailable')
    records=legacy.execute('SELECT to_jsonb(m) FROM public.memories m ORDER BY id').fetchall()
    hashes=[]; rows=[]
    for (record,) in records:
        fingerprint=digest(record); hashes.append(fingerprint)
        key=normalize(record.get('attribute'))
        if key in KEYS:
            rows.append(dict(legacy_id=record['id'],legacy_subject_id=record['subject_id'],
                key=key,source=record.get('source'),deleted=record.get('deleted_at') is not None,
                superseded=record.get('superseded_at') is not None,fingerprint=fingerprint))
    return dict(database=legacy.execute('SELECT current_database()').fetchone()[0],
        system_identifier=legacy.execute('SELECT system_identifier::text FROM pg_control_system()').fetchone()[0],
        total_rows=len(records),protected_rows=len(rows),snapshot_sha256=digest(hashes),rows=rows,
        content_exported=False,ownership_inferred=False,accepted_order_inferred=False)

def install_guard(legacy,expected_snapshot,backup_path):
    """Exclusive table lock also drains old DML before the DB guard is installed."""
    legacy.execute('LOCK TABLE public.memories IN ACCESS EXCLUSIVE MODE')
    before=inventory(legacy)
    if before['snapshot_sha256']!=expected_snapshot:
        raise ValueError('Legacy snapshot changed; review a fresh inventory')
    path=Path(backup_path)
    if path.exists(): raise ValueError('Backup destination already exists')
    path.parent.mkdir(parents=True,exist_ok=True)
    raw=[r[0] for r in legacy.execute('SELECT to_jsonb(m) FROM public.memories m ORDER BY id').fetchall()]
    # Private local recovery artifact only. The caller must choose private storage.
    with path.open('x',encoding='utf-8') as f: json.dump(raw,f,default=str)
    legacy.execute((Path(__file__).parent/'legacy_cutover_guard.sql').read_text())
    after=inventory(legacy)
    if before!=after: raise RuntimeError('Guard changed legacy rows')
    return dict(status='installed',table='public.memories',before=before,after=after,
        existing_rows_unchanged=True,backup_sha256=sha256(path.read_bytes()).hexdigest(),
        backup_scope='private_local_legacy_rows_not_full_database',
        legacy_preference_writer='permanently_blocked')

def record_guard(bank,report):
    identity={k:report['after'][k] for k in ('database','system_identifier','snapshot_sha256')}
    identity.update(status='installed',guard_sha256=sha256((Path(__file__).parent/'legacy_cutover_guard.sql').read_bytes()).hexdigest(),
                    authority='privileged_operator_installation_attestation')
    bank.execute('SELECT banking.record_legacy_guard(%s)',(Jsonb(identity),))
    return identity

def build_plan(legacy,bank,mappings):
    snapshot=inventory(legacy)
    if not isinstance(mappings,list): raise ValueError('Mappings must be a list')
    by_id={}
    for mapping in mappings:
        if set(mapping)!={'legacy_id','customer_id','source_id','value'}:
            raise ValueError('Unexpected mapping fields')
        if str(mapping['legacy_id']) in by_id: raise ValueError('Duplicate legacy mapping')
        by_id[str(mapping['legacy_id'])]=mapping
    known={str(r['legacy_id']) for r in snapshot['rows']}
    if set(by_id)-known: raise ValueError('Mapping references a missing or unsupported legacy row')
    classified=[]; counts=Counter()
    for row in snapshot['rows']:
        item=dict(row); mapping=by_id.get(str(row['legacy_id']))
        reason='missing_reviewed_owner_and_accepted_source'
        if row['deleted'] or row['superseded']: reason='retired_legacy_row_not_backfilled'
        elif row['source'] not in ('user_statement','user_preference'): reason='legacy_user_provenance_unverified'
        elif mapping:
            customer=str(mapping['customer_id']); source=str(mapping['source_id'])
            bank.execute("SELECT set_config('bank.customer_id',%s,true)",(customer,))
            owner=bank.execute('SELECT customer_id FROM banking.legacy_owner_links WHERE legacy_subject_id=%s',(row['legacy_subject_id'],)).fetchone()
            accepted=bank.execute('SELECT actor_ref,sequence,payload FROM banking.accepted_user_events WHERE id=%s AND customer_id=%s',(source,customer)).fetchone()
            proposal=bank.execute('SELECT proposals FROM banking.preference_proposals WHERE source_id=%s AND customer_id=%s',(source,customer)).fetchone()
            erased=bank.execute('SELECT 1 FROM banking.erasure_ledger WHERE source_id=%s',(source,)).fetchone()
            expected=dict(key=row['key'],value=mapping['value'],operation='SET',scope_key='global')
            if not owner or str(owner[0])!=customer: reason='missing_reviewed_owner_link'
            elif erased: reason='accepted_source_erased'
            elif not accepted or accepted[0]!='user' or accepted[1]<=0: reason='accepted_source_owner_mismatch'
            elif mapping['value'] not in KEYS[row['key']]: reason='unsupported_typed_value'
            elif not proposal or expected not in proposal[0]: reason='accepted_typed_source_mismatch'
            else:
                reason='validated_candidate_requires_plan_review'
                item.update(customer_id=customer,source_id=source,value=mapping['value'])
        item['classification']=reason; classified.append(item); counts[reason]+=1
    return dict(mode='dry_run',snapshot={k:v for k,v in snapshot.items() if k!='rows'},
        input_rows=snapshot['protected_rows'],classified_rows=len(classified),
        counts=dict(sorted(counts.items())),rows=classified,writes=0,
        automatic_backfill=False,owner_link_authority='explicit_privileged_operator_attestation',
        source_authority='existing_accepted_user_event_and_exact_typed_proposal')

def apply_plan(legacy,bank,mappings,reviewed_plan_sha256):
    legacy.execute('LOCK TABLE public.memories IN SHARE MODE')
    installed=legacy.execute("SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='public.memories'::regclass AND tgname='banking_preference_writer_retired' AND tgenabled='O')").fetchone()[0]
    if not installed: raise ValueError('Legacy DB writer guard required before backfill')
    plan=build_plan(legacy,bank,mappings)
    if digest(plan)!=reviewed_plan_sha256: raise ValueError('Reviewed plan hash mismatch')
    results=[]
    for row in plan['rows']:
        if row['classification']!='validated_candidate_requires_plan_review': continue
        bank.execute("SELECT set_config('bank.customer_id',%s,true)",(row['customer_id'],))
        outcome=bank.execute('SELECT banking.backfill_legacy_preference(%s,%s,%s,%s,%s,%s,%s)',
            (row['legacy_id'],row['legacy_subject_id'],row['source_id'],row['key'],row['value'],
             plan['snapshot']['snapshot_sha256'],reviewed_plan_sha256)).fetchone()[0]
        results.append(dict(legacy_id=row['legacy_id'],outcome=outcome))
    return dict(mode='reviewed_backfill',reviewed_plan_sha256=reviewed_plan_sha256,
        classified_rows=plan['classified_rows'],attempted_rows=len(results),
        skipped_rows=plan['classified_rows']-len(results),results=results,
        accepted_events_created=0,legacy_rows_modified=0)
