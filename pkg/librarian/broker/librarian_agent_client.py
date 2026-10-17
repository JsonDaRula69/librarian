"""In-image, single-session Hermes transport driver for controller wire v1.

No CLI/profile discovery, network clients, built-in tools, or ambient agent state.
The controller remains authoritative; a final model answer is not evidence.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import argparse
from types import SimpleNamespace

from .librarian_request import parse_request, _unique_object, _invalid_constant, _TOKEN, _token


class ClientError(ValueError):
    """Fail-closed protocol or model response error."""

    def __init__(self, message, *, recoverable=False):
        super().__init__(message)
        self.recoverable = recoverable

    @property
    def category(self):
        message = str(self.args[0]) if self.args else ''
        if message in ('invalid JSON', 'invalid decision', 'invalid inference result'):
            return 'invalid_decision_json'
        if message.startswith('invalid ') and message not in ('invalid frame', 'invalid response', 'invalid result', 'invalid rejection'):
            return 'invalid_tool_args'
        if message == 'iteration limit':
            return 'iteration_limit'
        return 'transport_error'


RECOVERABLE_ERROR_CODES = frozenset({
    'prepare_source_first', 'submit_artifact_first', 'reconciliation_required',
    'acquire_source_first', 'source_acquisition_blocked',
})


def decode(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ClientError('invalid JSON') from None


class StdioRPC:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.lock = threading.Lock()
        self.sequence = 0
        self.closed = False

    def read_line(self, limit):
        line = self.reader.readline(limit + 1)
        if not line.endswith(b'\n') or len(line) > limit:
            self.closed = True
            raise ClientError('invalid frame')
        return line

    def request(self):
        return parse_request(self.read_line(1048576).decode('utf-8'))

    def call(self, op, params):
        with self.lock:
            if self.closed or self.sequence >= 128:
                raise ClientError('connection closed')
            self.sequence += 1
            frame = (json.dumps({'id': self.sequence, 'op': op, 'params': params},
                                separators=(',', ':'), allow_nan=False) + '\n').encode()
            if len(frame) > 65536:
                raise ClientError('frame limit')
            try:
                self.writer.write(frame)
                self.writer.flush()
                reply = decode(self.read_line(65536))
                if (type(reply) is not dict or type(reply.get('id')) is not int
                        or reply['id'] != self.sequence or type(reply.get('ok')) is not bool):
                    raise ClientError('invalid response')
                if reply['ok']:
                    if set(reply) != {'id', 'ok', 'result'} or type(reply['result']) is not dict:
                        raise ClientError('invalid result')
                    return reply['result']
                if reply == {'id': self.sequence, 'ok': False, 'error': 'request_rejected'}:
                    raise ClientError('request rejected')
                error = reply.get('error')
                if (set(reply) == {'id', 'ok', 'error'} and type(error) is dict
                        and set(error) == {'code'}
                        and error['code'] in RECOVERABLE_ERROR_CODES):
                    raise ClientError(error['code'], recoverable=True)
                raise ClientError('invalid rejection')
            except ClientError as exc:
                if not exc.recoverable:
                    self.closed = True
                raise
            except Exception:
                self.closed = True
                raise


OPERATIONS = {
    'source_acquire': ('source.acquire', 'source_id'),
    'source_prepare': ('source.prepare', 'source_id'),
    'artifact_submit': ('artifact.submit', 'artifact_id'),
    'artifact_poll': ('artifact.poll', 'artifact_id'),
    'source_list': ('source.list', None),
    'source_propose': ('source.propose', 'source'),
    'component_propose': ('component.propose', 'component'),
    'discovery_status': ('discovery.status', None),
    'discovery_fetch': ('discovery.fetch', 'url'),
    'discovery_read': ('discovery.read', ('receipt_id', 'offset', 'limit')),
    'discovery_links': ('discovery.links', ('receipt_id', 'offset', 'limit')),
    'discovery_register_candidate': ('discovery.register_candidate',
                                     ('candidate_id', 'receipt_id', 'authority_receipt_id')),
    'artifact_list': ('artifact.list', ('source_id', 'offset', 'limit')),
    'ingestion_resume': ('ingestion.resume', None),
}


# Discovery clients must not advertise lifecycle or ingestion operations.
DISCOVERY_OPERATIONS = frozenset({
    'source_list', 'source_propose', 'component_propose', 'discovery_status',
    'discovery_fetch', 'discovery_read', 'discovery_links',
    'discovery_register_candidate',
})


TOOL_DESCRIPTIONS = {
    'source_list': 'List authorized controller source IDs for discovery; no lifecycle ordering is imposed.',
    'source_acquire': 'Acquire an authorized source and create its receipt before source_prepare.',
    'source_prepare': 'Prepare an acquired source; lifecycle is acquire then prepare then submit then poll.',
    'artifact_submit': 'Submit a prepared artifact after acquire then prepare; poll next.',
    'artifact_poll': 'Poll a submitted artifact after acquire then prepare then submit.',
    'ingestion_resume': 'Resume a bounded ingestion batch using ingestion.resume.',
    'artifact_list': 'List prepared artifacts for an authorized source without forcing discovery ordering.',
}



def argument_schema(field):
    if isinstance(field, tuple):
        fields = list(field)
        properties = {}
        for name in fields:
            if name == 'offset':
                properties[name] = {'type': 'integer', 'minimum': 0}
            elif name == 'limit':
                properties[name] = {'type': 'integer', 'minimum': 1, 'maximum': 4096 if 'receipt_id' in fields else 256}
            elif name == 'receipt_id':
                properties[name] = {'type': 'string', 'pattern': '^[0-9a-f]{32}$'}
            else:
                properties[name] = {'type': 'string'}
        return {'type': 'object', 'properties': properties, 'required': fields,
                'additionalProperties': False}
    fields = [] if field is None else [field]
    if field == 'source':
        fields = ['product_id', 'version', 'candidate_id']
    elif field == 'component':
        fields = ['product_id', 'version', 'dependency_of', 'evidence_source_ids']
    return {'type': 'object', 'properties': {
        f: ({'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 32,
             'uniqueItems': True} if f == 'evidence_source_ids' else {'type': 'string'})
        for f in fields}, 'required': fields, 'additionalProperties': False}


def controller_tools(rpc, *, discovery_only=False):
    from tools.registry import ToolRegistry
    registry = ToolRegistry()
    allowed = DISCOVERY_OPERATIONS if discovery_only else OPERATIONS.keys()
    for name, (operation, field) in OPERATIONS.items():
        if name not in allowed:
            continue
        def handler(args, _op=operation, _field=field, **kwargs):
            validate_arguments(_field, args)
            try:
                result = rpc.call(_op, args)
            except ClientError as exc:
                if not exc.recoverable:
                    raise
                result = {'error': exc.args[0], 'recoverable': True}
            return json.dumps(result, separators=(',', ':'))
        registry.register(name=name, toolset='librarian_controller',
                          schema={'name': name, 'description': TOOL_DESCRIPTIONS.get(name, 'Controller ' + operation),
                                  'parameters': argument_schema(field)},
                          handler=handler, check_fn=lambda: True)
    return registry


def validate_arguments(field, args):
    if field is None:
        if type(args) is not dict or args:
            raise ClientError('invalid tool arguments')
        return
    if isinstance(field, tuple):
        if field == ('receipt_id', 'offset', 'limit'):
            allowed = ({'receipt_id'}, set(field))
        else:
            allowed = (set(field),)
        if type(args) is not dict or set(args) not in allowed:
            raise ClientError('invalid tool arguments')
        for key, value in args.items():
            if key == 'source_id':
                validate_arguments('source_id', {key: value})
            elif key == 'receipt_id':
                if type(value) is not str or re.fullmatch(r'[0-9a-f]{32}', value) is None:
                    raise ClientError('invalid receipt')
            elif key == 'authority_receipt_id':
                if type(value) is not str or re.fullmatch(r'[0-9a-f]{32}', value) is None:
                    raise ClientError('invalid authority receipt')
            elif key == 'url':
                if (type(value) is not str or len(value) > 2048
                        or re.fullmatch(r'https://[^\\s]+', value) is None):
                    raise ClientError('invalid candidate URL')
            elif key == 'offset':
                if type(value) is not int or value < 0:
                    raise ClientError('invalid offset')
            elif key == 'limit':
                maximum = 32 if field == ('receipt_id', 'offset', 'limit') else 4096
                if type(value) is not int or not 1 <= value <= maximum:
                    raise ClientError('invalid limit')
        return
    if field in ('source', 'component'):
        if type(args) is not dict or set(args) != set(argument_schema(field)['required']):
            raise ClientError('invalid tool arguments')
        for key, value in args.items():
            if key == 'evidence_source_ids':
                if (type(value) is not list or not 1 <= len(value) <= 32
                        or any(type(v) is not str for v in value) or len(set(value)) != len(value)):
                    raise ClientError('invalid evidence IDs')
                for identifier in value:
                    validate_arguments('source_id', {'source_id': identifier})
            elif key in ('product_id', 'dependency_of'):
                try:
                    _token(value, _TOKEN, key)
                except ValueError:
                    raise ClientError('invalid product token') from None
            elif key == 'version':
                if type(value) is not str or re.fullmatch(r'[A-Za-z0-9*][A-Za-z0-9._+*-]{0,99}', value) is None:
                    raise ClientError('invalid version')
            else:
                validate_arguments('source_id', {'source_id': value})
        if field == 'component' and args['product_id'] == args['dependency_of']:
            raise ClientError('invalid dependency')
        return
    pattern = (r'https://[^\s]+' if field == 'url' else
               r'[0-9a-f]{32}' if field == 'receipt_id' else
               r'[0-9a-f]{32}' if field == 'authority_receipt_id' else
               r'[A-Za-z0-9_-]{1,128}' if field == 'source_id' else r'[0-9a-f]{64}')
    if (type(args) is not dict or set(args) != {field} or type(args[field]) is not str
            or (len(args[field]) > 2048 if field == 'url' else False)
            or re.fullmatch(pattern, args[field]) is None):
        raise ClientError('invalid tool arguments')


class ControllerCompletionClient:
    """ProviderProfile.create_client-compatible non-streaming SDK surface.

    Text-only inference carries a closed JSON decision, not native tool calls.
    Unknown/extra fields fail closed before any controller operation.
    """
    HERMES_SKIP_TRANSPORT_WRAP = True

    def __init__(self, rpc):
        self.rpc = rpc
        self.chat = SimpleNamespace(completions=self)

    def create(self, *, messages, tools=None, stream=False, **kwargs):
        if stream:
            raise ClientError('streaming unsupported')
        prompt = json.dumps({'protocol': 'librarian-decision-v1',
                             'response_format': {'tool_call': {'tool': 'tool_name', 'arguments': {}},
                                                 'final_answer': {'final': 'text'}},
                             'messages': messages, 'tools': tools or []}, separators=(',', ':'))
        if len(prompt.encode()) > 32768:
            raise ClientError('inference prompt limit')
        result = self.rpc.call('inference.complete', {'prompt': prompt})
        if set(result) != {'text'} or type(result['text']) is not str:
            raise ClientError('invalid inference result')
        try:
            decision = decode(result['text'])
        except ClientError:
            # Spend one inference call, not another agent turn, correcting only
            # the wire format. Tool validation remains unchanged below.
            retry_messages = list(messages) + [{
                'role': 'user',
                'content': 'Format correction only: return exactly one valid JSON decision object; no markdown or prose.'
            }]
            retry_prompt = json.dumps({'protocol': 'librarian-decision-v1',
                                       'response_format': {'tool_call': {'tool': 'tool_name', 'arguments': {}},
                                                           'final_answer': {'final': 'text'}},
                                       'messages': retry_messages, 'tools': tools or []}, separators=(',', ':'))
            if len(retry_prompt.encode()) > 32768:
                raise ClientError('inference prompt limit')
            retry_result = self.rpc.call('inference.complete', {'prompt': retry_prompt})
            if set(retry_result) != {'text'} or type(retry_result['text']) is not str:
                raise ClientError('invalid inference result')
            decision = decode(retry_result['text'])
        if type(decision) is not dict:
            raise ClientError('invalid decision')
        from openai.types.chat import ChatCompletion
        if set(decision) == {'final'} and type(decision['final']) is str and decision['final']:
            message = {'role': 'assistant', 'content': decision['final']}
            reason = 'stop'
        elif (set(decision) == {'tool', 'arguments'} and type(decision['tool']) is str
              and decision['tool'] in OPERATIONS):
            name = decision['tool']
            advertised = {t['function']['name'] for t in tools or []}
            if name not in advertised:
                raise ClientError('tool not exposed')
            validate_arguments(OPERATIONS[name][1], decision['arguments'])
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'rpc_' + str(self.rpc.sequence), 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(decision['arguments'])}}]}
            reason = 'tool_calls'
        else:
            raise ClientError('invalid decision')
        return ChatCompletion(id='controller', created=0, model='controller-pinned',
                              object='chat.completion', choices=[{
                                  'index': 0, 'finish_reason': reason, 'message': message}])

    def close(self):
        self.rpc.closed = True


def provider_profile(rpc):
    from providers.base import ProviderProfile
    class ControllerProfile(ProviderProfile):
        def create_client(self, **kwargs):
            return ControllerCompletionClient(rpc)

        def fetch_models(self, **kwargs):
            return ['controller-pinned']
    return ControllerProfile(name='librarian-stdio', auth_type='external_process',
                             api_mode='chat_completions', env_vars=(),
                             fallback_models=('controller-pinned',))


def run(rpc, *, max_iterations=16, discovery_only=False):
    """Use Hermes' real provider transport and a private Hermes tool registry.

    Deliberately not AIAgent: that entrypoint discovers ambient plugins, memory,
    auxiliary clients and default tools. This bounded driver imports none of it.
    """
    request = rpc.request()
    from broker.librarian_identity import MISSION
    from agent.transports.chat_completions import ChatCompletionsTransport
    registry = controller_tools(rpc, discovery_only=discovery_only)
    allowed = DISCOVERY_OPERATIONS if discovery_only else frozenset(OPERATIONS)
    tools = registry.get_definitions(set(allowed), quiet=True)
    profile = provider_profile(rpc)
    client = profile.create_client()
    transport = ChatCompletionsTransport()
    messages = [{'role': 'system', 'content': (MISSION + '\n' +
        'Operate only through the listed controller tools. Source IDs must be controller '
        'IDs; list the catalog to learn them. Propose candidate IDs or components for '
        'trusted controller validation; never claim official authority. Components '
        'require evidence source IDs. Unresolved proposals block completion. Return exactly one JSON '
        'decision: {"tool":"name","arguments":{...}} or {"final":"text"}. '
        'A final answer is not controller evidence.')},
        {'role': 'user', 'content': json.dumps(request, separators=(',', ':'))}]
    try:
        for _ in range(max_iterations):
            kwargs = transport.build_kwargs('controller-pinned', messages, tools,
                                            provider_profile=profile)
            response = client.chat.completions.create(**kwargs)
            normalized = transport.normalize_response(response)
            if not normalized.tool_calls:
                return normalized.content
            messages.append(response.choices[0].message.model_dump(exclude_none=True))
            for call in normalized.tool_calls:
                if call.name not in allowed:
                    raise ClientError('tool not exposed')
                args = decode(call.arguments)
                validate_arguments(OPERATIONS[call.name][1], args)
                result = registry.dispatch(call.name, args)
                if rpc.closed:
                    raise ClientError('controller rejected request')
                messages.append({'role': 'tool', 'tool_call_id': call.id, 'content': result})
        raise ClientError('iteration limit')
    finally:
        client.close()


def main():
    # Duplicate the protocol FD, then redirect FD 1 itself: native writes and
    # import-time print statements cannot corrupt the controller's RPC stream.
    sys.stdout.flush()
    writer = os.fdopen(os.dup(1), 'wb', buffering=0)
    os.dup2(2, 1)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--discovery-only', action='store_true')
    options, unknown = parser.parse_known_args()
    if unknown:
        sys.stderr.write(json.dumps({'category': 'transport_error'}, separators=(',', ':')) + '\n')
        return 1
    try:
        run(StdioRPC(sys.stdin.buffer, writer), discovery_only=options.discovery_only)
        return 0
    except Exception as exc:
        category = exc.category if isinstance(exc, ClientError) else 'transport_error'
        sys.stderr.write(json.dumps({'category': category}, separators=(',', ':')) + '\n')
        return 1
    finally:
        writer.close()


if __name__ == '__main__':
    raise SystemExit(main())
