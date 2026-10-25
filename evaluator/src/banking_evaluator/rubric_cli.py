import argparse
from pathlib import Path
from .contracts import canonical,parse_json
from .rubric import review_view,analyze_reviews


def main(argv=None):
    p=argparse.ArgumentParser(description='Human rubric pilot; imports predictions, never calls a model')
    p.add_argument('action',choices=('view','analyze'));p.add_argument('--dataset',type=Path,required=True)
    p.add_argument('--ratings',type=Path,action='append',default=[]);p.add_argument('--predictions',type=Path,action='append',default=[])
    p.add_argument('--order',choices=('forward','reverse'),default='forward');p.add_argument('--verbosity',choices=('base','expanded'),default='base')
    p.add_argument('--test-mode',action='store_true');p.add_argument('--output',type=Path,required=True);a=p.parse_args(argv)
    if a.output.exists():p.error('Evidence exists; choose a new output')
    try:
        dataset=parse_json(a.dataset.read_text(encoding='utf-8'))
        if a.action=='view':
            if a.ratings or a.predictions or a.test_mode:p.error('View never accepts ratings/predictions')
            result=review_view(dataset,order=a.order,verbosity=a.verbosity)
        else:result=analyze_reviews(dataset,[parse_json(p.read_text(encoding='utf-8')) for p in a.ratings],
            [parse_json(p.read_text(encoding='utf-8')) for p in a.predictions],test_mode=a.test_mode)
        exit_code=0
    except (ValueError,TypeError,KeyError,OSError):result={'schema_version':'1','status':'invalid','error':'invalid_review_input'};exit_code=2
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('x',encoding='utf-8') as f:f.write(canonical(result)+'\n')
    print(canonical({'status':result.get('status','view_exported'),'output':str(a.output)}));return exit_code


if __name__=='__main__':raise SystemExit(main())
