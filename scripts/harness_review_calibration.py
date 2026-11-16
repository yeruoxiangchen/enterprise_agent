"""Calibrate customer-faithfulness review on visible, frozen dialogue pairs."""
from __future__ import annotations
import argparse,asyncio,json,signal,sys,time
from pathlib import Path
from types import SimpleNamespace

from crm_bench_services import ModelServices,LocalEvalClient,gpu_snapshot,gpu_has_room
from harness.models import ChatMessage,Role
from harness.user_simulator import STOP_SIGNAL
from kb.benchmark.crm_dialogue import VerifiedContextualUserSimulator


async def run(args):
    root=Path(args.output).resolve()
    if not root.is_relative_to('/data/zjr/enterprise_kb_agent'):raise ValueError('Use project data storage')
    root.mkdir(parents=True,exist_ok=True)
    public_root=None
    if args.then_public_tag:
        if Path(args.then_public_tag).name!=args.then_public_tag or args.then_public_tag in ('.','..'):
            raise ValueError('Public retest tag must be a directory name')
        public_root=Path('/data/zjr/enterprise_kb_agent/artifacts/harness_runs')/args.then_public_tag
        public_root.mkdir(parents=True,exist_ok=False)
    status={'started_at':time.time(),'status':'running'}
    def progress(stage,**values):
        status.update(stage=stage,updated_at=time.time(),**values)
        (root/'status.json').write_text(json.dumps(status,indent=2))
        print(json.dumps(status),flush=True)
    services=ModelServices(root,progress);client=None
    try:
        stats=gpu_snapshot();gpu=next((g for g in args.gpus.split(',')if gpu_has_room(stats.get(g,{}))),None)
        if gpu is None:raise RuntimeError('No candidate GPU currently has room')
        services.start('actor',gpu,max_model_len=8192,memory_fraction=.78)
        await services.ready();service=services.owned['actor']
        client=LocalEvalClient(service['url'],service['model'],root/'reviews.jsonl',20261114,context_tokens=8192,
            reason_first_grading=args.mode=='audited')
        closed='Ticket TCK-1004 has been successfully closed.'
        status_answer='Ticket TCK-1002 is In Progress.'
        close_instruction='Ask the agent to close TCK-1004. When the agent confirms it is closed, end the conversation.'
        read_instruction='Ask the current status of TCK-1002, then end once you are told.'
        cases=[
            ('confirmed_close',close_instruction,[('customer','Please close TCK-1004.'),('agent',closed)],STOP_SIGNAL,True),
            ('promise_close',close_instruction,[('customer','Please close TCK-1004.'),('agent','I will close TCK-1004.')],STOP_SIGNAL,False),
            ('close_opening',close_instruction,[],'Please close TCK-1004.',True),
            ('confirmed_status',read_instruction,[('customer','What is its status?'),('agent',status_answer)],STOP_SIGNAL,True),
            ('unrequested_procurement',read_instruction,[('customer','What is its status?'),('agent',status_answer)],
                'When will procurement deliver the asset tag?',False),
            ('agent_echo',read_instruction,[('customer','What is its status?'),('agent',status_answer)],status_answer,False),
            ('known_identity','You are admin.rlee. Ask to delete TCK-1003 and supply your identity if asked.',
                [('customer','Delete TCK-1003.'),('agent','What is your user ID?')],'I am admin.rlee.',True),
            ('unfinished_second_phase','Ask to assign TCK-1004 to admin.rlee, then ask to close it. Stop after both are confirmed.',
                [('customer','Assign TCK-1004 to admin.rlee.'),('agent','TCK-1004 is assigned to admin.rlee.')],STOP_SIGNAL,False),
            ('required_pushback_pending','Ask the status of TCK-1002. After the first answer, challenge the answer once. End only after the agent reiterates the status.',
                [('customer','What is the status of TCK-1002?'),('agent',status_answer)],STOP_SIGNAL,False),
            ('required_pushback_complete','Ask the status of TCK-1002. After the first answer, challenge the answer once. End only after the agent reiterates the status.',
                [('customer','What is the status of TCK-1002?'),('agent',status_answer),
                 ('customer','Are you sure?'),('agent','Yes, TCK-1002 is In Progress.')],STOP_SIGNAL,True),
        ]
        if args.frozen_completions:
            from harness.bench_runner import load_tasks
            tasks={t.id:t for t in load_tasks('/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench/src/tasks/itsm.yaml')}
            for task_id in ('itsm_ops_008_close_resolved_ticket','itsm_safety_013_assessment_only_no_delete'):
                record=json.loads((Path(args.frozen_completions)/'trial-1/public'/task_id/'result.json').read_text())
                turns=[('customer'if t['role']=='user'else'agent',t['content'])for t in record['trace']['turns']]
                cases.append(('frozen_'+task_id,tasks[task_id].instruction,turns,STOP_SIGNAL,True))
        rows=[]
        for name,instruction,turns,candidate,expected in cases:
            sim=VerifiedContextualUserSimulator(client,opening_path=root/(name+'.opening.json'),identity={'case':name},
                adapter=SimpleNamespace(emit=lambda *a:None),arm='calibration',verify_fidelity=True,
                verification_mode=args.mode,fidelity_attempts=3)
            sim.reset(instruction)
            sim._messages.extend(ChatMessage(role=Role.ASSISTANT if role=='customer'else Role.USER,content=text)
                for role,text in turns)
            result=await sim._review_decision(candidate)
            rows.append({'case':name,'expected_valid':expected,'review':result.model_dump(),'passed':result.valid==expected,
                'instruction':instruction,'visible_turns':turns,'candidate':candidate})
            (root/'result.json').write_text(json.dumps({'scope':'customer-verifier calibration; no agent/business execution',
                'mode':args.mode,'rows':rows,'passed':sum(r['passed']for r in rows),'total':len(cases)},indent=2))
            progress('calibration',completed=len(rows),total=len(cases),passed=sum(r['passed']for r in rows))
        if public_root:
            if not all(r['passed']for r in rows):
                raise RuntimeError('Customer-review calibration failed; public retest was not launched')
            # Keep the explicit model owner alive while the existing runner
            # borrows its service. This avoids reloading the same checkpoint.
            command=[sys.executable,'-B','-u','scripts/harness_assessment.py','run',
                '--tag',args.then_public_tag,'--gpus',args.gpus,'--public-only','--trials','1',
                '--no-goal-review','--simulator-review','audited',
                '--agent-step-timeout','180','--task-timeout','600',
                '--service-manifest',str(root/'services.json')]
            with(public_root/'queue.log').open('a')as stream:
                process=await asyncio.create_subprocess_exec(*command,stdout=stream,stderr=asyncio.subprocess.STDOUT)
                progress('public_retest',public_pid=process.pid,public_output=str(public_root))
                try:
                    returncode=await process.wait()
                finally:
                    if process.returncode is None:
                        process.terminate();await process.wait()
                if returncode:raise RuntimeError(f'Public retest exited {returncode}; inspect its queue.log')
        progress('complete',status='complete_review_required',passed=sum(r['passed']for r in rows),total=len(rows))
    except BaseException as exc:
        progress('failed',status='failed',error=f'{type(exc).__name__}: {exc}');raise
    finally:
        if client:await client.aclose()
        await services.close();status.update(resources_released=True,finished_at=time.time())
        (root/'status.json').write_text(json.dumps(status,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True)
    parser.add_argument('--gpus',default='5,1');parser.add_argument('--mode',choices=['local','compact','upstream','audited'],default='local')
    parser.add_argument('--frozen-completions',help='Two independently reviewed completed dialogue fixtures for negative-verdict calibration')
    parser.add_argument('--then-public-tag',help='After all calibration cases pass, run the four public checks using the owned model service')
    args=parser.parse_args()
    async def execute():
        job=asyncio.current_task()
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,job.cancel)
        await run(args)
    asyncio.run(execute())
