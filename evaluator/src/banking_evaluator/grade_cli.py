import argparse
from pathlib import Path
from .contracts import canonical,parse_json
from .grading import grade


def main(argv=None):
    p=argparse.ArgumentParser(description='Bounded normalized banking outcome/process grader')
    p.add_argument('--case',type=Path,required=True);p.add_argument('--observation',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args(argv)
    if a.output.exists():p.error('Evidence exists; choose a new output')
    try:
        case=parse_json(a.case.read_text(encoding='utf-8'));observation=parse_json(a.observation.read_text(encoding='utf-8'))
        result=grade(case,observation);report={'schema_version':'1','case':case,'observation':observation,'grade':result}
    except (ValueError,TypeError,OSError):report={'schema_version':'1','grade':{'status':'invalid','error':'invalid_input'}}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('x',encoding='utf-8') as f:f.write(canonical(report)+'\n')
    print(canonical({'status':report['grade']['status'],'output':str(a.output)}))
    return {'pass':0,'fail':1,'invalid':2,'unknown':2}[report['grade']['status']]


if __name__=='__main__':raise SystemExit(main())
