"""Text/function subset of OpenAI Responses, with bounded persistent chaining."""
import json
import copy
import os
import sqlite3
import time
import uuid
from pathlib import Path

from chat_protocol import APIError, dumps, text_content, replay_fields


class ResponseStore:
    def __init__(self, path, ttl=3600, capacity=128):
        self.path, self.ttl, self.capacity = path, ttl, capacity
        Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        db = sqlite3.connect(path)
        try:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS responses (id TEXT PRIMARY KEY, expires REAL, data TEXT, history TEXT)')
        finally:
            db.close()

    def save(self, response, history):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('DELETE FROM responses WHERE expires<=?', (time.time(),))
                db.execute('INSERT INTO responses VALUES (?,?,?,?)',
                           (response['id'], time.time() + self.ttl, dumps(response), dumps(history)))
                db.execute('DELETE FROM responses WHERE id NOT IN (SELECT id FROM responses ORDER BY expires DESC LIMIT ?)', (self.capacity,))
        finally:
            db.close()

    def get(self, response_id):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                db.execute('DELETE FROM responses WHERE expires<=?', (time.time(),))
                row = db.execute('SELECT data,history FROM responses WHERE id=?', (response_id,)).fetchone()
        finally:
            db.close()
        if row is None:
            raise APIError(404, 'Response not found, expired, or not stored', 'response_not_found')
        return json.loads(row[0]), json.loads(row[1])

    def delete(self, response_id):
        self.get(response_id)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                db.execute('DELETE FROM responses WHERE id=?', (response_id,))
        finally:
            db.close()


def to_chat(body, store):
    if body.get('background'):
        raise APIError(400, 'Background responses are not supported', 'unsupported_feature')
    if not isinstance(body.get('store', True), bool):
        raise APIError(400, 'store must be boolean')
    messages = []
    previous = body.get('previous_response_id')
    if previous is not None:
        if not isinstance(previous, str):
            raise APIError(400, 'previous_response_id must be a string')
        _, messages = store.get(previous)
        # Responses instructions apply only to the current invocation.
        messages = [m for m in messages if not m.get('_response_instructions')]
    instructions = body.get('instructions')
    if instructions is not None:
        if not isinstance(instructions, str):
            raise APIError(400, 'instructions must be text')
        messages.insert(0, {'role': 'system', 'content': instructions, '_response_instructions': True})
    items = body.get('input')
    if isinstance(items, str):
        items = [{'role': 'user', 'content': items}]
    if not isinstance(items, list) or not items:
        raise APIError(400, 'input must be text or a nonempty list of text/function items')
    for item in items:
        if not isinstance(item, dict):
            raise APIError(400, 'Response input items must be objects')
        kind = item.get('type', 'message')
        if kind == 'message':
            role = item.get('role')
            messages.append({'role': role, 'content': text_content(item.get('content'), allow_null=role == 'assistant'),
                             **(replay_fields(item) if role == 'assistant' else {})})
        elif kind == 'function_call':
            call = {'id': item.get('call_id'), 'type': 'function',
                    'function': {'name': item.get('name'), 'arguments': item.get('arguments')}}
            if messages and messages[-1].get('role') == 'assistant' and messages[-1].get('content') is None:
                messages[-1].setdefault('tool_calls', []).append(call)
                messages[-1].update(replay_fields(item))
            else:
                messages.append({'role': 'assistant', 'content': None, 'tool_calls': [call], **replay_fields(item)})
        elif kind == 'function_call_output':
            messages.append({'role': 'tool', 'tool_call_id': item.get('call_id'), 'content': text_content(item.get('output'))})
        elif kind == 'reasoning':
            # Retain provider-native summary/encrypted continuation items exactly.
            messages.append({'role': 'assistant', 'content': None, 'reasoning': copy.deepcopy(item)})
        else:
            raise APIError(400, 'Unsupported Responses input item; only text/functions are implemented', 'unsupported_feature')
    tools = body.get('tools', [])
    if not isinstance(tools, list):
        raise APIError(400, 'tools must be an array')
    converted = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get('type') != 'function':
            raise APIError(400, 'Only function tools are supported', 'unsupported_feature')
        converted.append({'type': 'function', 'function': {k: v for k, v in tool.items() if k != 'type'}})
    choice = body.get('tool_choice', 'auto' if tools else 'none')
    if isinstance(choice, dict) and choice.get('type') == 'function':
        choice = {'type': 'function', 'function': {'name': choice.get('name')}}
    result = {'model': body.get('model'), 'messages': messages, 'tools': converted,
              'tool_choice': choice, 'parallel_tool_calls': body.get('parallel_tool_calls', True),
              'stream': body.get('stream', False)}
    for name in ('temperature', 'top_p'):
        if name in body:
            result[name] = body[name]
    if 'max_output_tokens' in body:
        result['max_completion_tokens'] = body['max_output_tokens']
    text = body.get('text', {})
    if not isinstance(text, dict) or not isinstance(text.get('format', {}), dict):
        raise APIError(400, 'text.format must be an object')
    fmt = text.get('format', {'type': 'text'})
    if fmt.get('type') == 'json_schema':
        result['response_format'] = {'type': 'json_schema', 'json_schema': {k: v for k, v in fmt.items() if k != 'type'}}
    else:
        result['response_format'] = fmt
    return result, messages


def build_response(body, completion):
    message = completion['message']
    output = []
    metadata = replay_fields(message, status=502)
    reasoning = metadata.get('reasoning')
    if isinstance(reasoning, dict) and reasoning.get('type') == 'reasoning':
        if not isinstance(reasoning.get('id'), str):
            raise APIError(502, 'Upstream Responses reasoning item has no valid ID', 'invalid_replay_metadata')
        output.append(copy.deepcopy(reasoning))
    for call in message.get('tool_calls', []):
        output.append({'id': 'fc_' + uuid.uuid4().hex, 'type': 'function_call', 'status': 'completed',
                       'call_id': call['id'], **call['function']})
    if message.get('content') is not None or message.get('refusal') is not None:
        parts = []
        if message.get('content') is not None:
            parts.append({'type': 'output_text', 'text': message['content'], 'annotations': [], 'logprobs': []})
        if message.get('refusal') is not None:
            parts.append({'type': 'refusal', 'refusal': message['refusal']})
        output.append({'id': 'msg_' + uuid.uuid4().hex, 'type': 'message', 'status': 'completed', 'role': 'assistant',
                       'content': parts})
    reason = completion['finish_reason']
    incomplete = reason in ('length', 'content_filter')
    if output and metadata:
        # Extra fields on the first assistant output item survive stateless replay.
        target = next((item for item in output if item['type'] != 'reasoning'), None)
        if target is not None:
            target.update(copy.deepcopy(metadata))
    if incomplete:
        for item in output:
            if item.get('type') == 'message':
                item['status'] = 'incomplete'
    response = {'id': 'resp_' + uuid.uuid4().hex, 'object': 'response', 'created_at': int(time.time()),
            'status': 'incomplete' if incomplete else 'completed', 'error': None,
            'incomplete_details': {'reason': 'max_output_tokens' if reason == 'length' else reason} if incomplete else None,
            'model': 'muse', 'output': output, 'usage': None, 'store': body.get('store', True),
            'previous_response_id': body.get('previous_response_id'), 'instructions': body.get('instructions'),
            'tools': body.get('tools', []), 'tool_choice': body.get('tool_choice', 'auto' if body.get('tools') else 'none'),
            'parallel_tool_calls': body.get('parallel_tool_calls', True), 'metadata': body.get('metadata', {}),
            'max_output_tokens': body.get('max_output_tokens'), 'text': body.get('text', {'format': {'type': 'text'}})}
    if completion.get('incomplete_tool_calls'):
        response['bridge_details'] = {'incomplete_tool_calls': completion['incomplete_tool_calls'], 'executable': False}
    return response


def events(response):
    """Buffered but correctly ordered Responses lifecycle events, no fake tokens."""
    seq = 0
    def event(kind, **fields):
        nonlocal seq
        result = {'type': kind, 'sequence_number': seq, **fields}
        seq += 1
        return result
    initial = dict(response, status='in_progress', output=[])
    yield event('response.created', response=initial)
    yield event('response.in_progress', response=initial)
    for index, item in enumerate(response['output']):
        if item['type'] == 'reasoning':
            yield event('response.output_item.added', output_index=index, item=item)
            yield event('response.output_item.done', output_index=index, item=item)
            continue
        opening = dict(item, status='in_progress')
        if item['type'] == 'function_call':
            opening['arguments'] = ''
        else:
            opening['content'] = []
        yield event('response.output_item.added', output_index=index, item=opening)
        if item['type'] == 'function_call':
            arguments = item['arguments']
            for offset in range(0, len(arguments), 512):
                yield event('response.function_call_arguments.delta', item_id=item['id'], output_index=index,
                            delta=arguments[offset:offset + 512])
            yield event('response.function_call_arguments.done', item_id=item['id'], output_index=index,
                        arguments=arguments)
        else:
            for ci, part in enumerate(item['content']):
                field = 'refusal' if part['type'] == 'refusal' else 'text'
                yield event('response.content_part.added', item_id=item['id'], output_index=index,
                            content_index=ci, part=dict(part, **{field: ''}))
                if field == 'refusal':
                    yield event('response.refusal.delta', item_id=item['id'], output_index=index,
                                content_index=ci, delta=part[field])
                    yield event('response.refusal.done', item_id=item['id'], output_index=index,
                                content_index=ci, refusal=part[field])
                else:
                    yield event('response.output_text.delta', item_id=item['id'], output_index=index,
                                content_index=ci, delta=part['text'], logprobs=[])
                    yield event('response.output_text.done', item_id=item['id'], output_index=index,
                                content_index=ci, text=part['text'], logprobs=[])
                yield event('response.content_part.done', item_id=item['id'], output_index=index,
                            content_index=ci, part=part)
        yield event('response.output_item.done', output_index=index, item=item)
    yield event('response.' + response['status'], response=response)
