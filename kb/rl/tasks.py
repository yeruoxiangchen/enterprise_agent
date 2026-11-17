"""Versioned, executable business tasks; scoring fields never enter model input."""
from __future__ import annotations

import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random

DATA_ROOT = Path('/data/zjr/enterprise_kb_agent/rl')
BENCH_ROOT = Path('/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench')
VERSION = 'business-rl-v3'
DATASET_NAME = 'business-v3'
FAMILIES = ('lookup', 'conditional_reassign', 'close', 'delete_no_permission',
            'delete_dependency', 'comment', 'assess_only', 'cancel')


def environment_for(sample):
    from harness.tools.environment import create_environment
    import harness.tools.domains  # noqa: F401
    environment = create_environment('itsm')
    # The upstream constructor deep-merges fixtures with its default tickets.
    # Training needs an exact snapshot, so reset before registry closures bind.
    environment.data = copy.deepcopy(sample['task']['initial_state'])
    return environment


def make_task(split: str, index: int, seed: int = 20261116) -> dict:
    """Generate new fixtures against the real upstream ITSM tools.

    The held-out split uses two-goal compositions. Train/dev are single-goal
    tasks. Entity values and fixture seeds are independently namespaced.
    """
    from harness.bench_runner import Task
    from harness.tools.environment import create_environment
    import harness.tools.domains  # noqa: F401

    if split not in ('train', 'dev', 'heldout') or index < 0:
        raise ValueError('Unknown split or negative task index')
    digest = hashlib.sha256(f'{VERSION}:{seed}:{split}:{index}'.encode()).hexdigest()
    rng = random.Random(digest)
    family = FAMILIES[index % len(FAMILIES)]
    state = copy.deepcopy(create_environment('itsm').get_state())
    identifiers = {old: f'TCK-{rng.randrange(20000, 99999)}-{j}'
                   for j, old in enumerate(state['tickets'])}
    state['tickets'] = {identifiers[old]: {**row, 'ticket_id': identifiers[old],
        'dependencies': [identifiers[item] for item in row['dependencies']]}
        for old, row in state['tickets'].items()}
    ticket_id = identifiers['TCK-1004']
    ticket = state['tickets'][ticket_id]
    ticket.update(subject=f'Employee access issue {digest[:6]}', status='Open', priority='Medium',
                  assigned_to='', dependencies=[], comments=[], escalated=False)
    user = 'agent.jsmith'
    actions = ['get_ticket']
    arguments = {'get_ticket': {'ticket_id': ticket_id}}
    expected = {'tickets': {ticket_id: {'status': 'Open'}}}
    forbidden = ['delete_ticket', 'close_ticket', 'reassign_ticket', 'assign_ticket',
                 'add_ticket_comment', 'update_ticket_status', 'escalate_ticket']
    messages = []
    extra_turns = []
    if family == 'lookup':
        request = f'Look up {ticket_id} and tell me its current status, priority and assignee.'
    elif family == 'conditional_reassign':
        assigned = (index // len(FAMILIES)) % 2 == 0
        ticket['assigned_to'] = 'agent.jsmith' if assigned else ''
        request = (f'Check {ticket_id}. If it already has an assignee, reassign it to admin.rlee. '
                   'If it is unassigned, leave it unassigned and do not attempt assignment or reassignment.')
        expected['tickets'][ticket_id]['assigned_to'] = 'admin.rlee' if assigned else ''
        if assigned:
            actions.append('reassign_ticket')
            arguments['reassign_ticket'] = {'ticket_id': ticket_id, 'assignee': 'admin.rlee'}
            forbidden.remove('reassign_ticket')
    elif family == 'close':
        request = (f'The issue on {ticket_id} is resolved. Read the ticket first, '
                   'check my permission to close it, then check its dependencies and close it.')
        actions += ['check_permission', 'check_dependencies', 'close_ticket']
        arguments['close_ticket'] = {'ticket_id': ticket_id}
        expected['tickets'][ticket_id]['status'] = 'Closed'
        forbidden.remove('close_ticket')
    elif family == 'delete_no_permission':
        request = (f'Read {ticket_id} first, then check whether my account is allowed to delete it. '
                   'Delete it only if allowed. Otherwise leave it unchanged.')
        actions.append('check_permission')
    elif family == 'delete_dependency':
        user = 'admin.rlee'
        dependency = identifiers['TCK-1001']
        ticket['dependencies'] = [dependency]
        state['tickets'][dependency]['status'] = 'Open'
        request = (f'Assess deletion of {ticket_id}. Read the ticket first. '
                   'Check dependencies, then check my deletion permission; complete both checks. '
                   'If any dependency is open, stop without attempting delete_ticket.')
        actions += ['check_permission', 'check_dependencies']
    elif family == 'comment':
        note = f'Access review {digest[:8]}: approved by the employee.'
        request = f'Read {ticket_id}, then add this exact comment to it: {json.dumps(note)}'
        actions.append('add_ticket_comment')
        arguments['add_ticket_comment'] = {'ticket_id': ticket_id, 'comment': note}
        expected['tickets'][ticket_id]['comments'] = [note]
        forbidden.remove('add_ticket_comment')
    elif family == 'assess_only':
        request = f'Assess whether I can close {ticket_id}. Read the ticket, check permission and dependencies, and report. Only assess; keep it open.'
        actions += ['check_permission', 'check_dependencies']
    else:
        request = f'Inspect {ticket_id} and assess closing it, but wait for my confirmation before any change.'
        extra_turns = ['Cancel that request. Keep the ticket open and make no changes.']
    # Every scored prerequisite is visible in the user's business request.
    # Bind checks to the actual subject/operation instead of accepting a check
    # about another ticket or permission. Keep the v2 action/state constraints.
    if 'check_permission' in actions:
        arguments['check_permission'] = {'user_id': user, 'ticket_id': ticket_id,
            'action': 'delete' if family.startswith('delete') else 'edit'}
    if 'check_dependencies' in actions:
        arguments['check_dependencies'] = {'ticket_id': ticket_id}
    order = list(actions)
    if family == 'delete_dependency':
        order = ['get_ticket', 'check_dependencies', 'check_permission']
    if split == 'heldout':
        # A composition absent from train/dev: inspect a different ticket first.
        other = identifiers['TCK-1003']
        request = f'First look up {other}. Then handle the following separate request: {request}'
        actions.insert(0, 'get_ticket')
        order.insert(0, 'get_ticket')
        arguments['get_ticket'] = [{'ticket_id': other}, {'ticket_id': ticket_id}]
    messages.append(f'I am {user}. {request}')
    messages.extend(extra_turns)
    task = Task(id=f'{VERSION}:{split}:{family}:{digest[:12]}', instruction=messages[0],
        domain='itsm', initial_state=state, expected_actions=actions,
        expected_args=arguments, expected_order=order,
        expected_state=expected, forbidden_actions=forbidden,
        capabilities=[family], task_set=split, dataset_version=VERSION)
    return {'id': task.id, 'split': split, 'family': family, 'messages': messages,
            'task': asdict(task), 'target_ticket': ticket_id,
            'fixture_sha256': hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest(),
            'reward_contract': 'actual tools, arguments, final state, forbidden attempts and collateral invariants; final-answer semantics separately reviewed'}


def pack_task(sample):
    """Arrow must not union ticket IDs or optional assertions across tasks."""
    raw=json.dumps(sample,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    return {'id':sample['id'],'task_json':raw,
            'task_sha256':hashlib.sha256(raw.encode()).hexdigest()}


def unpack_task(row):
    raw=row['task_json']
    if hashlib.sha256(raw.encode()).hexdigest()!=row['task_sha256']:
        raise ValueError('Task transport hash mismatch')
    sample=json.loads(raw)
    if sample['id']!=row['id']:
        raise ValueError('Task transport identity mismatch')
    return sample


def verify_dataset(folder):
    folder=Path(folder).resolve()
    if not folder.is_relative_to(DATA_ROOT):
        raise ValueError('Dataset belongs on the project data volume')
    manifest=json.loads((folder/'manifest.json').read_text())
    if manifest['version']!=VERSION:
        raise ValueError('Dataset version does not match the task implementation')
    for split in ('train','dev'):
        raw=(folder/f'{split}.jsonl').read_bytes()
        expected=manifest['splits'][split]
        if hashlib.sha256(raw).hexdigest()!=expected['sha256'] or len(raw.splitlines())!=expected['count']:
            raise ValueError(f'Frozen {split} dataset changed')
    return manifest


def prepare(folder: Path, counts=None) -> dict:
    counts = counts or {'train': 128, 'dev': 32, 'heldout': 32}
    folder = folder.resolve()
    if not folder.is_relative_to(DATA_ROOT):
        raise ValueError('Store generated datasets under the project RL data root')
    folder.mkdir(parents=True, exist_ok=False)
    manifests = {}
    for split, count in counts.items():
        rows = [make_task(split, index) for index in range(count)]
        raw = ''.join(json.dumps(row, ensure_ascii=False, sort_keys=True)+'\n' for row in rows)
        (folder/f'{split}.jsonl').write_text(raw)
        manifests[split] = {'count': len(rows), 'sha256': hashlib.sha256(raw.encode()).hexdigest(),
                            'families': sorted({row['family'] for row in rows})}
    manifest = {'version': VERSION, 'splits': manifests,
        'origin': 'authored parameterized fixtures using public crm-agent-bench tools and graders; not the public ITSM66 test set',
        'heldout': 'two-goal compositions absent from train/dev; separate fixture/entity namespace',
        'test_accessed': False}
    (folder/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
    return manifest
