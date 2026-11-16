"""Lightweight run/receipt contracts; usable by the Web app without LLM packages."""
import hashlib
import json


def can_resume(run):
    if run['status'] not in ('partial_success', 'error') or run.get('cancel_requested'):
        return False
    state = json.loads(run['state'])
    business = state.get('business', {})
    if state.get('policy') in {'business_deep_v1','business_native_v1'}:
        return (business.get('resumable')is True and bool(business.get('workspace'))
                and business.get('calls',0)<business.get('max_calls',30)
                and business.get('tools',0)<business.get('max_tools',36))
    return (state.get('policy') == 'business_multi_v6' and bool(business.get('plan'))
            and bool(business.get('task_failures')) and business.get('calls', 0) < 12
            and business.get('tools', 0) < 15)


def can_resume_for(run, policy):
    """A new application runtime must not load a different runtime's checkpoint."""
    return json.loads(run['state']).get('policy') == policy and can_resume(run)


def verified_partial_draft(run, ticket):
    state = json.loads(run['state'])
    if state.get('policy') not in {'business_multi_v6','business_deep_v1','business_native_v1'}:
        return False
    business = state.get('business', {})
    for receipt in business.get('receipts', []):
        completed = business.get('completed_tasks', {}).get(receipt.get('task_id'), {}).get('result', {})
        if (receipt.get('action') == 'draft' and receipt.get('verified') is True
                and receipt.get('ticket_id') == ticket['id']
                and receipt.get('source_run') == ticket['source_run']
                and receipt.get('title') == ticket['title']
                and receipt.get('description_sha256') == hashlib.sha256(ticket['description'].encode()).hexdigest()
                and completed.get('status') == 'answered'):
            return True
    return False
