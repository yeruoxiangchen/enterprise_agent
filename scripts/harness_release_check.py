"""Check the published library through its actual online search, with fresh pairs.

Borrows an already running project model. Does not acquire or stop GPU services.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import importlib.metadata
from pathlib import Path
import shutil
import time

from harness_assessment import CaseRunner, ROOT, VALIDATION, write
from crm_bench_control import identity
from kb.harness.memory import WorkflowStore


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--service-manifest',required=True)
    p.add_argument('--store',required=True)
    p.add_argument('--output',required=True)
    args=p.parse_args()
    paths=[Path(v).resolve()for v in (args.service_manifest,args.store,args.output)]
    if any(not path.is_relative_to(ROOT)for path in paths):
        p.error('Runtime paths belong under /data/zjr/enterprise_kb_agent')
    manifest,release,folder=paths
    folder.mkdir(parents=True,exist_ok=True)
    with (folder/'started.json').open('x')as handle:
        json.dump({'started_at':time.time(),'release':str(release)},handle)
    service=json.loads(manifest.read_text())['actor']
    if service['released']or identity(service['pid'])!=service['start_ticks']:
        raise RuntimeError('The borrowed model service has exited')
    if not service['url'].startswith('http://127.0.0.1:'):
        raise ValueError('Use a local model service')
    source=Path(__file__).resolve().parents[1]
    sources=[*sorted((source/'kb/harness').glob('*.py')),Path(__file__),
             Path(__file__).with_name('harness_assessment.py'),source/'requirements.harness.lock']
    for file in sources:
        destination=folder/'source'/file.relative_to(source)
        destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(file,destination)
    write(folder/'protocol.json',{'scope':'three development variants; published two-card library; fresh paired execution',
        'release':str(release),'service_manifest':str(manifest),
        'dependencies':{name:importlib.metadata.version(name)for name in ('deepagents','langmem','langchain','langgraph')},
        'sources':{str(path.relative_to(source)):hashlib.sha256(path.read_bytes()).hexdigest()for path in sources}})
    def progress(stage,**values):
        write(folder/'status.json',{'stage':stage,'updated_at':time.time(),**values})
    library=WorkflowStore(release)
    runner=CaseRunner(folder,service,progress)
    try:
        pairs=[]
        for case in VALIDATION:
            before=runner.run_case(case)
            after=runner.run_case(case,library=library)
            pairs.append({'case':case['id'],'baseline':before,'memory':after})
        summary={'scope':'three development variants; no full-benchmark/generalization claim',
            'release_fingerprint':library.fingerprint(),'active_workflows':len(library.entries('active')),
            'baseline_passed':sum(pair['baseline']['passed']for pair in pairs),
            'memory_passed':sum(pair['memory']['passed']for pair in pairs),
            'regressions':sum(pair['baseline']['passed']and not pair['memory']['passed']for pair in pairs),
            'baseline_tokens':sum(pair['baseline']['tokens']for pair in pairs),
            'memory_tokens':sum(pair['memory']['tokens']for pair in pairs),
            'workflow_used_cases':sum(bool(pair['memory']['workflow_ids'])for pair in pairs),
            'policy_violations':sum(pair['memory']['policy_violations']for pair in pairs),
            'pairs':pairs}
        write(folder/'summary.json',summary)
        print(json.dumps({k:v for k,v in summary.items()if k!='pairs'}),flush=True)
        progress('complete_review_required',completed=len(runner.rows),total=2*len(VALIDATION))
    except BaseException as exc:
        progress('failed',error=f'{type(exc).__name__}: {exc}',completed=len(runner.rows))
        raise
    finally:
        library.close()


if __name__=='__main__':main()
