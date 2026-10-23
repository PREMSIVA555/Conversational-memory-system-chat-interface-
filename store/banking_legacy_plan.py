"""Read-only classification; never guesses customer ownership or edits legacy rows."""
from collections import Counter


def classify(rows):
    counts = Counter()
    ambiguities = []
    for index, row in enumerate(rows):
        key, value = row.get('key'), row.get('value')
        allowed = {'preferred_response_language': {'en', 'hi'},
                   'preferred_response_format': {'concise', 'detailed'}}
        if not row.get('customer_id'):
            reason = 'missing_customer_mapping'
        elif not row.get('source_user_event_id') or not isinstance(row.get('sequence'), int) or row['sequence'] <= 0:
            reason = 'missing_accepted_source_order'
        elif key not in allowed or not isinstance(value, str) or value not in allowed[key]:
            reason = 'unsupported_key_or_value'
        elif row.get('scope_key') != 'global':
            reason = 'scope_requires_review'
        else:
            reason = 'candidate_requires_owner_and_source_validation'
        counts[reason] += 1
        ambiguities.append({'row_index': index, 'reason': reason})
    return {'mode': 'dry_run', 'input_rows': sum(counts.values()), 'classified_rows': sum(counts.values()),
            'counts': dict(sorted(counts.items())), 'review_rows': ambiguities,
            'writes': 0, 'automatic_backfill': False}
