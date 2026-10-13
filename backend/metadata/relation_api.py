"""Strict request contract; caller always supplies the active local profile."""


def dispatch_relation_write(store, profile_uuid, action, request):
    fields = {
        'executions': {'sourceKey', 'evidence', 'inputs', 'outputs'},
        'candidates': {'sourceExecutionUuid', 'targetExecutionUuid', 'sourceKey', 'evidence', 'domain', 'kind'},
        'confirm': {'relationUuid', 'expectedDecisionVersion', 'reason'},
        'revoke': {'relationUuid', 'expectedDecisionVersion', 'reason'},
    }
    if action not in fields or not isinstance(request, dict) or set(request) != fields[action]:
        raise ValueError('Invalid creation relation request fields')
    if action == 'executions':
        # User entry is an unverified execution claim, never a parse-run alias.
        return {'executionUuid': store.execution('user-entry', request['sourceKey'], request['evidence'],
                inputs=request['inputs'], outputs=request['outputs'], identity_state='execution_unknown')}
    if action == 'candidates':
        return {'relationUuid': store.relation(request['sourceExecutionUuid'], request['targetExecutionUuid'],
                'user-entry', request['sourceKey'], request['evidence'], domain=request['domain'],
                kind=request['kind'], evidence_state='inferred')}
    return store.decide(profile_uuid, request['relationUuid'], request['expectedDecisionVersion'],
                        state='confirmed' if action == 'confirm' else 'revoked', reason=request['reason'])
