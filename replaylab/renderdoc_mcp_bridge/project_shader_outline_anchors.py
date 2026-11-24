"""Session-only native positions for semantic directory anchors (Python 3.6).

The saved overview supplies annotations; optional worker-prepared navigation
checkpoints relocate their source positions when opening a document. Once an
anchor has a native identity, edits follow that identity instead of searching
for matching text. This module only projects a copy; it never writes a sidecar.
"""
from __future__ import absolute_import

import bisect
import copy
import hashlib
import importlib.util
import json
import os
import re

try:
    from . import project_shader_stage_outline as _outline
except (ImportError, ValueError):
    _spec = importlib.util.spec_from_file_location(
        "_bread_outline_anchor_tokens",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "project_shader_stage_outline.py"))
    _outline = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_outline)


class _SourcePositions(object):
    """Convert UTF-8 positions using only the non-ASCII character boundaries."""
    def __init__(self, source):
        self.source = source
        self.byte_length = len(source.encode('utf-8'))
        self.characters, self.byte_ends, self.extra = [], [], [0]
        for match in re.finditer(r'[^\x00-\x7f]', source):
            width = len(match.group().encode('utf-8'))
            self.characters.append(match.start())
            self.byte_ends.append(match.start() + self.extra[-1] + width)
            self.extra.append(self.extra[-1] + width - 1)

    def byte(self, position):
        return position + self.extra[bisect.bisect_left(self.characters, position)]

    def character(self, position):
        if type(position) is not int or not 0 <= position <= self.byte_length:
            return None
        result = position - self.extra[bisect.bisect_right(self.byte_ends, position)]
        if not 0 <= result <= len(self.source) or self.byte(result) != position:
            return None
        return result


class _CurrentSource(object):
    def __init__(self, hierarchy):
        self.index = hierarchy
        self.positions = _SourcePositions(hierarchy.source)
        self.records = [record for record in hierarchy.functions.values() if record is not None]
        self.signatures = {}
        self.record_signatures = {}
        self.bodies = {}
        for record in self.records:
            signature = _outline._signature(record['signature'])
            self.record_signatures[record['id']] = signature
            self.signatures.setdefault(signature, []).append(record)

    def function(self, signature):
        matches = self.signatures.get(signature, []) if isinstance(signature, str) else []
        return matches[0] if len(matches) == 1 else None

    def body(self, record):
        key = record['id']
        if key not in self.bodies:
            start = self.index.tokens[record['start'] - 1].end()
            end = self.index.tokens[record['end']].start()
            tokens = [(word, start + offset) for word, offset in
                      _outline._anchor_tokens(self.index.source[start:end])]
            words = [word for word, _ in tokens]
            self.bodies[key] = {'start': start, 'end': end, 'tokens': tokens,
                                'words': words, 'starts': [offset for _, offset in tokens],
                                'sha256': hashlib.sha256(' '.join(words).encode('utf-8')).hexdigest()}
        return self.bodies[key]

    def name_span(self, record):
        token = self.index.tokens[record['declaration']]
        return token.start(), token.end()

    def named_at(self, span):
        matches = [record for record in self.records if self.name_span(record) == span]
        return matches[0] if len(matches) == 1 else None

    def owner(self, span):
        start, end = span
        matches = [record for record in self.records
                   if self.index.tokens[record['start'] - 1].end() <= start < end
                   <= self.index.tokens[record['end']].start()]
        return matches[0] if len(matches) == 1 else None


def prepare(hierarchy):
    """Precompute pure source data on the existing hierarchy worker, without Qt.

    The immutable hierarchy owns this cache. Native range reads/creation still
    belong exclusively to ``update`` on the editor thread.
    """
    current = getattr(hierarchy, '_live_outline_anchor_source', None)
    if current is None:
        _outline.prepare_navigation(hierarchy)
        current = _CurrentSource(hierarchy)
        for record in current.records:
            current.body(record)
        hierarchy._live_outline_anchor_source = current
    return current


class LiveOutlineAnchors(object):
    """Project a durable overview through native editor ranges in this session.

    Call ``prepare(hierarchy)`` on the existing worker, then
    ``update(current_hierarchy, original_overview)`` on the editor thread.
    Pass the same original metadata again after edits, never its projection.
    ``None``/stale hierarchies defer initial binding. ``clear`` releases native
    ranges, and must be called on buffer reload or when closing the document.
    """
    def __init__(self, editor):
        self.editor = editor
        self.supported = all(callable(getattr(editor, name, None)) for name in
                             ('CreateRangeAnchor', 'GetRangeAnchorRange', 'RemoveRangeAnchor'))
        self._fingerprint = None
        self._bindings = {}
        self._owned = set()

    def clear(self):
        for handle in self._owned:
            self.editor.RemoveRangeAnchor(handle)
        self._owned.clear()
        self._bindings.clear()
        self._fingerprint = None

    def _create(self, current, span):
        handle = self.editor.CreateRangeAnchor(current.positions.byte(span[0]),
                                              current.positions.byte(span[1]))
        if type(handle) is not int or handle < 0:
            return None
        self._owned.add(handle)
        return handle

    def _span(self, current, handle):
        if handle is None:
            return None
        positions = list(self.editor.GetRangeAnchorRange(handle))
        if len(positions) != 2:
            return None
        first, stop = (current.positions.character(value) for value in positions)
        return (first, stop) if first is not None and stop is not None and first < stop else None

    def _bind(self, current, anchor):
        if anchor.get('navigationUnresolved'):
            return None
        kind = anchor.get('kind')
        record = current.function(anchor.get('functionSignature'))
        if record is None or kind not in ('function', 'range'):
            return None
        span = None
        if kind == 'range':
            signature = anchor.get('tokenSignature')
            if not isinstance(signature, str) or not signature or len(signature) > 16384:
                return None
            needle = signature.split(' ')
            body = current.body(record)
            words = body['words']
            matches = [offset for offset in range(max(0, len(words) - len(needle) + 1))
                       if words[offset:offset + len(needle)] == needle]
            # Opening a document requires a unique textual match; old line
            # numbers must never choose one of several identical statements.
            if len(matches) != 1:
                return None
            first, stop = matches[0], matches[0] + len(needle)
            tokens = body['tokens']
            span = (tokens[first][1], tokens[stop - 1][1] + len(tokens[stop - 1][0]))
        function_handle = self._create(current, current.name_span(record))
        if function_handle is None:
            return None
        range_handle = self._create(current, span) if span else None
        if span and range_handle is None:
            self.editor.RemoveRangeAnchor(function_handle)
            self._owned.remove(function_handle)
            return None
        return {'function': function_handle, 'range': range_handle, 'kind': kind}

    def _project(self, current, anchor, binding):
        if binding['kind'] == 'function':
            record = current.named_at(self._span(current, binding['function']))
            if record is None:
                return False
            first = current.index.tokens[record['declaration']].start()
            last = current.index.tokens[record['end']].start()
        else:
            span = self._span(current, binding['range'])
            record = current.owner(span) if span else None
            if record is None:
                return False
            body = current.body(record)
            tokens = body['tokens']
            start, stop = (bisect.bisect_left(body['starts'], value) for value in span)
            if start == stop:
                return False
            # Do not turn a partial operator/identifier into a neighboring
            # statement. Valid native lexical endpoints contain full tokens.
            if ((start and tokens[start - 1][1] + len(tokens[start - 1][0]) > span[0]) or
                    tokens[stop - 1][1] + len(tokens[stop - 1][0]) > span[1]):
                return False
            signature = ' '.join(body['words'][start:stop])
            if len(signature) > 16384:
                return False
            anchor.update(tokenStart=start, tokenEnd=stop, tokenSignature=signature)
            first, last = tokens[start][1], tokens[stop - 1][1]
        anchor.update(functionName=record['name'],
                      functionSignature=current.record_signatures[record['id']],
                      functionBodySha256=current.body(record)['sha256'],
                      sourceSha256=current.index.source_sha256,
                      sourceRange={'startLine': bisect.bisect_right(current.index.lines, first),
                                   'endLine': bisect.bisect_right(current.index.lines, last)})
        return True

    def update(self, hierarchy, overview, prepared=None):
        projected = copy.deepcopy(overview)
        if not self.supported:
            return projected
        try:
            encoded = json.dumps(overview, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        except (TypeError, ValueError):
            self.clear()
            return projected
        fingerprint = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        if fingerprint != self._fingerprint:
            self.clear()
            self._fingerprint = fingerprint
        if (not isinstance(projected, dict) or projected.get('version') != 1 or
                not isinstance(projected.get('stages'), list) or len(projected['stages']) > 256 or
                len(encoded.encode('utf-8')) > 256 * 1024):
            return projected
        current = None
        if hierarchy is not None and hierarchy.source == str(self.editor.GetText()):
            current = prepare(hierarchy)
            projected = copy.deepcopy(prepared) if isinstance(prepared, dict) else _outline.reanchor_semantic_overview(hierarchy, overview)
        count = 0
        for stage_index, stage in enumerate(projected['stages']):
            if not isinstance(stage, dict):
                continue
            anchors = stage.get('anchors', [])
            if isinstance(anchors, list):
                for ordinal, anchor in enumerate(anchors):
                    count += 1
                    if not isinstance(anchor, dict) or count > 512:
                        continue
                    key = ('anchor', stage_index, ordinal)
                    binding = self._bindings.get(key)
                    if binding is None and current is not None:
                        binding = self._bind(current, anchor)
                        if binding is not None:
                            self._bindings[key] = binding
                    if current is None or binding is None:
                        # No native identity has been established yet. Keep the
                        # old evidence for recovery after an incomplete edit or
                        # a later external file correction.
                        anchor['navigationUnresolved'] = True
                    elif not self._project(current, anchor, binding):
                        anchor['navigationUnresolved'] = True
                        # An empty signature uses the existing resolver's
                        # unresolved path without extending the sidecar schema.
                        # Keep the handle: only native Undo/move may revive it.
                        handle = binding['range'] if binding['kind'] == 'range' else binding['function']
                        if self._span(current, handle) is None:
                            anchor['functionSignature'] = ''
                    elif current is not None:
                        anchor.pop('navigationUnresolved', None)
                        signature = anchor['functionSignature']
                        record = current.function(signature)
                        if record is not None and len(current.body(record)['words']) <= 8192:
                            projected.setdefault('navigationBodies', {})[current.body(record)['sha256']] = current.body(record)['words']
            helpers = stage.get('helperSignatures')
            if isinstance(helpers, list):
                for ordinal, signature in enumerate(helpers[:512]):
                    key = ('helper', stage_index, ordinal)
                    binding = self._bindings.get(key)
                    if binding is None and current is not None:
                        binding = self._bind(current, {'kind': 'function', 'functionSignature': signature})
                        if binding is not None:
                            self._bindings[key] = binding
                    record = (current.named_at(self._span(current, binding['function']))
                              if current is not None and binding is not None else None)
                    helpers[ordinal] = current.record_signatures[record['id']] if record else ''
                    if record is not None and len(current.body(record)['words']) <= 8192:
                        projected.setdefault('navigationBodies', {})[helpers[ordinal]] = current.body(record)['words']
        return projected
