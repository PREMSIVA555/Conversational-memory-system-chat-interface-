"""T05.3 reconciliation gate; no paid calls or ordinary demo reset."""
import hashlib,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    os.chdir(ROOT)
    os.environ.setdefault('PYTHONUTF8','1')
    os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP','True')
    interpreter=ROOT/'.genesis/local/ux-python.txt'
    if interpreter.exists() and Path(interpreter.read_text(encoding='utf-8').strip()).resolve()!=Path(sys.executable).resolve():
        return subprocess.call([interpreter.read_text(encoding='utf-8').strip(),str(Path(__file__).resolve())])
    expected=json.loads((ROOT/'.genesis/sources/P05_SOURCE.json').read_text(encoding='utf-8'))['manifest']['preserved_migration_sha256']
    if hashlib.sha256((ROOT/'store/banking_migrations/0028_support_proposals.sql').read_bytes()).hexdigest()!=expected:
        raise RuntimeError('Preserved T05.1 migration bytes changed')
    code=subprocess.call([sys.executable,'-m','pytest','tests/p05','tests/ux','tests/p04','tests/p03/test_account_slice.py',
                          '-q','--junitxml=.genesis/evidence/T05.3-regression.xml'])
    if code:return code
    ui=ROOT/'frontend';alternate=ROOT/'.genesis/local/ux-ui'
    if alternate.exists():
        for folder in ('app','components','lib','public'):
            for source in (ui/folder).rglob('*'):
                if source.is_file() and (not (alternate/source.relative_to(ui)).exists() or source.read_bytes()!=(alternate/source.relative_to(ui)).read_bytes()):
                    raise RuntimeError('Review UI differs from source: '+str(source.relative_to(ui)))
        for name in ('package.json','package-lock.json','tsconfig.json','next.config.ts'):
            if (ui/name).read_bytes()!=(alternate/name).read_bytes():raise RuntimeError('Review UI config differs: '+name)
        ui=alternate
    code=subprocess.call(['npm.cmd' if os.name=='nt' else 'npm','run','typecheck'],cwd=ui)
    print(json.dumps({'gate':'T05.3 reconciliation','pytest_exit':0,'typescript_exit':code,'paid_calls':0,'human_review':'pending'}))
    return code

if __name__=='__main__':raise SystemExit(main())
