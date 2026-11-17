"""Execute one isolated native business episode with separate model roles."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time

from .tasks import BENCH_ROOT, DATA_ROOT, environment_for
from .reward import score_task


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n')
    temporary.replace(path)


async def run_episode(sample, supervisor, specialist, folder, *, temperature=0.7,
                      context_tokens=8192, output_tokens=512, max_calls=24,
                      timeout_seconds=180, model_factory=None,train_role='supervisor',
                      delegation_protocol='explicit',completion_review=False):
    from langchain_openai import ChatOpenAI
    from harness.tools.environment import create_environment
    import harness.tools.domains  # noqa: F401
    import yaml
    from kb.benchmark.crm_native_adapter import NativeCRMAdapter
    from kb.config import Settings

    folder = Path(folder).resolve()
    if not folder.is_relative_to(DATA_ROOT):
        raise ValueError('Keep RL episodes on project data storage')
    folder.mkdir(parents=True, exist_ok=False)
    if supervisor['url'] == specialist['url'] and supervisor['model'] == specialist['model']:
        raise ValueError('Selective training needs distinct policy and frozen-model resources')
    factory = model_factory or ChatOpenAI
    from threading import Lock
    from .role_usage import RoleUsage
    role_counts={}
    role_lock=Lock()

    def model(resource, role, sampling_temperature):
        candidate=factory(model=resource['model'], base_url=resource['url'],
            api_key=resource.get('key') or 'local', temperature=sampling_temperature,
            max_tokens=output_tokens, timeout=60, max_retries=0,
            metadata={'rl_role': role, 'rl_task_id': sample['id']},
            extra_body={'chat_template_kwargs': {'enable_thinking': False}})
        return candidate.model_copy(update={'callbacks':[
            *(candidate.callbacks or []),RoleUsage(role,role_counts,role_lock)]})

    if train_role not in ('supervisor','specialists'):
        raise ValueError('Unknown trainable agent role')
    root_trainable=train_role=='supervisor'
    root_resource=supervisor if root_trainable else specialist
    child_resource=specialist if root_trainable else supervisor
    root_model=model(root_resource,'supervisor' if root_trainable else 'frozen_supervisor',
                     temperature if root_trainable else 0)
    child_model=model(child_resource,'frozen_specialist' if root_trainable else 'specialist_policy',
                      0 if root_trainable else temperature)
    roles={'*':child_model}
    if completion_review:
        frozen_resource=child_resource if root_trainable else root_resource
        roles['reviewer']=model(frozen_resource,'frozen_reviewer',0)
    cfg = Settings(root=folder, mode='agent', llm_url=supervisor['url'],
        llm_model=supervisor['model'], llm_key='local',
        tokenize_url=specialist['url'].removesuffix('/v1')+'/tokenize',
        tokenize_model=specialist['model'],tokenize_key='',
        enable_thinking=False,
        context_tokens=context_tokens, output_tokens=output_tokens, timeout=60,
        harness_model_calls=max_calls, harness_tool_calls=32)
    environment = environment_for(sample)
    adapter = NativeCRMAdapter(environment, cfg,
        yaml.safe_load((BENCH_ROOT/'src/agents/itsm.yaml').read_text()),
        (BENCH_ROOT/'src/tasks/policies/itsm_policy.md').read_text(), folder,
        model=root_model, role_models=roles, fork_subagents=True,
        operation_permissions=True, prepare_operations=True,
        specialist_owned_checks=True, require_dependency_confirmation=True,
        bind_user_request=True,delegation_protocol=delegation_protocol,completion_review=completion_review)
    started = time.monotonic()
    turns = []
    error = None
    try:
        async def conversation():
            for message in sample['messages']:
                response = await adapter.run(message, turns)
                turns.extend([{'role': 'user', 'content': message},
                              {'role': 'assistant', 'content': response.content}])
        await asyncio.wait_for(conversation(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        error = 'EpisodeBudgetExceeded: conversation did not finish within the episode budget'
    except (ConnectionError, OSError) as exc:
        # Environment/provider outages are not negative behavior examples.
        error = f'{type(exc).__name__}: {exc}'
        raise
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        from kb.harness.native import NativeBudgetExceeded, NativeOutputContractError
        from langgraph.errors import GraphRecursionError
        from langchain_core.exceptions import ContextOverflowError, OutputParserException
        policy_failure=isinstance(exc,(NativeBudgetExceeded,NativeOutputContractError,
            ContextOverflowError,GraphRecursionError,OutputParserException))
        empty_answer=isinstance(exc,ValueError) and str(exc)=='Native graph returned without a textual answer'
        if not (policy_failure or empty_answer):
            # Programming, transport and framework failures must stop training,
            # rather than being silently labelled as bad policy trajectories.
            raise
    finally:
        result = score_task(sample, adapter, error=error)
        result.update(task_id=sample['id'], family=sample['family'], split=sample['split'],
            role_model_calls=role_counts,
            policy_model_calls_completed=role_counts[
                'supervisor' if root_trainable else 'specialist_policy']['completed'],
            seconds=time.monotonic()-started, turns=turns,
            model_calls=adapter.model_calls, tokens=adapter.prompt_tokens+adapter.completion_tokens,
            usage_missing_calls=adapter.usage_missing_calls,
            environment=environment.get_state(), audit=adapter.tool_calls,
            effects=adapter.engine.journal.records(),
            train_role=train_role,
            harness_profile={'delegation_protocol':delegation_protocol,
                             'completion_review':completion_review},
            review_status=adapter.engine.last_review_status,
            review_evaluations=adapter.engine.review_evaluations,
            supervisor={'model':root_resource['model'],'temperature':temperature if root_trainable else 0,
                        'trainable':root_trainable},
            specialist={'model':child_resource['model'],'temperature':0 if root_trainable else temperature,
                        'trainable':not root_trainable})
        write_json(folder/'result.json', result)
        await adapter.aclose()
    return result
