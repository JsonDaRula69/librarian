"""Purpose-free acquisition requests. Values are validated, never rewritten.

Schema 1: products have an id and version (use "unknown" when not known),
plus optional dependency_evidence references: kind, workspace-relative path,
and dependency_of (another requested product id). No excerpts or prose.
"""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath


_TOKEN = re.compile(r'[A-Za-z0-9@][A-Za-z0-9._@+:/-]{0,199}\Z')
_VERSION = re.compile(r'[A-Za-z0-9*][A-Za-z0-9._+*-]{0,99}\Z')



def _object(value, required: set[str], optional: set[str] | frozenset[str] = frozenset()) -> None:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise ValueError('request has missing or unknown fields; purpose, task and instructions are forbidden')


def _token(value, pattern, label: str) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f'invalid {label}: expected an identifier, not prose')
    if any(part in {'', '.', '..'} for part in value.split('/')):
        raise ValueError(f'invalid {label}: empty or traversal component')


def _evidence_path(value: object) -> None:
    """Accept a literal, safe workspace-relative POSIX path."""
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('invalid evidence path: expected a workspace-relative path')
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError('invalid evidence path: control characters are forbidden')
    if any(part in {'', '.', '..'} for part in value.split('/')):
        raise ValueError('invalid evidence path: empty or traversal component')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('invalid evidence path: absolute or traversal path')


def validate_request(value: dict) -> dict:
    """Return the unchanged JSON value after closed-schema validation."""
    _object(value, {'schema_version', 'products'})
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('request schema_version must be 1')
    products = value['products']
    if not isinstance(products, list) or not 1 <= len(products) <= 100:
        raise ValueError('request products must contain 1 to 100 entries')
    ids = set()
    for product in products:
        _object(product, {'id', 'version'}, {'dependency_evidence'})
        _token(product['id'], _TOKEN, 'product id')
        _token(product['version'], _VERSION, 'version')
        if product['id'] in ids:
            raise ValueError('duplicate product id')
        ids.add(product['id'])
    for product in products:
        evidence = product.get('dependency_evidence', [])
        if not isinstance(evidence, list) or len(evidence) > 100:
            raise ValueError('dependency_evidence must be a list of at most 100 references')
        for item in evidence:
            _object(item, {'kind', 'path', 'dependency_of'})
            if item['kind'] not in ('manifest', 'lockfile', 'configuration', 'import'):
                raise ValueError('invalid dependency evidence kind')
            _evidence_path(item['path'])
            _token(item['dependency_of'], _TOKEN, 'dependency_of')
            if item['dependency_of'] not in ids:
                raise ValueError('dependency_of must reference a requested product')
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('non-finite JSON constants are forbidden')


def parse_request(raw: str) -> dict:
    """Decode a structured request; legacy prose is intentionally unsupported."""
    if not isinstance(raw, str) or len(raw) > 1_000_000:
        raise ValueError('request must be a JSON string of at most 1000000 characters')
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError('request must be strict JSON; legacy prose tasks are not supported') from exc
    return validate_request(value)
