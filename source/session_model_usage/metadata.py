"""Request attribution from scoped, ordered metadata; never from UI selection."""
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass


FIELDS = ('model', 'reasoning_effort', 'service_tier')


def string(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def fields(data):
    result = {}
    if 'model' in data:
        result['model'] = string(data['model'])
    for target, aliases in (('reasoning_effort', ('reasoning_effort', 'effort')),
                            ('service_tier', ('service_tier', 'serviceTier'))):
        for key in aliases:
            if key in data:
                value = string(data[key])
                result[target] = value.lower() if value else None
                break
    return result


def fast_mode(tier):
    if tier in ('priority', 'fast'):
        return True
    if tier in ('default', 'standard'):
        return False
    return None


@dataclass
class Signal:
    position: int
    values: dict
    explicit: dict
    source: str


class TurnMetadata:
    def __init__(self):
        self.history = defaultdict(list)
        self.positions = defaultdict(list)
        self.current_turn = None
        self.active = False
        self.settings = {}
        self.settings_position = 0
        self.consumed_settings = 0
        self.active_start = 0

    def append(self, turn, position, values, source):
        if not isinstance(turn, str) or not turn:
            return
        previous = self.history[turn][-1].values if self.history[turn] else {}
        self.history[turn].append(Signal(position, {**previous, **values}, dict(values), source))
        self.positions[turn].append(position)

    def begin(self, turn, position):
        self.current_turn, self.active = turn, True
        self.active_start = position
        if self.history.get(turn):
            return
        # Service preference persists, but a prior turn's model does not.
        initial = {k: v for k, v in self.settings.items() if k == 'service_tier'}
        if self.settings_position > self.consumed_settings:
            initial.update(self.settings)
        self.consumed_settings = self.settings_position
        self.append(turn, position, initial, 'start')

    def context(self, turn, position, data):
        history = self.history.get(turn, [])
        if not history or self.current_turn is None or (
                history[0].source == 'pending' and history[0].position >= self.active_start):
            self.begin(turn, position)
        values = fields(data)
        values.setdefault('model', None)
        self.append(turn, position, values, 'context')

    def note_request(self, turn, position):
        if isinstance(turn, str) and turn and turn not in self.history:
            # A later context for this old request must not steal the active turn.
            self.append(turn, position, {}, 'pending')

    def apply_settings(self, position, data, turn=None):
        values = fields(data)
        self.settings.update(values)
        self.settings_position = position
        if turn is not None:
            self.append(turn, position, values, 'settings')
        elif self.active:
            self.append(self.current_turn, position, values, 'settings')

    def reroute(self, position, data, turn=None):
        values = fields(data)
        model = string(data.get('to_model', data.get('toModel')))
        if model:
            values['model'] = model
        self.append(turn or self.current_turn, position, values, 'reroute')

    def complete(self, turn=None):
        if turn is None or turn == self.current_turn:
            self.active = False

    def resolve(self, turn, position, explicit=None, active_turn=None):
        signals = self.history.get(turn, [])
        index = bisect_right(self.positions.get(turn, []), position)
        values = dict(signals[index - 1].values) if index else {}
        for name in FIELDS:
            if active_turn is not None and active_turn != turn:
                # A late response spanning changes has no request-start evidence.
                known = {s.values.get(name) for s in signals[:index]
                         if s.values.get(name) is not None}
                if len(known) > 1:
                    values[name] = None
                    continue
            if values.get(name) is not None:
                continue
            # Only the first following context can describe the initial request.
            future = signals[index:]
            context = next((s for s in future if s.source == 'context'), None)
            if context is None or context.explicit.get(name) is None:
                continue
            candidate = context.explicit[name]
            intervening = [s for s in future if s.position < context.position]
            if any(s.source == 'reroute' or
                   (s.explicit.get(name) is not None and s.explicit[name] != candidate)
                   for s in intervening):
                continue
            values[name] = candidate
        return {name: (explicit or {}).get(name, values.get(name)) for name in FIELDS}
