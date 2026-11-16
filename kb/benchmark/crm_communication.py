"""Additional observed-dialogue grading with the unchanged upstream rubric.

The original task-instruction grade is retained as the native result. This audit
frames the actual customer requests as context so simulated-user imperatives do
not become additional obligations for the service assistant.
"""
import json


async def review_observed_dialogue(judge,task,trace):
    criteria=task.expected_response_semantic
    if not criteria or trace.error or trace.timed_out:return None
    turns=[{'role':turn.role.value if hasattr(turn.role,'value')else turn.role,'content':turn.content}
           if not isinstance(turn,dict)else turn for turn in trace.turns]
    context={'service_role':'assistant','customer_role':'user','customer_requests':[
        turn['content']for turn in turns if turn['role']=='user']}
    outcome=await judge.grade(task_id=task.id,task_instruction=json.dumps(context,ensure_ascii=False),
        criteria=list(criteria),turns=turns)
    return {'scope':'additional observed-dialogue communication audit; native score preserved',
        'passed':outcome.passed,'failures':outcome.failures,'audit':outcome.audit,
        'criteria':list(criteria),'context':context}
