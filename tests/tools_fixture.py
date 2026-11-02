"""Shared synthetic model fixture. Never used by the running bridge."""
import json
import re

TOOL = {'type': 'function', 'function': {'name': 'add', 'strict': True,
        'parameters': {'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}},
                       'required': ['a', 'b'], 'additionalProperties': False}}}


def make_envelope(body, payload):
    nonce = re.search(r'<osas-([0-9a-f]{32})>', body['messages'][0]['content']).group(1)
    return f'<osas-{nonce}>' + json.dumps(payload) + f'</osas-{nonce}>'
