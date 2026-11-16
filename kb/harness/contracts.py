"""Typed boundaries between agents, tools, memories and evaluation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol
import json
import re

from pydantic import BaseModel, ConfigDict, Field,model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class TaskResult(StrictModel):
    """Deliver an evidence-grounded result or explicitly request missing input."""
    status: Literal['completed', 'needs_user_input', 'refused', 'partial', 'needs_reconciliation']
    answer: str = Field(min_length=1, max_length=6000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)
    remaining_work: list[str] = Field(default_factory=list, max_length=12)
    deferred_user_actions: list[str] = Field(default_factory=list,max_length=4)


class SpecialistReport(StrictModel):
    """Observed specialist output; the supervisor owns overall completion."""
    kind: Literal['specialist_report'] = 'specialist_report'
    answer: str = Field(min_length=1,max_length=6000)
    observations: list[dict] = Field(default_factory=list)


class RemainingWorkItem(StrictModel):
    kind: Literal['agent_action','missing_user_input','deferred_user_action'] = Field(
        description='agent_action is unfinished authorized agent work; missing_user_input blocks that work; deferred_user_action is a future user decision after the requested agent deliverable is ready, such as confirming a prepared draft in the UI.')
    description: str = Field(min_length=1,max_length=500)


class CompletionMetadata(StrictModel):
    """Short schema-only finishing call; the original answer is preserved."""
    status: Literal['completed', 'needs_user_input', 'refused', 'partial', 'needs_reconciliation'] = Field(
        description='Use needs_user_input when the answer asks for missing user information, refused for a grounded denial, partial for authorized agent work still unfinished, and completed when the current request is delivered.')
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    remaining_work: list[RemainingWorkItem] = Field(default_factory=list, max_length=4,
        description='Classify every remaining item by who owns the next action. A future user decision is distinct from missing information that blocks the agent.')

    def task_result(self,answer):
        agent=[w.description for w in self.remaining_work if w.kind=='agent_action']
        missing=[w.description for w in self.remaining_work if w.kind=='missing_user_input']
        deferred=[w.description for w in self.remaining_work if w.kind=='deferred_user_action']
        status=self.status
        if status in ('completed','partial','needs_user_input'):
            if agent:status='partial'
            elif missing:status='needs_user_input'
            elif deferred:status='completed'
        return TaskResult(status=status,answer=answer,evidence_ids=self.evidence_ids,
            remaining_work=agent+missing,deferred_user_actions=deferred)


class WorkflowStep(StrictModel):
    description: str
    tool: str = Field(description='A real tool name from the source tool catalogue.')
    arguments: dict[str, Any] = Field(default_factory=dict,
        description='Use $slot references, never source-specific IDs or names.')
    depends_on: list[int] = Field(default_factory=list)


class WorkflowCard(StrictModel):
    """A reusable procedure induced from actual execution, with failure branches."""
    title: str = Field(min_length=3, max_length=160)
    applicability: str = Field(min_length=10, max_length=1200)
    slots: list[str] = Field(min_length=1, max_length=12)
    steps: list[WorkflowStep] = Field(min_length=1, max_length=12)
    exception_branches: list[str] = Field(min_length=1, max_length=10)
    stop_conditions: list[str] = Field(min_length=1, max_length=10)
    source_trace_ids: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def valid_workflow_bindings(self):
        def parameters(value):
            if isinstance(value,str)and value.startswith('$'):yield value[1:]
            elif isinstance(value,dict):
                for child in value.values():yield from parameters(child)
            elif isinstance(value,list):
                for child in value:yield from parameters(child)
        for i,step in enumerate(self.steps):
            if any(d<0 or d>=i for d in step.depends_on):
                raise ValueError('Workflow dependencies must form an ordered DAG')
            missing=set(parameters(step.arguments))-set(self.slots)
            if missing:raise ValueError('Workflow uses an undeclared parameter slot: '+', '.join(sorted(missing)))
        text=json.dumps(self.model_dump(exclude={'source_trace_ids'}),ensure_ascii=False)
        if re.search(r'\bTCK-[A-Za-z0-9]+\b|\b(?:admin|agent)\.[\w]+\b|expected_actions|expected_answer',text):
            raise ValueError('Workflow contains source-specific identity or evaluation material')
        return self


class ProgressDecision(StrictModel):
    """Magentic-One style progress decision grounded in execution feedback."""
    is_request_satisfied: bool
    is_progress_being_made: bool
    is_in_loop: bool
    next_action: Literal['replan', 'ask_user', 'reconcile', 'stop']
    instruction: str = Field(min_length=1, max_length=1500)
    next_agent: str | None = Field(default=None,
        description='On replan, choose an actual available specialist or supervisor. The runtime dispatches this model-selected next speaker through the native task node.')
    final_result: TaskResult | None = Field(default=None,
        description='When satisfied and stopping, provide the final result grounded in ledger evidence.')


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    domain: str
    write: bool = False


@dataclass
class ToolOutcome:
    value: Any
    status: Literal['observed', 'committed', 'refused', 'unknown'] = 'observed'


class BusinessPort(Protocol):
    """Actor-visible tools only. Evaluator answers are never part of this API."""
    policy: str
    def tools(self) -> list[ToolSpec]: ...
    def execute(self, name: str, arguments: dict, operation_id: str) -> ToolOutcome: ...
    def reconcile(self, name: str, arguments: dict, operation_id: str) -> ToolOutcome: ...
    def version(self) -> str: ...
