"""Tiny synthetic counter, not a banking adapter or authorization test."""
from pathlib import Path
import platform
import sys
from .contracts import canonical, digest, parse_json


def main():
    body = parse_json(sys.stdin.read())
    count = 0
    results = []
    for step in body['steps']:
        if step['op'] == 'increment': count += 1
        elif step['op'] != 'read': raise ValueError('Unknown synthetic operation')
        results.append({'count':count})
    print(canonical(dict(schema_version='1',results=results,oracle={'count':count},
        identities=dict(source=digest(Path(__file__).read_text(encoding='utf-8')),data=digest({'seed':0}),
                        model='not-used',tools='synthetic-counter/1',policy='not-applicable',environment=platform.python_version()),
        reset=dict(namespace='synthetic:'+body['run_id'],semantic_hash=digest({'count':0}),kind='new-in-process-counter'))))


if __name__ == '__main__': main()
