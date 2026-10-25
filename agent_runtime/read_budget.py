"""Frozen opt-in policy for task-local controller read attempts in the tool ledger."""
from dataclasses import dataclass
import json


@dataclass(frozen=True)
class ControllerReadBudget:
    max_attempts: int
    tool_names: tuple[str, ...]

    def __post_init__(self):
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 64:
            raise ValueError('Controller read attempt limit must be an integer from 1 to 64')
        if (not isinstance(self.tool_names, tuple) or not 1 <= len(self.tool_names) <= 64
                or any(not isinstance(n, str) or not n.startswith('container_') for n in self.tool_names)
                or len(set(self.tool_names)) != len(self.tool_names)):
            raise ValueError('A bounded unique controller tool set is required')
        object.__setattr__(self, 'tool_names', tuple(sorted(self.tool_names)))

    def manifest(self):
        return {'max_attempts': self.max_attempts, 'tool_names': list(self.tool_names),
                'semantics': 'task-started-attempts-v1'}

    def recover_exhaustion(self, serialized):
        payload = json.loads(serialized)
        if (not isinstance(payload, dict) or payload.get('code') != ControllerReadBudgetExhausted.code
                or type(payload.get('maximum')) is not int or payload['maximum'] != self.max_attempts
                or type(payload.get('consumed')) is not int or payload['consumed'] < self.max_attempts):
            raise ValueError('Budget denial does not match the frozen policy')
        return ControllerReadBudgetExhausted(self.max_attempts, payload['consumed'])


class ControllerReadBudgetExhausted(RuntimeError):
    code = 'CONTROLLER_READ_BUDGET_EXHAUSTED'

    def __init__(self, maximum, consumed):
        self.maximum, self.consumed = maximum, consumed
        super().__init__(self.code)
