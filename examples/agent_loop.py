"""Real client-side read/write tool loop, scoped to an explicitly selected folder."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from muse_backend import validate_url


TOOLS = [
    {'type': 'function', 'function': {'name': 'read_file', 'description': 'Read a UTF-8 text file in the selected workspace',
     'strict': True, 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}},
                                    'required': ['path'], 'additionalProperties': False}}},
    {'type': 'function', 'function': {'name': 'write_file', 'description': 'Create a new UTF-8 file in the selected workspace; refuses overwrite',
     'strict': True, 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
                                    'required': ['path', 'content'], 'additionalProperties': False}}},
]


def execute(root, name, arguments):
    path = (root / arguments['path']).resolve()
    if not path.is_relative_to(root) or path == root:
        return {'error': 'Path must stay within the selected workspace'}
    try:
        if name == 'read_file':
            with path.open('rb') as handle:
                data = handle.read(100_001)
            if len(data) > 100_000:
                return {'error': 'Text file exceeds 100 KB'}
            return {'content': data.decode('utf-8')}
        if name == 'write_file':
            if len(arguments['content'].encode()) > 100_000:
                return {'error': 'Content exceeds 100 KB'}
            with path.open('x', encoding='utf-8') as handle:
                handle.write(arguments['content'])
            return {'created': arguments['path']}
    except (OSError, UnicodeError):
        return {'error': 'File unavailable, invalid UTF-8, or output already exists'}
    return {'error': 'Unknown tool'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default=os.getenv('BRIDGE_URL', 'http://127.0.0.1:8765/v1'))
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--max-steps', type=int, default=8, choices=range(1, 33))
    parser.add_argument('prompt')
    args = parser.parse_args()
    root = args.workspace.resolve(strict=True)
    if not root.is_dir():
        parser.error('workspace must be an existing directory')
    key = os.getenv('BRIDGE_USER_KEY', '')
    if not key:
        parser.error('Set BRIDGE_USER_KEY privately in the client environment')
    try:
        url = validate_url(args.base_url) + '/chat/completions'
        messages = [{'role': 'user', 'content': args.prompt}]
        for _ in range(args.max_steps):
            payload = {'model': 'muse', 'messages': messages, 'tools': TOOLS, 'parallel_tool_calls': False}
            request = urllib.request.Request(url, json.dumps(payload).encode(),
                {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=270) as response:
                message = json.load(response)['choices'][0]['message']
            messages.append(message)
            if not message.get('tool_calls'):
                print(message.get('content') or message.get('refusal') or '')
                return
            for call in message['tool_calls']:
                result = execute(root, call['function']['name'], json.loads(call['function']['arguments']))
                messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(result)})
        parser.exit(1, 'Agent step limit reached; no automatic continuation.\n')
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        parser.exit(1, 'Agent request failed; inspect bridge status and private configuration.\n')


if __name__ == '__main__':
    main()
