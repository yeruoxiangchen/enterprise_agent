"""Build observation/execution SubAgent specs from the actual tool catalogue.

The public task API stays native. Observation graphs have no write tools.
Execution graphs use domain capabilities by default; operation-level splitting
remains an explicit experiment. Executors share the declared read-only checks.
No customer-text classification, task labels or benchmark-specific routing.
"""
from __future__ import annotations


def native_capabilities(groups, specs, *, granularity='domain'):
    if granularity not in ('domain','operation'):
        raise ValueError('Unknown execution capability granularity')
    result={}
    # Shared observations include cross-domain permission/dependency checks.
    # Writes remain isolated; actual backend authorization still applies to reads.
    observations=[name for name,spec in specs.items()if not spec.write]
    reserved=set(groups)|{'general-purpose'}
    for role,cap in groups.items():
        reads=[name for name in cap['tools'] if not specs[name].write]
        writes=[name for name in cap['tools'] if specs[name].write]
        if not writes:
            result[role]={**cap,'model_role':role}
            continue
        executors=({name:role+('__execute'if len(writes)==1 else'__'+name)for name in writes}
            if granularity=='operation'else{'*':role+'__execute'})
        for executor in executors.values():
            if executor in reserved:
                raise ValueError('Generated execution role collides with the backend catalogue: '+executor)
            reserved.add(executor)
        result[role]={
            'tools':reads,'model_role':role,
            'description':'Read-only observations: '+('; '.join(specs[n].description for n in reads) or
                'No business observations here; use general-purpose for investigation')+
                '. Changes are handled by '+', '.join(executors.values())+'.',
            'instructions':'You are the observation specialist for this delegated objective. '
                'Complete every requested factual check using the available read tools. '
                'Report the actual results and receipts to the supervisor. '
                'The supervisor owns subsequent actions and delegation; return observations '
                'without promising to perform those actions.'}
        for name,executor in executors.items():
            selected=writes if name=='*'else[name]
            result[executor]={**cap,'tools':[*observations,*selected],'model_role':role,
                'description':'Execute requested changes: '+'; '.join(specs[n].description for n in selected)+
                    '. Follow the user conditions and report actual completed operations.'}
    return result
