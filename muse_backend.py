"""Text-chat connector to a separately operated Muse2API adapter."""
import json
import urllib.error
import urllib.parse
import urllib.request


class BackendError(Exception):
    def __init__(self, message, code='muse_backend_error', status=502):
        self.message, self.code, self.status = message, code, status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def validate_url(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname and
                 not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
        if parsed.port is not None and not 0 < parsed.port <= 65535:
            valid = False
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Adapter URL must be HTTP(S), with no embedded credentials, query, or fragment')
    if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('A remote Muse adapter requires HTTPS')
    return url.rstrip('/')


def request_json(url, api_key, path, body, timeout=20, method='POST'):
    request = urllib.request.Request(validate_url(url) + path, json.dumps(body).encode(),
        {'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'}, method=method)
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise BackendError('Muse adapter response exceeds 1 MiB')
            return json.loads(data)
    except urllib.error.HTTPError as e:
        # Preserve machine-readable context/limit failures without exposing the
        # provider's error text, prompts or account/session information.
        if e.code in (400, 413, 422):
            try:
                error = json.loads(e.read(65536)).get('error', {})
                code = error.get('code') if isinstance(error, dict) else None
            except (ValueError, TypeError, AttributeError):
                code = None
            if code in ('context_length_exceeded', 'output_limit_exceeded'):
                raise BackendError('Upstream rejected the request because its model token limit was exceeded', code, 400) from None
        if e.code in (401, 403):
            raise BackendError('Muse adapter authentication failed; check its private key', 'muse_auth_error') from None
        if e.code == 429:
            raise BackendError('Muse adapter is rate limited', 'muse_rate_limit', 503) from None
        raise BackendError(f'Muse adapter returned HTTP {e.code}; inspect its private console') from None
    except (OSError, ValueError):
        # Never return upstream bodies, session cookies, proxy credentials or URLs.
        raise BackendError('Muse adapter connection or response failed; check its status and HTTPS trust') from None


def chat_payload(model, request):
    return dict(request, model=model, stream=False)


def complete(url, api_key, model, request, timeout):
    body = chat_payload(model, request)
    result = request_json(url, api_key, '/chat/completions', body, timeout=timeout)
    try:
        choice = result['choices'][0]
        content = choice['message']['content']
        finish_reason = choice.get('finish_reason', 'stop')
        if not isinstance(content, str) and not choice['message'].get('tool_calls') and not choice['message'].get('refusal'):
            raise ValueError()
        if finish_reason not in ('stop', 'length', 'content_filter', 'tool_calls'):
            raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError):
        raise BackendError('Muse adapter did not return a supported text completion') from None
    response = {'message': choice['message'], 'finish_reason': finish_reason}
    # Retained internally for budget-contract checking, not claimed as frontend
    # billing usage (the pinned web adapter returns placeholder zero counters).
    if 'usage' in result:
        response['usage'] = result['usage']
    return response
