import copy
import json
import unittest

from chat_protocol import APIError, validate_request, prepare_request, normalize_completion, loads

TOOL = {'type': 'function', 'function': {'name': 'add', 'description': 'Add two integers', 'strict': True,
        'parameters': {'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}},
                       'required': ['a', 'b'], 'additionalProperties': False}}}


def request(**kwargs):
    return validate_request({'model': 'muse', 'messages': [{'role': 'user', 'content': 'add 2 and 3'}],
                             'tools': [copy.deepcopy(TOOL)], **kwargs})


def envelope(ctx, payload):
    return f'<osas-{ctx["nonce"]}>' + json.dumps(payload) + f'</osas-{ctx["nonce"]}>'


class ToolProtocolTests(unittest.TestCase):
    def test_auto_tool_call_and_final_answer(self):
        req = request()
        wire, ctx = prepare_request(req)
        self.assertNotIn('tools', wire)
        self.assertIn('parameters', wire['messages'][0]['content'])
        result = normalize_completion(envelope(ctx, {'type': 'tool_calls', 'calls': [
            {'name': 'add', 'arguments': {'a': 2, 'b': 3}}]}), req, ctx)
        self.assertEqual(result['finish_reason'], 'tool_calls')
        call = result['message']['tool_calls'][0]
        self.assertTrue(call['id'].startswith('call_'))
        self.assertEqual(loads(call['function']['arguments']), {'a': 2, 'b': 3})
        final = normalize_completion(envelope(ctx, {'type': 'final', 'content': '5'}), req, ctx)
        self.assertEqual(final['message']['content'], '5')

    def test_choice_none_required_and_named_enforced(self):
        payload = {'type': 'tool_calls', 'calls': [{'name': 'add', 'arguments': {'a': 2, 'b': 3}}]}
        for choice in ('none', 'required', {'type': 'function', 'function': {'name': 'add'}}):
            req = request(tool_choice=choice)
            _, ctx = prepare_request(req)
            if choice == 'none':
                with self.assertRaises(APIError):
                    normalize_completion(envelope(ctx, payload), req, ctx)
            else:
                with self.assertRaises(APIError):
                    normalize_completion(envelope(ctx, {'type': 'final', 'content': 'I called it'}), req, ctx)
                self.assertEqual(normalize_completion(envelope(ctx, payload), req, ctx)['finish_reason'], 'tool_calls')

    def test_schema_violations_and_undeclared_calls_fail_closed(self):
        req = request()
        _, ctx = prepare_request(req)
        for call in [{'name': 'unknown', 'arguments': {}}, {'name': 'add', 'arguments': {'a': 1}},
                     {'name': 'add', 'arguments': {'a': '1', 'b': 2}},
                     {'name': 'add', 'arguments': {'a': 1, 'b': 2, 'unexpected': True}}]:
            with self.subTest(call_name=call['name']), self.assertRaises(APIError) as error:
                normalize_completion(envelope(ctx, {'type': 'tool_calls', 'calls': [call]}), req, ctx)
            self.assertEqual(error.exception.status, 502)

    def test_parallel_false_rejects_multiple_calls(self):
        req = request(parallel_tool_calls=False)
        _, ctx = prepare_request(req)
        with self.assertRaises(APIError):
            normalize_completion(envelope(ctx, {'type': 'tool_calls', 'calls': [
                {'name': 'add', 'arguments': {'a': 1, 'b': 2}}, {'name': 'add', 'arguments': {'a': 3, 'b': 4}}]}), req, ctx)

    def test_tool_history_requires_each_result_exactly_once(self):
        call = {'id': 'call_one', 'type': 'function', 'function': {'name': 'add', 'arguments': '{"a":2,"b":3}'}}
        history = [{'role': 'user', 'content': 'calculate'}, {'role': 'assistant', 'content': None, 'tool_calls': [call]}]
        with self.assertRaises(APIError):
            request(messages=history)
        valid = history + [{'role': 'tool', 'tool_call_id': 'call_one', 'content': '5'}]
        normalized = request(messages=valid)
        wire, _ = prepare_request(normalized)
        self.assertEqual(json.loads(wire['messages'][-1]['content'])['tool_call_id'], 'call_one')
        for messages in (valid + [valid[-1]], history + [{'role': 'tool', 'tool_call_id': 'wrong', 'content': '5'}]):
            with self.assertRaises(APIError):
                request(messages=messages)

    def test_local_refs_combinators_and_remote_refs(self):
        tool = copy.deepcopy(TOOL)
        tool['function']['parameters'] = {'type': 'object', '$defs': {'positive': {'type': 'integer', 'minimum': 1}},
            'properties': {'a': {'$ref': '#/$defs/positive'}, 'b': {'anyOf': [{'type': 'integer'}, {'type': 'null'}]}},
            'required': ['a', 'b'], 'additionalProperties': False}
        req = request(tools=[tool]); _, ctx = prepare_request(req)
        value = {'type': 'tool_calls', 'calls': [{'name': 'add', 'arguments': {'a': 2, 'b': None}}]}
        self.assertEqual(normalize_completion(envelope(ctx, value), req, ctx)['finish_reason'], 'tool_calls')
        tool['function']['parameters']['properties']['a'] = {'$ref': 'https://example.com/schema'}
        with self.assertRaises(APIError):
            request(tools=[tool])

    def test_json_schema_final_and_json_object_validation(self):
        fmt = {'type': 'json_schema', 'json_schema': {'name': 'result', 'strict': True,
               'schema': {'type': 'object', 'properties': {'sum': {'type': 'integer'}},
                          'required': ['sum'], 'additionalProperties': False}}}
        req = request(tools=[], response_format=fmt); _, ctx = prepare_request(req)
        good = normalize_completion(envelope(ctx, {'type': 'final', 'content': '{"sum":5}'}), req, ctx)
        self.assertEqual(loads(good['message']['content']), {'sum': 5})
        with self.assertRaises(APIError):
            normalize_completion(envelope(ctx, {'type': 'final', 'content': '{"sum":"five"}'}), req, ctx)
        req = request(tools=[], response_format={'type': 'json_object'}); _, ctx = prepare_request(req)
        with self.assertRaises(APIError):
            normalize_completion(envelope(ctx, {'type': 'final', 'content': '[]'}), req, ctx)

    def test_malformed_envelope_duplicate_keys_and_truncation(self):
        req = request(); _, ctx = prepare_request(req)
        for output in ('I ran the tool', '```json\n{}\n```', envelope(ctx, {'type': 'tool_calls', 'calls': []}),
                       envelope(ctx, {'type': 'final', 'content': 'ok', 'unexpected': True})):
            with self.assertRaises(APIError):
                normalize_completion(output, req, ctx)
        with self.assertRaises(ValueError):
            loads('{"a":1,"a":2}')
        with self.assertRaises(ValueError):
            loads('{"a":NaN}')
        raw = {'message': {'content': envelope(ctx, {'type': 'tool_calls', 'calls': [
            {'name': 'add', 'arguments': {'a': 1, 'b': 2}}]})}, 'finish_reason': 'length'}
        truncated = normalize_completion(raw, req, ctx)
        self.assertEqual(truncated['finish_reason'], 'length')
        self.assertNotIn('tool_calls', truncated['message'])

    def test_native_mode_validates_args_and_keeps_call_ids(self):
        req = request(); wire, ctx = prepare_request(req, 'native')
        self.assertIsNone(ctx)
        self.assertEqual(wire['tools'], [TOOL])
        raw = {'message': {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'call_native', 'type': 'function', 'function': {'name': 'add', 'arguments': '{"a":1,"b":2}'}}]},
               'finish_reason': 'tool_calls'}
        self.assertEqual(normalize_completion(raw, req)['message']['tool_calls'][0]['id'], 'call_native')
        raw['message']['tool_calls'][0]['function']['arguments'] = '{"a":"bad","b":2}'
        with self.assertRaises(APIError):
            normalize_completion(raw, req)

    def test_disabled_mode_and_refusal(self):
        with self.assertRaises(APIError):
            prepare_request(request(), 'disabled')
        result = normalize_completion({'message': {'role': 'assistant', 'content': None, 'refusal': 'Cannot do that'},
                                       'finish_reason': 'stop'}, request(tool_choice='required'))
        self.assertEqual(result['message']['refusal'], 'Cannot do that')
        history = [{'role': 'user', 'content': 'request'}, result['message'], {'role': 'user', 'content': 'Try something else'}]
        self.assertEqual(request(messages=history)['messages'][1]['content'], 'Cannot do that')


if __name__ == '__main__':
    unittest.main()
