"""Build observation/execution SubAgent specs from the actual tool catalogue.

The public task API stays native. Selecting an observation graph gives no write
tools; selecting its execution graph keeps the backend's declared capability.
No customer-text classification, task labels or benchmark-specific routing.
"""
from __future__ import annotations


def native_capabilities(groups, specs):
    result={}
    reserved=set(groups)|{'general-purpose'}
    for role,cap in groups.items():
        reads=[name for name in cap['tools'] if not specs[name].write]
        writes=[name for name in cap['tools'] if specs[name].write]
        if not writes:
            result[role]={**cap,'model_role':role}
            continue
        executor=role+'__execute'
        if executor in reserved:
            raise ValueError('Generated execution role collides with the backend catalogue: '+executor)
        result[role]={
            'tools':reads,'model_role':role,
            'description':'Read-only observations: '+('; '.join(specs[n].description for n in reads) or
                'No business observations here; use general-purpose for investigation')+
                '. Changes are handled by '+executor+'.',
            'instructions':'You are the observation specialist for this delegated objective. '
                'Complete every requested factual check using the available read tools. '
                'Report the actual results and receipts to the supervisor. '
                'The supervisor owns subsequent actions and delegation; return observations '
                'without promising to perform those actions.'}
        result[executor]={**cap,'model_role':role,
            'description':'Execute requested changes: '+'; '.join(specs[n].description for n in writes)+
                '. Follow the user conditions and report actual completed operations.'}
    return result
