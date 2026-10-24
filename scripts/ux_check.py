"""UX gate: guarded Docker worlds, scripted model boundary and UI type check.

No paid completions or normal-demo resets. Browser/manual/live evidence is
separate; a green exit does not claim human review or complete UX approval.
"""
import json,os,subprocess,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def main():
    os.chdir(ROOT)
    os.environ.setdefault('PYTHONUTF8','1')
    os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP','True')
    # Genesis may invoke a system Python; private runtime interpreter selection
    # is configuration, never a committed machine-specific executable path.
    interpreter=ROOT/'.genesis/local/ux-python.txt'
    if interpreter.exists():
        executable=Path(interpreter.read_text(encoding='utf-8').strip())
        if executable.resolve()!=Path(sys.executable).resolve():
            return subprocess.call([str(executable),str(Path(__file__).resolve()),*sys.argv[1:]])
    command=[sys.executable,'-m','pytest','tests/ux','tests/p04','tests/p03/test_account_slice.py',
             '-q','--junitxml=.genesis/evidence/UX01-regression.xml']
    code=subprocess.call(command)
    if code:return code
    # A private dependency install is allowed on Windows where the shared
    # checkout's node_modules junction cannot be bundled across drive letters.
    ui=ROOT/'frontend'
    alternate=ROOT/'.genesis/local/ux-ui'
    if alternate.exists():
        for folder in ('app','components','lib','public'):
            for source in (ui/folder).rglob('*'):
                if source.is_file():
                    target=alternate/source.relative_to(ui)
                    if not target.exists() or target.read_bytes()!=source.read_bytes():
                        raise RuntimeError('Private UI copy differs from tracked source: '+str(source.relative_to(ui)))
        for name in ('package.json','package-lock.json','tsconfig.json','next.config.ts'):
            if (ui/name).read_bytes()!=(alternate/name).read_bytes():raise RuntimeError('Private UI config mismatch: '+name)
        ui=alternate
    npm='npm.cmd' if os.name=='nt' else 'npm'
    code=subprocess.call([npm,'run','typecheck'],cwd=ui)
    print(json.dumps({'gate':'UX01','pytest_exit':0,'typecheck_exit':code,
                      'live_requests_dispatched':0,'human_review':'pending'}))
    return code

if __name__=='__main__':raise SystemExit(main())
