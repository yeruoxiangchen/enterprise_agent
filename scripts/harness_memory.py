"""Reusable offline workflow-learning CLI; never imports benchmark gold into memory."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_openai import ChatOpenAI
import yaml

from kb.harness.memory import WorkflowStore,export_episode
from kb.harness.ports import CRMPort,ApplicationPort,WRITES

ROOT=Path('/data/zjr/enterprise_kb_agent')


def export_public_row(row,ident):
    """Only visible messages/effects and scalar feedback cross this boundary."""
    trace=row['trace'];effects=[]
    for turn in trace['turns']:
        for call in turn.get('tool_calls',[]):
            if not call.get('backend_executed'):continue
            raw=call.get('result')
            try:value=json.loads(raw)if isinstance(raw,str)else raw
            except ValueError:value={'text':raw}
            outcome=call.get('business_outcome')
            status=outcome if outcome in ('committed','unknown') else 'refused' if (
                not call.get('ok')or isinstance(value,dict)and value.get('error'))else 'observed'
            if call['name']in WRITES and status=='observed':status='committed'
            effects.append({'name':call['name'],'arguments':call['arguments'],'status':status,'result':value})
    return export_episode(ident,trace['turns'],effects,{
        'completed':row.get('local_verdict')is True,'error':trace.get('error'),
        'tokens':row.get('agent_usage',{}).get('total_tokens',0)},split='development')


def data_path(value):
    path=Path(value).resolve()
    if not path.is_relative_to(ROOT):raise ValueError('Runtime inputs and outputs belong under /data/zjr/enterprise_kb_agent')
    return path


def main():
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=['export','induce','status','publish'])
    p.add_argument('--input');p.add_argument('--output');p.add_argument('--store')
    p.add_argument('--url');p.add_argument('--model',default='crm-qwen3-8b')
    p.add_argument('--limit',type=int,default=50)
    p.add_argument('--format',choices=['public','harness'],default='public')
    p.add_argument('--catalog',choices=['crm','application'],default='crm')
    a=p.parse_args()
    if a.action=='export':
        source=data_path(a.input);target=data_path(a.output);target.parent.mkdir(parents=True,exist_ok=True)
        episodes=[]
        if a.format=='public':
            for file in sorted(source.glob('*.json'))[:a.limit]:
                row=json.loads(file.read_text());ident='episode-'+hashlib.sha256(file.read_bytes()).hexdigest()[:20]
                episodes.append(export_public_row(row,ident))
        else:
            connection=sqlite3.connect(f'file:{source}/effects.sqlite3?mode=ro',uri=True)
            connection.row_factory=sqlite3.Row
            try:
                for row in connection.execute('SELECT * FROM requests LIMIT ?',(a.limit,)):
                    result=json.loads(row['result'])if row['result']else {}
                    effects=[{**dict(e),'arguments':json.loads(e['arguments']),'result':json.loads(e['result'])}
                        for e in connection.execute('SELECT * FROM effects WHERE request_id=?',(row['id'],))]
                    episodes.append(export_episode('episode-'+hashlib.sha256(row['id'].encode()).hexdigest()[:20],
                        [{'role':'user','content':row['question']},{'role':'assistant','content':result.get('answer','')}],
                        effects,{'completed':result.get('status')in ('completed','refused')}))
            finally:connection.close()
        target.write_text('\n'.join(json.dumps(e,ensure_ascii=False)for e in episodes)+'\n')
        print(json.dumps({'episodes':len(episodes),'output':str(target)}));return
    store=WorkflowStore(data_path(a.store))
    try:
        if a.action=='publish':
            if not store.entries('active'):raise ValueError('No independently validated active workflows to publish')
            destination=data_path(a.output);destination.parent.mkdir(parents=True,exist_ok=True)
            if destination==data_path(a.store):raise ValueError('Choose a separate release path')
            if destination.exists():raise ValueError('Releases are immutable; choose a new versioned path')
            temporary=destination.with_suffix('.release-tmp')
            target=sqlite3.connect(temporary)
            try:store.db.backup(target)
            finally:target.close()
            os.replace(temporary,destination)
            print(json.dumps({'release':str(destination),'fingerprint':store.fingerprint(),
                              'active_workflows':len(store.entries('active'))}));return
        if a.action=='status':
            print(json.dumps([{'id':e['id'],'revision':e['revision'],'status':e['status'],
                'title':e['card'].title}for e in store.entries()],ensure_ascii=False,indent=2));return
        if not a.url:p.error('--url is required for induction')
        usage=UsageMetadataCallbackHandler()
        model=ChatOpenAI(model=a.model,base_url=a.url,api_key='local',temperature=0,max_tokens=2200,
            timeout=120,max_retries=0,callbacks=[usage],
            extra_body={'chat_template_kwargs':{'enable_thinking':False}})
        episodes=[json.loads(line)for line in data_path(a.input).read_text().splitlines()if line.strip()][:a.limit]
        from harness.tools.environment import create_environment
        import harness.tools.domains
        benchmark=ROOT/'benchmarks/crm-agent-bench'
        port=CRMPort(create_environment('itsm'),yaml.safe_load((benchmark/'src/agents/itsm.yaml').read_text()),
                     (benchmark/'src/tasks/policies/itsm_policy.md').read_text())
        names={t.name:t.parameters for t in (port.tools()if a.catalog=='crm'else ApplicationPort.tools())}
        ids=[]
        for i in range(0,len(episodes),2):ids.extend(store.induce(model,episodes[i:i+2],names))
        report={'induced':list(dict.fromkeys(ids)),'usage':usage.usage_metadata,'episodes':len(episodes)}
        if a.output:data_path(a.output).write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:store.close()


if __name__=='__main__':main()
