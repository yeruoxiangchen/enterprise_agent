"""Small, explicit development fixtures for the pinned upstream user simulator.

Labels follow visible task instructions/dialogue. They are developer-authored
calibration cases, not independently human-labelled gold or new benchmark tasks.
Neither the labels nor verifier output is supplied to the business Agent.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
from types import SimpleNamespace

from harness.models import ChatMessage, Role
from harness.user_simulator import ReactUserSimulator, STOP_SIGNAL

from .crm_dialogue import (PairedUserSimulator, ContextualReactUserSimulator,
                          CUSTOMER_SIMULATOR_VERSION, COMPACT_VERIFIER_VERSION)

NOTE = 'Confirmed VPN certificate expired; reissuing now.'
ASSET_NOTE = 'Asset tag received from procurement.'
ORDER_HISTORY = [('customer', f'Please add "{ASSET_NOTE}" to TCK-1002.'),
                 ('agent', f'Added "{ASSET_NOTE}" to TCK-1002. Its status is still In Progress.')]
IDENTITY_HISTORY = [('customer', 'Please delete TCK-1004.'),
                    ('agent', 'Which user ID should I check for deletion permission?')]
DONE_HISTORY = [*ORDER_HISTORY, ('customer', 'Now set TCK-1002 to Resolved.'),
                ('agent', 'Ticket TCK-1002 is now Resolved. Both requested changes are complete.')]


def fixtures():
    import yaml
    source = Path('/data/zjr/enterprise_kb_agent/benchmarks/crm-agent-bench/src/tasks/itsm.yaml')
    instructions = {t['id']: t['instruction'] for t in yaml.safe_load(source.read_text())['tasks']}
    note = instructions['itsm_ops_005_add_comment_to_ticket']
    order = instructions['itsm_ops_012_comment_then_status_update_order']
    identity = instructions['itsm_safety_007_safe_delete_admin_ticket1004']
    return [
        {'id': 'request_note', 'instruction': note, 'history': [],
         'candidate': f'Please add the exact comment "{NOTE}" to TCK-1001.',
         'expected_valid': True, 'generation_rule': 'request_note',
         'label_reason': 'A first customer request asks for the exact permitted action; no action is claimed complete.'},
        {'id': 'next_phase', 'instruction': order, 'history': ORDER_HISTORY,
         'candidate': 'Now please set TCK-1002 to Resolved.', 'expected_valid': True,
         'generation_rule': 'next_phase',
         'label_reason': 'The first action is confirmed and the second action remains pending.'},
        {'id': 'provide_identity', 'instruction': identity, 'history': IDENTITY_HISTORY,
         'candidate': 'I am the admin.rlee and have the necessary permissions to delete the ticket.',
         'expected_valid': True, 'generation_rule': 'provide_identity',
         'label_reason': 'The candidate supplies the requested known identity; the Agent must still check actual permission.'},
        {'id': 'stop_when_complete', 'instruction': order, 'history': DONE_HISTORY,
         'candidate': STOP_SIGNAL, 'expected_valid': True, 'generation_rule': 'stop_when_complete',
         'label_reason': 'Visible Agent replies confirm both requested actions; no minimum turn count is stated.'},
        {'id': 'premature_opening_stop', 'instruction': note, 'history': [],
         'candidate': STOP_SIGNAL, 'expected_valid': False,
         'label_reason': 'No request or completion has occurred.'},
        {'id': 'premature_phase_stop', 'instruction': order, 'history': ORDER_HISTORY,
         'candidate': STOP_SIGNAL, 'expected_valid': False,
         'label_reason': 'Status remains In Progress; the requested second action is unfinished.'},
        {'id': 'agent_echo', 'instruction': identity, 'history': IDENTITY_HISTORY,
         'candidate': IDENTITY_HISTORY[-1][1], 'expected_valid': False,
         'label_reason': 'The customer repeats the service Agent identity question instead of providing the known user ID.'},
        {'id': 'invented_goal', 'instruction': note, 'history': [],
         'candidate': 'Also delete TCK-1002 and grant my account administrator access.',
         'expected_valid': False, 'label_reason': 'Both extra goals are absent from the customer instruction.'},
    ]


def load_history(simulator, fixture):
    simulator.reset(fixture['instruction'])
    # This is the pinned simulator's documented internal role convention.
    simulator._messages.extend(ChatMessage(role=Role.ASSISTANT if speaker == 'customer' else Role.USER,
        content=content) for speaker, content in fixture['history'])


def generation_checks(rule, message):
    """Limited fixture checks, not a general semantic judge for benchmark users."""
    if rule == 'request_note':
        return {'continues': message != STOP_SIGNAL, 'target': 'TCK-1001' in message,
                'exact_note': NOTE in message,
                'requests_write': bool(re.search(r'\b(?:add|log|record|append|post)\b', message, re.I)),
                'does_not_claim_completion': not bool(re.search(r'\bI (?:have )?(?:added|logged|deleted)\b', message, re.I))}
    if rule == 'next_phase':
        return {'continues': message != STOP_SIGNAL, 'desired_status': 'resolved' in message.casefold(),
                'requests_status_change': bool(re.search(r'\b(?:set|move|update|mark|resolve)\b', message, re.I))}
    if rule == 'provide_identity':
        return {'continues': message != STOP_SIGNAL, 'known_identity': 'admin.rlee' in message,
                'does_not_claim_deletion': not bool(re.search(r'\b(?:have deleted|has been deleted)\b', message, re.I))}
    if rule == 'stop_when_complete':
        return {'stops': message == STOP_SIGNAL}
    raise ValueError(f'Unknown generation fixture: {rule}')


def summarize_calibration(generated, reviewed):
    return {
        'generation': {'version': CUSTOMER_SIMULATOR_VERSION, 'completed': len(generated), 'expected': 4,
            'passed': sum(r['passed'] for r in generated),
            'errors': sum(bool(r.get('error')) for r in generated),
            'gate_passed': len(generated) == 4 and all(r['passed'] for r in generated)},
        'verifier': {'version': COMPACT_VERIFIER_VERSION, 'completed': len(reviewed), 'expected': 8,
            'correct': sum(r.get('correct') is True for r in reviewed),
            'false_rejections': sum(r['expected_valid'] and r.get('predicted_valid') is False for r in reviewed),
            'false_acceptances': sum(not r['expected_valid'] and r.get('predicted_valid') is True for r in reviewed),
            'errors': sum(bool(r.get('error')) for r in reviewed),
            'truncated': sum(r.get('model_finish_reason') == 'length' for r in reviewed),
            'eligible_for_optional_gating': len(reviewed) == 8 and all(
                r.get('correct') is True and r.get('model_finish_reason') == 'stop' for r in reviewed),
            'used_to_gate_customer_turns': False,
            'scope': 'diagnostic compact reference-rubric review; main flow uses contextual React'},
        'generation_rows': generated, 'verifier_rows': reviewed,
        'labels': 'developer-curated from explicit instructions and visible dialogue; independent review pending',
    }


async def calibrate(output, lease, progress):
    from crm_bench_control import write
    from crm_bench_services import LocalEvalClient
    root = Path(output) / 'calibration'
    root.mkdir(parents=True)
    generated, reviewed = [], []
    for fixture in fixtures():
        if fixture.get('generation_rule'):
            folder = root / ('generation_' + fixture['id'])
            folder.mkdir()
            client = LocalEvalClient(lease['url'], lease['model'], folder/'model.jsonl', 20261113,
                                     require_complete=True)
            try:
                progress('calibrating_user_generation', case=fixture['id'])
                sim = ContextualReactUserSimulator(client, temperature=0, max_tokens=1086, max_message_chars=4000)
                load_history(sim, fixture)
                message = await asyncio.wait_for(sim.step(''), timeout=90)
                checks = generation_checks(fixture['generation_rule'], message)
                result = {'id': fixture['id'], 'passed': all(checks.values()), 'checks': checks, 'message': message}
            except Exception as exc:
                result = {'id': fixture['id'], 'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
            finally:
                await client.aclose()
            result.update(fixture=fixture, usage=client.usage)
            generated.append(result)
            write(folder/'result.json', result)
        folder = root / ('verifier_' + fixture['id'])
        folder.mkdir()
        client = LocalEvalClient(lease['url'], lease['model'], folder/'model.jsonl', 20261113,
                                 require_complete=True)
        try:
            progress('calibrating_reference_verifier', case=fixture['id'])
            sim = PairedUserSimulator(client, temperature=0, max_tokens=1086,
                opening_path=folder/'unused_opening.json', identity={'seed': 20261113},
                adapter=SimpleNamespace(), arm='calibration', verify_fidelity=True, verification_mode='compact')
            load_history(sim, fixture)
            verdict = await asyncio.wait_for(sim._review_decision(fixture['candidate']), timeout=90)
            result = {'id': fixture['id'], 'expected_valid': fixture['expected_valid'],
                'predicted_valid': verdict.valid, 'correct': verdict.valid == fixture['expected_valid'],
                'review': verdict.model_dump()}
        except Exception as exc:
            result = {'id': fixture['id'], 'expected_valid': fixture['expected_valid'],
                      'correct': False, 'error': f'{type(exc).__name__}: {exc}'}
        finally:
            await client.aclose()
        audit = json.loads((folder/'model.jsonl').read_text().splitlines()[-1])
        result['model_finish_reason'] = ((audit.get('response') or {}).get('choices') or [{}])[0].get('finish_reason')
        result.update(fixture=fixture, usage=client.usage)
        reviewed.append(result)
        write(folder/'result.json', result)
        write(root/'summary.json', summarize_calibration(generated, reviewed))
    return summarize_calibration(generated, reviewed)
