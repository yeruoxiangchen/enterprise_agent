"""Read-only preparation for backend-defined ticket mutation prerequisites.

These are service contracts, not learned workflow memories. The Agent chooses
the operation; this adapter obtains the facts its backend requires before the
official HITL middleware evaluates that proposed operation.
"""

READ_REQUIREMENTS = {
    'close_ticket': ('get_ticket', 'check_dependencies'),
    'reassign_ticket': ('get_ticket',),
}


def prepare(adapter, call, available):
    reads = READ_REQUIREMENTS.get(call['name'], ())
    arguments = {'ticket_id': call['args'].get('ticket_id')}
    for name in reads:
        if name not in available:
            raise PermissionError(f'{call["name"]} preparation cannot access {name} in this specialist')
        if adapter.port._proof(name, arguments) is None:
            result = adapter.engine.observe_precondition(name, arguments, call['id'])
            if result['status'] != 'observed' or result['value'].get('error'):
                break
