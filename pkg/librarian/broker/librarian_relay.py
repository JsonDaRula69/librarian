"""Controller-only JSON-line RPC for a no-network agent.

Wire v1: {"id": positive integer, "op": name, "params": exact object}.
Only controller source IDs and prepared artifact IDs cross acquisition APIs.
Discovery accepts purpose-free identity tokens and evidence source IDs, never
plans, URLs, paths, credentials, evidence objects or executable proposals.
This module neither launches Docker nor changes any runtime configuration.
"""
import json
import re


INFERENCE_DIAGNOSTIC_REASONS = frozenset({
    'prompt_budget_exhausted', 'call_budget_exhausted',
    'private_value_detected', 'invalid_prompt', 'output_budget_exhausted',
})


class RelayError(ValueError):
    pass


class RecoverableRelayError(RelayError):
    """A bounded tool precondition the model may correct and retry."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value)


class ControllerAdapter:
    """Projection over IntegratedController and optional ControllerIngestion."""

    def __init__(self, controller, ingestion=None, *, discovery=None, browser=None):
        self.controller = controller
        self.ingestion = ingestion
        if discovery is not None and discovery.controller is not controller:
            raise RelayError()
        self.discovery = discovery
        self.browser = browser
        self.artifacts = set()
        # Keep the controller's canonical ordering stable across bounded pages.
        self._artifact_ids = {}

    @property
    def sources(self):
        return frozenset(self.controller.plan)

    def catalog(self):
        if self.discovery is not None:
            return self.discovery.catalog()
        return {'sources': [{'source_id': i} for i in sorted(self.sources)],
                'components': [], 'proposals': []}

    def propose(self, kind, proposal):
        if self.discovery is None:
            raise RelayError()
        return self.discovery.propose(kind, proposal)

    def discovery_status(self):
        if self.discovery is None:
            return {'status': 'ready', 'unresolved_proposal_ids': []}
        return self.discovery.completion()

    def prepare(self, source_id):
        if not _identifier(source_id) or source_id not in self.sources or self.ingestion is None:
            raise RelayError()
        # An authorized plan is not an acquisition receipt. Let the client
        # acquire first, but never bypass a blocked or failed receipt.
        state = getattr(self.controller, '_state', None)
        receipts = state.get('sources') if isinstance(state, dict) else None
        if isinstance(receipts, dict):
            receipt = receipts.get(source_id)
            if receipt is None:
                raise RecoverableRelayError('acquire_source_first')
            if receipt.get('status') != 'complete':
                raise RecoverableRelayError('source_acquisition_blocked')
        ids = self._artifact_ids.get(source_id)
        if ids is None:
            artifacts = self.ingestion.prepare(source_id)
            ids = [item['id'] for item in artifacts]
            if any(not isinstance(i, str) or not re.fullmatch(r'[0-9a-f]{64}', i) for i in ids):
                raise RelayError()
            if len(ids) != len(set(ids)):
                raise RelayError()
            self._artifact_ids[source_id] = tuple(ids)
            self.artifacts.update(ids)
        return self._artifact_page(source_id, 0, 256)

    def _artifact_page(self, source_id, offset, limit):
        if source_id not in self._artifact_ids:
            raise RecoverableRelayError('prepare_source_first')
        ids = self._artifact_ids[source_id]
        end = min(offset + limit, len(ids))
        return {'artifact_ids': list(ids[offset:end]), 'total': len(ids),
                'next_offset': end if end < len(ids) else None}

    def artifact_list(self, source_id, offset, limit):
        if (not _identifier(source_id) or source_id not in self.sources
                or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 256):
            raise RelayError()
        return self._artifact_page(source_id, offset, limit)

    def ingestion_resume(self):
        if self.ingestion is None:
            raise RelayError()
        result = self.ingestion.resume()
        if (not isinstance(result, dict) or type(result.get('submitted')) is not int
                or type(result.get('checked')) is not int
                or type(result.get('has_more')) is not bool
                or not 0 <= result['submitted'] <= 256
                or not 0 <= result['checked'] <= 256):
            raise RelayError()
        return {key: result[key] for key in ('submitted', 'checked', 'has_more')}

    def discovery_fetch(self, url):
        if (self.browser is None or not isinstance(url, str)
                or len(url) > 2048 or not re.fullmatch(r'https://[^\s]+', url)):
            raise RelayError()
        try:
            result = self.browser.fetch(url)
        except Exception:
            raise RelayError() from None
        if (not isinstance(result, dict) or result.get('authority') != 'unverified_candidate'
                or not isinstance(result.get('receipt_id'), str)
                or not re.fullmatch(r'[0-9a-f]{32}', result['receipt_id'])):
            raise RelayError()
        return {key: value for key, value in result.items()
                if key in ('receipt_id', 'requested_url', 'final_url', 'http_status',
                           'sha256', 'redirects', 'authority', 'text_length', 'truncated')}

    def discovery_read(self, receipt_id, offset, limit):
        if (self.browser is None or not isinstance(receipt_id, str)
                or not re.fullmatch(r'[0-9a-f]{32}', receipt_id)
                or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 4096):
            raise RelayError()
        try:
            result = self.browser.read(receipt_id, offset, limit)
        except Exception:
            raise RelayError() from None
        if (not isinstance(result, dict) or result.get('receipt_id') != receipt_id
                or not isinstance(result.get('text'), str)):
            raise RelayError()
        return {'receipt_id': receipt_id, 'offset': offset, 'limit': limit,
                'text': result['text'], 'next_offset': result.get('next_offset'),
                'truncated': result.get('truncated')}

    def discovery_links(self, receipt_id, offset=0, limit=32):
        if (self.browser is None or not isinstance(receipt_id, str)
                or not re.fullmatch(r'[0-9a-f]{32}', receipt_id)
                or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 32):
            raise RelayError()
        try:
            all_links = self.browser.enumerate_links(receipt_id)
            links = all_links[offset:offset + limit]
        except Exception:
            raise RelayError() from None
        if not isinstance(links, list) or len(links) > 32:
            raise RelayError()
        total = len(all_links)
        end = min(offset + len(links), total)
        return {'receipt_id': receipt_id, 'links': links, 'total': total,
                'next_offset': end if end < total else None}

    def discovery_register_candidate(self, candidate_id, receipt_id, authority_receipt_id):
        """Register only receipt IDs; policy owns URL/plan derivation."""
        if (self.discovery is None or not _identifier(candidate_id)
                or not isinstance(receipt_id, str)
                or not re.fullmatch(r'[0-9a-f]{32}', receipt_id)
                or not isinstance(authority_receipt_id, str)
                or not re.fullmatch(r'[0-9a-f]{32}', authority_receipt_id)):
            raise RelayError()
        register = getattr(self.discovery.policy, 'register_candidate', None)
        if not callable(register):
            raise RelayError()
        try:
            result = register(candidate_id=candidate_id, receipt_id=receipt_id,
                              authority_receipt_id=authority_receipt_id)
        except Exception:
            raise RelayError() from None
        if not isinstance(result, dict):
            raise RelayError()
        # Never project policy plans or arbitrary controller data over the wire.
        allowed = ('candidate_id', 'receipt_id', 'authority_receipt_id',
                   'authority_origin', 'authority')
        if (set(result) - (set(allowed) | {'url'}) or result.get('candidate_id') != candidate_id
                or result.get('receipt_id') != receipt_id
                or result.get('authority_receipt_id') != authority_receipt_id
                or not isinstance(result.get('authority_origin'), str)
                or not re.fullmatch(r'https://[^/\s]+(?::[0-9]{1,5})?', result['authority_origin'])
                or result.get('authority') != 'seed->linked-receipt'):
            raise RelayError()
        return {key: result[key] for key in allowed if key in result}

    def artifact(self, operation, artifact_id):
        if (not isinstance(artifact_id, str) or artifact_id not in self.artifacts
                or self.ingestion is None):
            raise RelayError()
        if operation == 'artifact.submit':
            result = self.ingestion.submit(artifact_id)
        elif operation == 'artifact.poll':
            # Classify the controller precondition before calling poll. An absent
            # receipt is safe to submit; an existing unknown receipt is not.
            state = getattr(self.ingestion, 'state', None)
            receipts = state.get('receipts') if isinstance(state, dict) else None
            if isinstance(receipts, dict) and artifact_id not in receipts:
                raise RecoverableRelayError('submit_artifact_first')
            if (isinstance(receipts, dict) and artifact_id in receipts
                    and 'operation_id' not in receipts[artifact_id]):
                raise RecoverableRelayError('reconciliation_required')
            result = self.ingestion.poll(artifact_id)
        else:
            raise RelayError()
        status = result.get('status')
        if status not in ('unknown', 'accepted', 'pending', 'processing', 'completed', 'failed', 'cancelled'):
            raise RelayError()
        return {'status': status}

    def acquire(self, source_id):
        if not _identifier(source_id) or source_id not in self.sources:
            raise RelayError()
        result = self.controller.acquire(source_id)
        status = result.get('status')
        if status not in ('complete', 'blocked', 'failed'):
            raise RelayError()
        return {'status': status}


class InferenceProxy:
    """Trusted adapter receives a fixed route; it must enforce timeout/token cap.

    Adapter returns text only, never SDK objects, headers or provider errors.
    Supply all adapter-held private values for defense-in-depth output blocking.
    The proxy never reads environment, credentials or controller task context.
    """

    def __init__(self, route, adapter, *, max_calls=16, max_prompt_bytes=32768,
                 max_output_bytes=32768, max_output_tokens=1024, timeout=30,
                 private_values=()):
        if not isinstance(route, str) or not route or not callable(adapter):
            raise ValueError('invalid inference configuration')
        for value in (max_calls, max_prompt_bytes, max_output_bytes, max_output_tokens, timeout):
            if type(value) is not int or not 1 <= value <= 1048576:
                raise ValueError('invalid inference limit')
        if any(not isinstance(v, str) or not v for v in private_values):
            raise ValueError('invalid private values')
        self._route, self._adapter = route, adapter
        self._private = tuple(private_values) + (route,)
        self._remaining = max_calls
        self.max_prompt_bytes, self.max_output_bytes = max_prompt_bytes, max_output_bytes
        self.max_output_tokens, self.timeout = max_output_tokens, timeout
        self._diagnostic = {'reason': None, 'prompt_bytes': 0,
                            'output_bytes': 0, 'call_count': 0}

    def diagnostic_receipt(self):
        value = dict(self._diagnostic)
        reader = getattr(self._adapter, 'diagnostic_receipt', None)
        if callable(reader):
            try:
                adapter = reader()
                if isinstance(adapter, dict):
                    finish_reason = adapter.get('finish_reason')
                    if isinstance(finish_reason, str) and len(finish_reason) <= 64:
                        value['adapter_finish_reason'] = finish_reason
            except Exception:
                pass
        return value

    def complete(self, prompt):
        prompt_bytes = len(prompt.encode('utf-8')) if isinstance(prompt, str) else 0
        self._diagnostic['prompt_bytes'] = prompt_bytes
        if not isinstance(prompt, str) or not prompt:
            self._diagnostic['reason'] = 'invalid_prompt'
            raise RelayError()
        if prompt_bytes > self.max_prompt_bytes:
            self._diagnostic['reason'] = 'prompt_budget_exhausted'
            raise RelayError()
        if any(v in prompt for v in self._private):
            self._diagnostic['reason'] = 'private_value_detected'
            raise RelayError()
        if self._remaining == 0:
            self._diagnostic['reason'] = 'call_budget_exhausted'
            raise RelayError()
        self._remaining -= 1
        self._diagnostic['call_count'] += 1
        try:
            text = self._adapter(route=self._route, prompt=prompt,
                                 max_output_tokens=self.max_output_tokens, timeout=self.timeout)
        except Exception as exc:
            message = str(exc)
            self._diagnostic['reason'] = (
                'output_budget_exhausted' if message == 'output_budget_exhausted'
                else 'private_value_detected' if 'private' in message.lower()
                else 'invalid_prompt')
            raise RelayError() from None
        output_bytes = len(text.encode('utf-8')) if isinstance(text, str) else 0
        self._diagnostic['output_bytes'] = output_bytes
        if type(text) is not str or output_bytes > self.max_output_bytes:
            self._diagnostic['reason'] = 'output_budget_exhausted'
            raise RelayError()
        if any(v in text for v in self._private):
            self._diagnostic['reason'] = 'private_value_detected'
            raise RelayError()
        return {'text': text}


class RelaySession:
    """Single-owner incremental connection, permanently closed on budget failure."""

    def __init__(self, acquisition, *, inference=None, max_frame_bytes=65536,
                 max_requests=128, max_total_bytes=1048576):
        for value in (max_frame_bytes, max_requests, max_total_bytes):
            if type(value) is not int or not 1 <= value <= 16777216:
                raise ValueError('invalid relay limit')
        if max_frame_bytes < 128:
            raise ValueError('frame limit too small')
        self.acquisition, self.inference = acquisition, inference
        self.max_frame_bytes, self.max_requests = max_frame_bytes, max_requests
        self.max_total_bytes = max_total_bytes
        self._buffer = bytearray()
        self._requests = self._received = self._sent = 0
        self.closed = False

    def _abort(self):
        self.closed = True
        self._buffer.clear()
        raise RelayError('connection rejected')

    def feed(self, data):
        if self.closed or not isinstance(data, bytes):
            self._abort()
        self._received += len(data)
        if self._received > self.max_total_bytes:
            self._abort()
        output = bytearray()
        for part in data.splitlines(keepends=True):
            self._buffer.extend(part)
            if len(self._buffer) > self.max_frame_bytes:
                self._abort()
            if self._buffer.endswith(b'\n'):
                self._requests += 1
                if self._requests > self.max_requests:
                    self._abort()
                response = self._dispatch(bytes(self._buffer))
                self._buffer.clear()
                self._sent += len(response)
                if len(response) > self.max_frame_bytes or self._sent > self.max_total_bytes:
                    self._abort()
                output.extend(response)
        return bytes(output)

    def finish(self):
        if self._buffer:
            self._abort()
        self.closed = True
        if isinstance(self.acquisition, ControllerAdapter) and self.acquisition.discovery is not None:
            self.acquisition.discovery.assert_complete()

    def _dispatch(self, data):
        identifier = None
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise RelayError()
                    result[key] = value
                return result

            req = json.loads(data.decode('utf-8'), object_pairs_hook=unique)
            if type(req) is not dict or set(req) != {'id', 'op', 'params'}:
                raise RelayError()
            if type(req['params']) is not dict or type(req['op']) is not str:
                raise RelayError()
            identifier = req['id']
            if type(identifier) is not int or not 1 <= identifier <= 2147483647:
                identifier = None
                raise RelayError()
            if req['op'] in ('source.list', 'discovery.catalog') and not req['params']:
                result = self.acquisition.catalog()
            elif req['op'] in ('source.propose', 'component.propose'):
                result = self.acquisition.propose(req['op'].split('.')[0], req['params'])
            elif req['op'] == 'discovery.status' and not req['params']:
                result = self.acquisition.discovery_status()
            elif req['op'] == 'source.acquire' and set(req['params']) == {'source_id'}:
                result = self.acquisition.acquire(req['params']['source_id'])
            elif req['op'] == 'source.prepare' and set(req['params']) == {'source_id'}:
                result = self.acquisition.prepare(req['params']['source_id'])
            elif req['op'] == 'artifact.list' and set(req['params']) == {'source_id', 'offset', 'limit'}:
                result = self.acquisition.artifact_list(req['params']['source_id'],
                                                        req['params']['offset'], req['params']['limit'])
            elif req['op'] == 'ingestion.resume' and not req['params']:
                result = self.acquisition.ingestion_resume()
            elif req['op'] == 'discovery.fetch' and set(req['params']) == {'url'}:
                result = self.acquisition.discovery_fetch(req['params']['url'])
            elif req['op'] == 'discovery.read' and set(req['params']) == {'receipt_id', 'offset', 'limit'}:
                result = self.acquisition.discovery_read(req['params']['receipt_id'],
                                                         req['params']['offset'], req['params']['limit'])
            elif req['op'] == 'discovery.links' and set(req['params']) in ({'receipt_id'}, {'receipt_id', 'offset', 'limit'}):
                result = self.acquisition.discovery_links(req['params']['receipt_id'], req['params'].get('offset', 0), req['params'].get('limit', 32))
            elif req['op'] == 'discovery.register_candidate' and set(req['params']) == {
                    'candidate_id', 'receipt_id', 'authority_receipt_id'}:
                result = self.acquisition.discovery_register_candidate(
                    req['params']['candidate_id'], req['params']['receipt_id'],
                    req['params']['authority_receipt_id'])
            elif req['op'] in ('artifact.submit', 'artifact.poll') and set(req['params']) == {'artifact_id'}:
                result = self.acquisition.artifact(req['op'], req['params']['artifact_id'])
            elif req['op'] == 'inference.complete' and set(req['params']) == {'prompt'} and self.inference:
                result = self.inference.complete(req['params']['prompt'])
            else:
                raise RelayError()
            response = {'id': identifier, 'ok': True, 'result': result}
        except RecoverableRelayError as exc:
            response = {'id': identifier, 'ok': False, 'error': {'code': exc.code}}
        except Exception:
            response = {'id': identifier, 'ok': False, 'error': 'request_rejected'}
        return json.dumps(response, separators=(',', ':')).encode() + b'\n'


def serve_connection(session, agent_stdout, agent_stdin, *, timeout=300):
    """Service binary Popen pipes, including Docker -i --network=none pipes.

    Dedicated stdout is RPC only, never mixed with logs. Caller must concurrently
    drain/cap stderr, enforce a whole-process watchdog, close pipes, and remove
    the container on every exit. This loop bounds pipe IO, not synchronous trusted
    adapter execution: adapters MUST enforce their own network/process deadlines.

    Outstanding runtime integration: run_isolated currently uses one-shot stdin;
    its owner must wire this pump into a duplex launcher preserving preflight,
    stderr budgets, deadline and verified container cleanup. An in-image Hermes
    RPC client/provider adapter is also required. This is not enabled production.
    """
    import math
    import os
    import selectors
    import time

    if not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or not 0 < timeout <= 86400:
        raise ValueError('invalid connection timeout')
    read_fd, write_fd = agent_stdout.fileno(), agent_stdin.fileno()
    blocking = {fd: os.get_blocking(fd) for fd in (read_fd, write_fd)}
    pending = bytearray()
    eof = False
    deadline = time.monotonic() + timeout
    try:
        for fd in blocking:
            os.set_blocking(fd, False)
        with selectors.DefaultSelector() as selector:
            selector.register(read_fd, selectors.EVENT_READ)
            while not eof or pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RelayError('connection rejected')
                for key, _ in selector.select(remaining):
                    if key.fd == read_fd:
                        data = os.read(read_fd, min(4096, session.max_frame_bytes))
                        if not data:
                            eof = True
                            selector.unregister(read_fd)
                            session.finish()
                        else:
                            pending.extend(session.feed(data))
                            if pending:
                                selector.unregister(read_fd)
                                selector.register(write_fd, selectors.EVENT_WRITE)
                    else:
                        sent = os.write(write_fd, pending[:4096])
                        del pending[:sent]
                        if not pending:
                            selector.unregister(write_fd)
                            if not eof:
                                selector.register(read_fd, selectors.EVENT_READ)
    except (OSError, ValueError) as exc:
        session.closed = True
        raise RelayError('connection rejected') from None
    finally:
        for fd, state in blocking.items():
            os.set_blocking(fd, state)
