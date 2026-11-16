"""Durable user-goal boundaries alongside the authoritative operation journal.

The model resolves continuation versus a new request. Values retain literal user
provenance; accepted refusals are reusable only while backend facts are unchanged.
Agent scheduling and checkpoints remain owned by LangChain/LangGraph.
"""
from __future__ import annotations

from contextlib import nullcontext
from typing import Literal
import time

from pydantic import Field,model_validator

from .contracts import StrictModel,TaskResult


class UserFact(StrictModel):
    parameter: str = Field(description='Argument name from the actual business tool catalogue.')
    value: str = Field(min_length=1,max_length=500)
    quote: str = Field(min_length=1,max_length=1000,
        description='Exact excerpt from the latest user message containing this value.')


class RequestBoundary(StrictModel):
    relation: Literal['continue','replace','cancel'] = Field(
        description='Continue the pending goal with a clarification or repeated request; replace for a different/new goal; cancel for an explicit cancellation.')
    facts: list[UserFact] = Field(default_factory=list,max_length=12)
    acknowledgement: str = Field(default='',max_length=1000)


class RequestedAction(StrictModel):
    tool: str
    arguments: dict = Field(default_factory=dict,
        description='Only argument constraints explicitly supplied by the user; omit unknown or model-generated values.')
    description: str = Field(default='',max_length=300)
    after: list[int] = Field(default_factory=list,
        description='Zero-based indices in THIS actions list only; never receipt identifiers or positions. The first action must have after=[]. Use earlier action indices for a verification after a change.')


class GoalRequirements(StrictModel):
    kind: Literal['business_operations','information_only','unsupported']
    actions: list[RequestedAction] = Field(default_factory=list,max_length=12)
    communication_requirements: list[str]|None = Field(default=None,max_length=10,
        description='Additional requested explanations/content beyond reporting tool outcomes. Use [] when actions and their result confirmations cover the objective; null means coverage was not determined.')

    @model_validator(mode='after')
    def ordered_dependencies(self):
        if self.kind=='business_operations'and not self.actions:
            raise ValueError('A requested business operation requires at least one actual tool requirement')
        for i,action in enumerate(self.actions):
            if any(index<0 or index>=i for index in action.after):
                raise ValueError(f'actions[{i}].after={action.after} is invalid. Use only indices less than {i} '
                    'in this actions list, never observed receipt positions. The first action must have after=[].')
        return self


class GoalEpisode(StrictModel):
    id: str
    original_request: str
    updates: list[dict[str,str]]
    status: str = 'active'
    version: str = ''
    denial_evidence_ids: list[str] = Field(default_factory=list)
    result: TaskResult|None = None
    requirements: GoalRequirements|None = None

    @property
    def request_ids(self):return {u['request_id']for u in self.updates}

    @property
    def objective(self):
        extra=[u['text']for u in self.updates[1:]]
        return self.original_request+('\nUser-supplied follow-up messages:\n'+'\n'.join(extra)if extra else'')

    def unproven_actions(self,records,specs):
        if self.requirements is None:return []
        actions=self.requirements.actions
        has_writes=any(specs[a.tool].write for a in actions)
        ancestors={}
        for i,action in enumerate(actions):
            ancestors[i]=set()
            for j in action.after:
                if specs[actions[j].tool].write:ancestors[i].add(j)
                ancestors[i].update(ancestors[j])
        required={i for i,a in enumerate(actions)if not has_writes or specs[a.tool].write or ancestors[i]}
        proofs={};used=set();missing=[]
        for i,action in enumerate(actions):
            if i not in required:continue
            # The backend owns mutation preconditions. Its committed receipt
            # is authoritative over model-added prerequisite reads. Requested
            # post-write verification and subsequent mutations retain ordering.
            ordered=set(ancestors[i])|{j for j in action.after if j in required and (
                specs[action.tool].write or specs[actions[j].tool].write)}
            candidates=[r for r in records if r['id']not in used and r['name']==action.tool
                and r['status']==('committed'if specs[action.tool].write else'observed')
                and all(r['arguments'].get(k)==v for k,v in action.arguments.items())
                and all(j in proofs and r['created']>proofs[j]['created']for j in ordered)]
            if candidates:proofs[i]=candidates[0];used.add(candidates[0]['id'])
            else:missing.append(action)
        return missing


class GoalStore:
    def __init__(self,journal):
        self.journal=journal;self.db=journal.db;self.namespace=journal.namespace
        with journal.lock,self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS goal_episodes ('
                'id TEXT PRIMARY KEY,namespace TEXT,body TEXT,created REAL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS goal_requests ('
                'request_id TEXT PRIMARY KEY,goal_id TEXT)')

    def latest(self):
        row=self.db.execute('SELECT body FROM goal_episodes WHERE namespace=? ORDER BY created DESC LIMIT 1',
                            (self.namespace,)).fetchone()
        return GoalEpisode.model_validate_json(row[0])if row else None

    def for_request(self,request_id):
        row=self.db.execute('SELECT e.body FROM goal_episodes e JOIN goal_requests r ON r.goal_id=e.id '
                            'WHERE e.namespace=? AND r.request_id=?',(self.namespace,request_id)).fetchone()
        return GoalEpisode.model_validate_json(row[0])if row else None

    def save(self,episode,*,commit=True):
        with self.journal.lock,(self.db if commit else nullcontext()):
            self.db.execute('INSERT INTO goal_episodes VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                (episode.id,self.namespace,episode.model_dump_json(),time.time()))
            for update in episode.updates:
                self.db.execute('INSERT OR IGNORE INTO goal_requests VALUES(?,?)',(update['request_id'],episode.id))

    def fresh(self,question,request_id):
        result=GoalEpisode(id=request_id,original_request=question,
            updates=[{'request_id':request_id,'text':question}])
        self.save(result);return result

    def records(self,episode):
        return [r for r in self.journal.records()if r['request_id']in episode.request_ids]

    def known_facts(self,episode):
        text='\n'.join(update['text']for update in episode.updates)
        return {(key,value)for record in self.records(episode)for key,value in record['arguments'].items()
                if isinstance(value,str)and value in text}
