import argparse
from dataclasses import asdict
from pathlib import Path
import sys
from .adapters import CommandAdapter, ReplayAdapter
from .contracts import Task, canonical, parse_json
from .runner import run


def main(argv=None):
    p = argparse.ArgumentParser(description='Synthetic evaluator; projection grades are unvalidated smoke checks')
    p.add_argument('--task',type=Path)
    p.add_argument('--demo',action='store_true')
    p.add_argument('--replay',type=Path)
    p.add_argument('--command-json',help='Explicit trusted argv array; no shell')
    p.add_argument('--timeout',type=float,default=60)
    p.add_argument('--output',type=Path,required=True)
    args = p.parse_args(argv)
    if bool(args.task) == bool(args.demo): p.error('Choose --task or --demo')
    if args.replay and args.command_json: p.error('Choose one adapter')
    if args.output.exists(): p.error('Evidence output already exists; use a new path')
    try:
        task = Task.parse(parse_json(args.task.read_text(encoding='utf-8'))) if args.task else Task.parse(dict(
            schema_version='1',id='synthetic-counter-v1',steps=[{'op':'increment'},{'op':'read'},{'op':'increment'}],
            projection={'count':['count']},expected={'count':2}))
        if args.replay: adapter = ReplayAdapter(args.replay)
        else:
            if not args.demo and not args.command_json: p.error('--task requires --command-json or --replay')
            command = parse_json(args.command_json) if args.command_json else [sys.executable,'-m','banking_evaluator.demo_operator']
            if not isinstance(command,list): raise ValueError('argv array required')
            adapter = CommandAdapter(command,timeout=args.timeout)
        report = run(task,adapter).to_dict()
    except (ValueError,TypeError,OSError) as exc:
        # Invalid input still leaves a retained, explicitly ungraded record.
        report = {'schema_version':'1','grade':{'status':'invalid','reason':'Invalid task/configuration'},'error':type(exc).__name__}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as f: f.write(canonical(report)+'\n')
    print(canonical({'status':report['grade']['status'],'output':str(args.output)}))
    return {'pass':0,'fail':1,'invalid':2,'unknown':2}[report['grade']['status']]


if __name__ == '__main__': raise SystemExit(main())
