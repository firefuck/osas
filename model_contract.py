"""Operator-verified model limits; unknown capabilities remain unknown."""
import copy
import json
from dataclasses import dataclass
from pathlib import Path

from chat_protocol import APIError, replay_fields


@dataclass(frozen=True)
class ModelContract:
    verified: bool = False
    source: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    output_parameter: str | None = None
    count_tokens_path: str | None = None
    context_enforced_upstream: bool = False
    reasoning_replay: bool = False

    @classmethod
    def load(cls, path, upstream_model):
        if not path:
            return cls()
        try:
            data = json.loads(Path(path).read_text())
            if not isinstance(data, dict) or data.get('upstream_model') != upstream_model:
                raise ValueError()
            args = {k: v for k, v in data.items() if k != 'upstream_model'}
            obj = cls(**args)
            for value in (obj.verified, obj.context_enforced_upstream, obj.reasoning_replay):
                if not isinstance(value, bool):
                    raise ValueError()
            for value in (obj.context_window, obj.max_output_tokens):
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                    raise ValueError()
            if obj.context_window is not None and obj.max_output_tokens is not None and obj.max_output_tokens > obj.context_window:
                raise ValueError()
            if obj.output_parameter not in (None, 'max_tokens', 'max_completion_tokens'):
                raise ValueError()
            if obj.output_parameter and obj.max_output_tokens is None:
                raise ValueError()
            if obj.count_tokens_path is not None and (not isinstance(obj.count_tokens_path, str) or
                    not obj.count_tokens_path.startswith('/') or obj.count_tokens_path.startswith('//') or
                    any(c in obj.count_tokens_path for c in ('?', '#', '..', '\r', '\n'))):
                raise ValueError()
            claims = any((obj.context_window, obj.max_output_tokens, obj.output_parameter, obj.count_tokens_path,
                          obj.context_enforced_upstream, obj.reasoning_replay))
            if claims and (not obj.verified or not isinstance(obj.source, str) or not obj.source.strip()):
                raise ValueError()
            if obj.verified and (not isinstance(obj.source, str) or not obj.source.strip()):
                raise ValueError()
            return obj
        except (OSError, ValueError, TypeError):
            raise ValueError('Invalid model profile: match upstream_model, record verification source, and use supported limits/capabilities') from None

    def metadata(self):
        return {'context_window': self.context_window, 'max_output_tokens': self.max_output_tokens,
                'output_limit_enforcement': 'upstream_contract' if self.output_parameter else 'unsupported',
                'context_enforcement': 'count_endpoint' if self.count_tokens_path else
                    'upstream_contract' if self.context_enforced_upstream else 'unavailable',
                'reasoning_replay': self.reasoning_replay,
                'profile_verified_by_operator': self.verified, 'verification_source': self.source}

    def prepare(self, request, wire, counter=None, policy='best_effort'):
        if policy not in ('best_effort', 'strict'):
            raise ValueError('Capability policy must be best_effort or strict')
        strict = policy == 'strict'
        # Best-effort forwards unknown capabilities without advertising support.
        # Neither policy turns opaque metadata into prompt text.
        if any(any(v is not None and v != '' and v != {} and v != [] for v in replay_fields(m).values())
               for m in request['messages']) and not self.reasoning_replay and strict:
            raise APIError(422, 'Upstream reasoning replay is not verified; configure a verified model profile before replaying state',
                           'reasoning_replay_unsupported')
        max_old, max_new = request.get('max_tokens'), request.get('max_completion_tokens')
        if max_old is not None and max_new is not None and max_old != max_new:
            raise APIError(400, 'max_tokens and max_completion_tokens must agree when both are supplied')
        budget = max_new if max_new is not None else max_old
        if budget is not None and not self.output_parameter and strict:
            raise APIError(422, 'This upstream does not have verified output-token enforcement', 'output_limit_unsupported')
        if budget is not None and self.max_output_tokens is not None and budget > self.max_output_tokens:
            raise APIError(400, 'Requested output budget exceeds the verified model output limit', 'output_limit_exceeded')
        result = copy.deepcopy(wire)
        if self.output_parameter:
            result.pop('max_tokens', None)
            result.pop('max_completion_tokens', None)
            budget = budget if budget is not None else self.max_output_tokens
            result[self.output_parameter] = budget
        if self.context_window is not None:
            if self.count_tokens_path:
                if counter is None:
                    if strict:
                        raise APIError(422, 'Exact context counting is unavailable for this backend', 'context_count_unavailable')
                else:
                    # Count the actual final prompt, tools, and provider state sent upstream.
                    count = counter(result)
                    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                        raise APIError(502, 'Upstream token-count endpoint returned an invalid count', 'invalid_token_count')
                    reserve = budget if budget is not None else self.max_output_tokens
                    if reserve is None:
                        if strict:
                            raise APIError(422, 'A verified output reserve is required for context accounting', 'context_count_unavailable')
                    elif count + reserve > self.context_window:
                        raise APIError(400, 'Input plus requested output exceeds the verified context window', 'context_length_exceeded')
            elif not self.context_enforced_upstream and strict:
                raise APIError(422, 'Configured context size has no verified enforcement/counting contract', 'context_count_unavailable')
        # Only check the returned count against a declared enforcement contract.
        # An unverified budget is a forwarded hint, not a promise from the bridge.
        return result, budget if self.output_parameter else None

    @staticmethod
    def check_result(raw, budget):
        if budget is None or not isinstance(raw, dict):
            return
        # An authoritative upstream counter may expose a violated budget. Never
        # truncate by characters and call that exact model-token enforcement.
        usage = raw.get('usage')
        used = usage.get('completion_tokens') if isinstance(usage, dict) else None
        if isinstance(used, int) and not isinstance(used, bool) and used > budget:
            raise APIError(502, 'Upstream exceeded its declared output-token contract', 'upstream_output_limit_violation')
