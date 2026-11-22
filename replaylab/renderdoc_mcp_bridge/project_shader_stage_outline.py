"""Source-bound semantic overview, independent of the function-flow graph.

An Agent-authored sidecar describes major responsibilities; structural parsing
only resolves source anchors and helper ownership. Legacy source gets a compact
provisional outline, never a required source-marker format or fixed checklist.
Compatible with qrenderdoc's Python 3.6 interpreter.
"""
from __future__ import print_function

import bisect
import hashlib
import re
import copy
import importlib.util
import os

try:
    from . import shader_structural_anchors as _structural
except (ImportError, ValueError):
    _spec = importlib.util.spec_from_file_location('_bread_outline_structural',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'shader_structural_anchors.py'))
    _structural = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_structural)


_STAGE = re.compile(r'^\s*//\s*@stage(?:\[([\w.:-]+)\])?\s+(.+?)\s*$')
_LEGACY_STAGE = re.compile(r'^\s*//\s*§\s*\d+\s+(.+?)\s*$')
_OVERVIEW = re.compile(r'^\s*//\s*@overview\s+([\w.:-]+?)\s*:\s+(.+?)\s*$')
_CONTROL = frozenset(('if', 'else', 'for', 'while', 'switch', 'do'))
_ARITHMETIC = frozenset(('+', '-', '*', '/', '%', '<<', '>>', '&', '|', '^',
                        '+=', '-=', '*=', '/=', '%=', '?'))
_TYPE = re.compile(r'^(?:(?:float|half|double|int|uint|bool)(?:[1-4](?:x[1-4])?)?'
                   r'|[biud]?vec[2-4]|[d]?mat[2-4](?:x[2-4])?)$')
_ANCHOR_TOKEN = re.compile(r"[A-Za-z_][A-Za-z_0-9]*|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[uUfFlL]*|::|->|==|!=|<=|>=|&&|\|\||\+\+|--|\+=|-=|\*=|/=|<<|>>|[^\s]")
_ANCHOR_COMMENTS = re.compile(r'//[^\r\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"')


def _anchor_tokens(source):
    """Canonical portable signature lexer; matches Host semantic_overview.py."""
    masked = list(source)
    for match in _ANCHOR_COMMENTS.finditer(source):
        for offset in range(match.start(), match.end()):
            if masked[offset] not in '\r\n':
                masked[offset] = ' '
    continuation, offset = False, 0
    for line in ''.join(masked).splitlines(True):
        if continuation or line.lstrip().startswith('#'):
            continuation = line.rstrip().endswith('\\')
            for position in range(offset, offset + len(line)):
                if masked[position] not in '\r\n':
                    masked[position] = ' '
        else:
            continuation = False
        offset += len(line)
    return [(match.group(), match.start()) for match in _ANCHOR_TOKEN.finditer(''.join(masked))]


def _signature(source):
    return ' '.join(value for value, _ in _anchor_tokens(source))


def prepare_navigation(index):
    cached = getattr(index, "_structural_navigation_functions", None)
    if cached is not None:
        return cached
    functions = []
    for record in index.functions.values():
        if record is None:
            continue
        start = index.tokens[record['start'] - 1].end()
        stop = index.tokens[record['end']].start()
        tokens = _anchor_tokens(index.source[start:stop])
        words = [word for word, _ in tokens]
        functions.append({'signature': _signature(record['signature']).split(' '),
                          'body': words, 'record': record, 'offset': start, 'tokens': tokens,
                          'hash': hashlib.sha256(' '.join(words).encode('utf-8')).hexdigest()})
    index._structural_navigation_functions = functions
    return functions


def reanchor_semantic_overview(index, overview):
    """Project navigation only. Never change a review's source hash or receipt."""
    projected = copy.deepcopy(overview)
    if not isinstance(projected, dict) or projected.get('version') != 1:
        return projected
    bodies = projected.get('navigationBodies', {})
    if not isinstance(bodies, dict) or len(bodies) > 512:
        bodies = {}
    functions = prepare_navigation(index)
    refreshed = {}

    def resolve(signature, old_body=None, body_hash=None):
        if not isinstance(signature, str) or not signature:
            return None
        if (isinstance(old_body, list) and len(old_body) <= _structural.MAX_TOKENS
                and all(isinstance(value, str) and 0 < len(value) <= 4096 for value in old_body)):
            if body_hash and hashlib.sha256(' '.join(old_body).encode('utf-8')).hexdigest() != body_hash:
                return None
            return _structural.function_match(signature.split(' '), old_body, functions)
        exact = [row for row in functions if ' '.join(row['signature']) == signature]
        if exact:
            return exact[0] if len(exact) == 1 else None
        # Legacy function anchors contain a body hash even without full tokens.
        # A unique unchanged body plus compatible signature supports a rename.
        matched = []
        old_signature = _structural.signature_parts(signature.split(' '))
        for row in functions:
            current_signature = _structural.signature_parts(row['signature'])
            if body_hash and row['hash'] == body_hash and old_signature and current_signature:
                if old_signature[1:] == current_signature[1:]:
                    matched.append(row)
        return matched[0] if len(matched) == 1 else None

    stages = projected.get('stages', [])
    if not isinstance(stages, list) or len(stages) > 256:
        return projected
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        anchors = stage.get('anchors', [])
        for anchor in anchors[:512] if isinstance(anchors, list) else []:
            if not isinstance(anchor, dict):
                continue
            if anchor.get('navigationUnresolved'):
                continue
            signature = anchor.get('functionSignature')
            old_body = bodies.get(anchor.get('functionBodySha256'), bodies.get(signature)) if isinstance(signature, str) else None
            row = resolve(signature, old_body, anchor.get('functionBodySha256'))
            if row is None:
                anchor['navigationUnresolved'] = True
                continue
            record, words = row['record'], row['body']
            if anchor.get('kind') == 'range':
                text = anchor.get('tokenSignature')
                needle = text.split(' ') if isinstance(text, str) and len(text) <= 16384 else []
                first, stop = anchor.get('tokenStart'), anchor.get('tokenEnd')
                unchanged = (row['hash'] == anchor.get('functionBodySha256') and type(first) is int
                             and type(stop) is int and 0 <= first < stop <= len(words)
                             and words[first:stop] == needle)
                if not unchanged:
                    # When statement history is available, verify lexical scope
                    # and declaration bindings even for an exact text match.
                    # A moved same-name statement in another block is not proof.
                    if isinstance(old_body, list) and _structural.supports_range(old_body, first, stop):
                        mapped = _structural.relocate_range(old_body, words, first, stop,
                                                           signature.split(' '), row['signature'])
                        if mapped is None:
                            anchor['navigationUnresolved'] = True
                            continue
                        first, stop = mapped['start'], mapped['end']
                        needle = words[first:stop]
                    matches = [at for at in range(len(words) - len(needle) + 1)
                               if needle and words[at:at + len(needle)] == needle]
                    if len(matches) == 1:
                        first, stop = matches[0], matches[0] + len(needle)
                    elif not matches and isinstance(old_body, list):
                        mapped = _structural.relocate_range(old_body, words, first, stop,
                                                           signature.split(' '), row['signature'])
                        if mapped is None:
                            anchor['navigationUnresolved'] = True
                            continue
                        first, stop = mapped['start'], mapped['end']
                    else:
                        anchor['navigationUnresolved'] = True
                        continue
                anchor.update(tokenStart=first, tokenEnd=stop, tokenSignature=' '.join(words[first:stop]),
                    sourceRange={'startLine': bisect.bisect_right(index.lines, row['offset'] + row['tokens'][first][1]),
                                 'endLine': bisect.bisect_right(index.lines, row['offset'] + row['tokens'][stop - 1][1])})
            elif anchor.get('kind') == 'function':
                first, stop = _span(index, record)
                anchor['sourceRange'] = {'startLine': first, 'endLine': stop}
            else:
                continue
            current_signature = ' '.join(row['signature'])
            anchor.update(functionName=record['name'], functionSignature=current_signature,
                          functionBodySha256=row['hash'], sourceSha256=index.source_sha256)
            if len(words) <= _structural.MAX_TOKENS:
                refreshed[row['hash']] = words
        helpers = stage.get('helperSignatures', [])
        if isinstance(helpers, list):
            for at, signature in enumerate(helpers[:512]):
                row = resolve(signature, bodies.get(signature) if isinstance(signature, str) else None)
                if row is not None:
                    helpers[at] = ' '.join(row['signature'])
                    if len(row['body']) <= _structural.MAX_TOKENS:
                        refreshed[helpers[at]] = row['body']
    if projected.get('sourceSha256') != index.source_sha256:
        projected['navigationSourceSha256'] = index.source_sha256
    # Keep bounded old evidence for unresolved anchors, rather than erasing it
    # while the user has temporarily incomplete syntax.
    needed = set()
    for stage in stages:
        if isinstance(stage, dict):
            needed.update(a.get('functionBodySha256') for a in stage.get('anchors', []) if isinstance(a, dict))
            needed.update(v for v in stage.get('helperSignatures', []) if isinstance(v, str))
    projected['navigationBodies'] = {key: value for key, value in dict(bodies, **refreshed).items() if key in needed}
    return projected


def resolve_semantic_overview(index, overview):
    """Resolve sidecar phases without treating old line numbers as authority.

    Function anchors survive body edits while their unique declaration remains.
    Range anchors survive line/comment edits and uniquely matching statements.
    Renamed, deleted or ambiguous anchors are reported instead of guessed.
    """
    overview = reanchor_semantic_overview(index, overview)
    result = {'rows': [], 'unresolved': []}
    if not isinstance(overview, dict) or overview.get('version') != 1:
        result['unresolved'].append({'reason': 'invalid-overview'})
        return result
    stages = overview.get('stages')
    if not isinstance(stages, list) or len(stages) > 256:
        result['unresolved'].append({'reason': 'invalid-stage-list'})
        return result
    by_signature, records = {}, {}
    for key, record in index.functions.items():
        if record is not None:
            records[key] = record
            by_signature.setdefault(_signature(record['signature']), []).append(record)
    ids, anchor_count = set(), 0
    for item in stages:
        identity = item.get('id') if isinstance(item, dict) else None
        title = item.get('title') if isinstance(item, dict) else None
        anchors = item.get('anchors') if isinstance(item, dict) else None
        if (not isinstance(identity, str) or not identity or len(identity) > 128 or identity in ids
                or not isinstance(title, str) or not title.strip() or len(title) > 96
                or not isinstance(anchors, list) or not anchors):
            result['unresolved'].append({'stageId': identity, 'reason': 'invalid-stage'})
            continue
        ids.add(identity)
        row = {'id': 'semantic:' + identity, 'semanticId': identity, 'title': title.strip(),
               'description': str(item.get('description') or '')[:1024], 'spans': [],
               'provenance': 'semantic-overview', 'anchorState': 'exact', 'helperFunctionIds': []}
        for helper in item.get('helperSignatures', []) if isinstance(item.get('helperSignatures', []), list) else []:
            matches = by_signature.get(helper, []) if isinstance(helper, str) else []
            if len(matches) == 1:
                row['helperFunctionIds'].append(matches[0]['id'])
            else:
                result['unresolved'].append({'stageId': identity, 'reason': 'helper-missing-or-ambiguous'})
        seen = set()
        for ordinal, anchor in enumerate(anchors):
            anchor_count += 1
            failure = {'stageId': identity, 'anchorIndex': ordinal, 'reason': 'invalid-anchor'}
            if not isinstance(anchor, dict) or anchor_count > 512:
                result['unresolved'].append(failure)
                continue
            if anchor.get('navigationUnresolved'):
                failure['reason'] = 'range-missing-or-ambiguous'
                result['unresolved'].append(failure)
                continue
            signature = anchor.get('functionSignature')
            candidates = by_signature.get(signature, []) if isinstance(signature, str) else []
            if len(candidates) != 1:
                failure['reason'] = 'function-missing-or-ambiguous'
                result['unresolved'].append(failure)
                continue
            record = candidates[0]
            body_start = index.tokens[record['start'] - 1].end()
            body_end = index.tokens[record['end']].start()
            tokens = _anchor_tokens(index.source[body_start:body_end])
            words = [value for value, _ in tokens]
            body_hash = hashlib.sha256(' '.join(words).encode('utf-8')).hexdigest()
            same_body = body_hash == anchor.get('functionBodySha256')
            if anchor.get('kind') == 'function':
                start, end = _span(index, record)
                position = index.tokens[record['declaration']].start()
            elif anchor.get('kind') == 'range':
                token_signature = anchor.get('tokenSignature')
                if not isinstance(token_signature, str) or not token_signature or len(token_signature) > 16384:
                    result['unresolved'].append(failure)
                    continue
                needle = token_signature.split(' ')
                first, stop = anchor.get('tokenStart'), anchor.get('tokenEnd')
                exact_offsets = (same_body and type(first) is int and type(stop) is int
                                 and 0 <= first < stop <= len(words) and words[first:stop] == needle)
                if not exact_offsets:
                    matches = [offset for offset in range(max(0, len(words) - len(needle) + 1))
                               if words[offset:offset + len(needle)] == needle]
                    if len(matches) != 1:
                        failure['reason'] = 'range-missing-or-ambiguous'
                        result['unresolved'].append(failure)
                        continue
                    first, stop = matches[0], matches[0] + len(needle)
                start = bisect.bisect_right(index.lines, body_start + tokens[first][1])
                end = bisect.bisect_right(index.lines, body_start + tokens[stop - 1][1])
                position = body_start + tokens[first][1]
            else:
                result['unresolved'].append(failure)
                continue
            column = position - index.lines[start - 1] + 1
            key = (record['id'], start, end, column)
            if key not in seen:
                seen.add(key)
                location = _location(index, record, start, end, _control_ranges(index, record))
                location['startColumn'] = column
                row['spans'].append(location)
            if not same_body or anchor.get('sourceSha256') != index.source_sha256 or overview.get('sourceSha256') != index.source_sha256:
                row['anchorState'] = 'reanchored'
        if row['spans']:
            duplicate = next((other for other in result['rows'] if other['title'] == row['title']
                and sorted((span['functionId'], span['startLine'], span['endLine'], span.get('startColumn', 1)) for span in other['spans'])
                == sorted((span['functionId'], span['startLine'], span['endLine'], span.get('startColumn', 1)) for span in row['spans'])), None)
            if duplicate is None:
                result['rows'].append(row)
            else:
                result['unresolved'].append({'stageId': identity, 'reason': 'duplicate-stage'})
    return result


def _line(index, position):
    return bisect.bisect_right(index.lines, index.tokens[position].start())


def _span(index, record):
    return _line(index, record['declaration']), _line(index, record['end'])


def _summary(description, fallback):
    return (re.split(r'[。；;\n]|(?<=\.)\s', str(description or ''), maxsplit=1)[0].strip()
            or fallback)[:96]


def _calls(index, record):
    result = []
    for position in range(record['start'], record['end'] - 1):
        if index.words[position + 1] != '(' or index.words[position - 1] in ('.', '::', '->'):
            continue
        target = index.function_identity(index.words[position])
        if target:
            end = index.parens.get(position + 1, position + 1)
            result.append({'functionId': target, 'startLine': _line(index, position),
                           'endLine': _line(index, end), 'token': position, 'endToken': end})
    return sorted(result, key=lambda item: item['endToken'])


def _control_ranges(index, record):
    """Keep conditional/loop ownership as code facts, not unconditional phases."""
    result = []
    for token in range(record['start'], record['end']):
        kind = index.words[token]
        if kind not in _CONTROL:
            continue
        body = token + 1
        if body in index.parens:
            body = index.parens[body] + 1
        if body >= record['end']:
            continue
        end = index.braces.get(body)
        if end is None:
            end = body
            while end < record['end'] and index.words[end] not in (';', '}'):
                end += 1
        result.append({'kind': kind, 'startLine': _line(index, token),
                       'endLine': _line(index, end), 'bodyLine': _line(index, body),
                       'expression': index.source[index.tokens[token].start():
                                                  index.tokens[body].start()].strip()[:256]})
    return result


def _location(index, record, start, end, controls):
    contexts = [dict(item) for item in controls if item['bodyLine'] <= start <= item['endLine']]
    return {'functionId': record['id'], 'functionName': record['name'],
            'startLine': start, 'endLine': end, 'line': start, 'contexts': contexts}


def _source_structure(index, entry_id):
    records = {key: value for key, value in index.functions.items() if value is not None}
    calls = {key: _calls(index, record) for key, record in records.items()}
    parents = {key: set() for key in records}
    paths, call_sites = {}, {key: [] for key in records}
    controls = {key: _control_ranges(index, record) for key, record in records.items()}
    for owner, items in calls.items():
        for item in items:
            parents[item['functionId']].add(owner)
            call_sites[item['functionId']].append(_location(
                index, records[owner], item['startLine'], item['endLine'], controls[owner]))

    def walk(key, path):
        if key in paths:
            return
        paths[key] = path
        for ordinal, call in enumerate(calls[key]):
            walk(call['functionId'], path + (call['endLine'], ordinal))
    walk(entry_id, ())
    parents = {key: owners.intersection(paths) for key, owners in parents.items()}
    call_sites = {key: [span for span in spans if span['functionId'] in paths]
                  for key, spans in call_sites.items()}
    return records, calls, parents, paths, call_sites, controls


def _annotated_rows(index, records, paths, controls):
    lines = index.source.splitlines()
    overview, markers = [], {}
    for number, text in enumerate(lines, 1):
        listed = _OVERVIEW.match(text)
        if listed:
            overview.append((listed.group(1), listed.group(2)))
            continue
        match = _STAGE.match(text)
        legacy = _LEGACY_STAGE.match(text) if not match else None
        if not match and not legacy:
            continue
        candidates = [record for key, record in records.items()
                      if key in paths and _span(index, record)[0] < number < _span(index, record)[1]]
        if not candidates and not legacy:
            # A marker directly attached to a definition can cover that function.
            # An unbound header/overview never invents a navigation location.
            candidates = [record for key, record in records.items() if key in paths
                          and _span(index, record)[0] > number
                          and all(not item.strip() or item.lstrip().startswith('//')
                                  for item in lines[number:_span(index, record)[0] - 1])]
            candidates.sort(key=lambda record: _span(index, record)[0])
            candidates = candidates[:1]
        if len(candidates) != 1:
            continue
        record = candidates[0]
        explicit_id = match.group(1) if match else None
        title = match.group(2) if match else legacy.group(1)
        markers.setdefault(record['id'], []).append((number, explicit_id, title))
    grouped = {}
    for function_id, values in markers.items():
        record = records[function_id]
        for ordinal, (line, explicit_id, title) in enumerate(values):
            start = max(line, _span(index, record)[0])
            end = values[ordinal + 1][0] - 1 if ordinal + 1 < len(values) else _span(index, record)[1]
            enclosing = [item['endLine'] for item in controls[function_id]
                         if item['bodyLine'] <= start <= item['endLine']]
            if enclosing:
                end = min([end] + enclosing)
            if end < start or not _has_calculation(index, record, start, end, transfers=True):
                continue
            identity = explicit_id or function_id + ':section:' + str(ordinal)
            row = grouped.setdefault(identity, {'id': 'stage:' + identity, 'annotationId': identity,
                'title': _summary(title, record['name']), 'description': title,
                'spans': [], 'provenance': 'source-annotation'})
            row['spans'].append(_location(index, record, start, end, controls[function_id]))
    # The independent overview is an ordered list of references to actual blocks.
    # It can group several functions under one calculation without changing code.
    ordered, used = [], set()
    for identity, title in overview:
        if identity in grouped and identity not in used:
            row = grouped[identity]
            row['title'] = _summary(title, row['title'])
            row['description'] = title
            ordered.append(row)
            used.add(identity)
    remaining = [row for identity, row in grouped.items() if identity not in used]
    remaining.sort(key=lambda row: min(paths[span['functionId']] + (span['startLine'],)
                                       for span in row['spans']))
    return ordered + remaining


def _has_calculation(index, record, start, end, excluded=(), transfers=False):
    for position in range(record['start'], record['end']):
        if not start <= _line(index, position) <= end:
            continue
        if any(begin <= position <= finish for begin, finish in excluded):
            continue
        word = index.words[position]
        if word in _ARITHMETIC:
            return True
        if transfers and word in ('=', 'return', 'discard'):
            return True
        if position + 1 < record['end'] and index.words[position + 1] == '(':
            if word not in _CONTROL and word not in ('return', 'sizeof') and not _TYPE.match(word):
                return True
    return False


def _complete_annotations(index, annotated, derived, records, calls, paths, controls):
    """A partly annotated edit must not hide the remaining source calculations."""
    covered, inherited = {}, set()
    for row in annotated:
        for span in row['spans']:
            covered.setdefault(span['functionId'], []).append((span['startLine'], span['endLine']))
            stack = [call['functionId'] for call in calls[span['functionId']]
                     if span['startLine'] <= call['startLine'] <= span['endLine']]
            while stack:
                key = stack.pop()
                if key in inherited:
                    continue
                inherited.add(key)
                stack.extend(call['functionId'] for call in calls[key])
    result = list(annotated)

    def position(row):
        return min(paths[span['functionId']] + (span['startLine'],) for span in row['spans'])

    for row in derived:
        spans = []
        for span in row['spans']:
            key = span['functionId']
            if key in inherited:
                continue
            fragments = [(span['startLine'], span['endLine'])]
            for begin, end in covered.get(key, []):
                fragments = [(left, right) for start, stop in fragments
                             for left, right in ((start, min(stop, begin - 1)),
                                                 (max(start, end + 1), stop)) if left <= right]
            for start, stop in fragments:
                # Annotation gaps use actual operations, not brace-only ranges
                # or otherwise unlabelled return/copy plumbing.
                if _has_calculation(index, records[key], start, stop,
                                    transfers=bool(row.get('description'))):
                    spans.append(_location(index, records[key], start, stop, controls[key]))
        if not spans:
            continue
        row['spans'] = spans
        insertion = next((ordinal for ordinal, other in enumerate(result)
                          if position(other) > position(row)), len(result))
        result.insert(insertion, row)
    return result


def _main_rows(index, entry_id, records, calls, controls):
    """Conservative major responsibilities when no reviewed sidecar is present.

    Unwrap pure interface plumbing, then stop at the entry's actual operations.
    Supporting helpers stay associated with their owning phase. An unsplit body
    is one provisional phase; ordinary comments do not invent an AI overview.
    """
    root, visited, wrapper_spans = entry_id, set(), []
    while root not in visited:
        visited.add(root)
        record, local_calls = records[root], calls[root]
        excluded = [(call['token'], call['endToken']) for call in local_calls]
        first, last = _span(index, record)
        targets = set(call['functionId'] for call in local_calls)
        conditional_calls = any(_location(index, record, call['startLine'], call['endLine'],
                                         controls[root])['contexts'] for call in local_calls)
        if (len(targets) != 1 or conditional_calls
                or _has_calculation(index, record, first, last, excluded)):
            break
        child = next(iter(targets))
        if child in visited:
            break
        wrapper_spans.extend(_location(index, record, call['startLine'], call['endLine'], controls[root])
                             for call in local_calls)
        root = child
    record, local_calls = records[root], calls[root]
    first, last = _span(index, record)
    excluded = [(call['token'], call['endToken']) for call in local_calls]
    # An entry that performs calculations itself is not merely a table of calls.
    # Its sidecar can split it semantically without inserting code markers.
    if not local_calls or _has_calculation(index, record, first, last, excluded):
        description = index.leading_comment(record['declaration'] - 1)
        return [{'id': 'main:' + root, 'title': _summary(description, record['name']),
                 'description': description, 'spans': [_location(index, record, first, last, controls[root])],
                 'callSpans': wrapper_spans,
                 'provenance': 'source-derived'}]
    result, grouped = [], {}
    for call in local_calls:
        target = records[call['functionId']]
        call_span = _location(index, record, call['startLine'], call['endLine'], controls[root])
        branch = tuple((item['kind'], item['startLine'], item['endLine']) for item in call_span['contexts'])
        identity = (target['id'], branch)
        if identity not in grouped:
            description = index.leading_comment(target['declaration'] - 1)
            start, end = _span(index, target)
            row = {'id': 'main:' + target['id'] + ':' + str(len(result)),
                   'title': _summary(description, target['name']), 'description': description,
                   'spans': [_location(index, target, start, end, controls[target['id']])],
                   'callSpans': list(wrapper_spans), 'provenance': 'source-derived'}
            grouped[identity] = row
            result.append(row)
        grouped[identity]['callSpans'].append(call_span)
    return result


def _compact_legacy_rows(rows):
    result, grouped = [], {}
    for row in rows:
        spans = row['spans']
        ownership = tuple(sorted((span['functionId'], tuple(
            (context['kind'], context['startLine'], context['endLine'])
            for context in span.get('contexts', []))) for span in spans))
        key = (row['title'], ownership)
        if key in grouped:
            grouped[key]['spans'].extend(spans)
        else:
            grouped[key] = row
            result.append(row)
    return result


def build_stage_outline(index, entry, overview=None):
    entry_id = index.function_identity(entry)
    if not entry_id:
        return []
    records, calls, parents, paths, call_sites, controls = _source_structure(index, entry_id)
    if overview is not None:
        # The sidecar can disambiguate an overload by its complete signature.
        # Include syntactically reachable overload candidates without selecting
        # one merely from its name; unresolved duplicate signatures stay absent.
        pending, visited = list(paths), set()
        while pending:
            owner = pending.pop()
            if owner in visited:
                continue
            visited.add(owner)
            record = records[owner]
            for position in range(record['start'], record['end'] - 1):
                if index.words[position + 1] != '(' or index.words[position - 1] in ('.', '::', '->'):
                    continue
                for target in index.by_name.get(index.words[position], []):
                    if target['id'] in records and target['id'] not in paths:
                        paths[target['id']] = paths[owner] + (_line(index, position),)
                        pending.append(target['id'])
        rows = resolve_semantic_overview(index, overview)['rows']
        for row in rows:
            row['spans'] = [span for span in row['spans'] if span['functionId'] in paths]
        rows = [row for row in rows if row['spans']]
    else:
        # Read old source annotations for compatibility; new organization writes
        # semantic-overview.json and never needs to add markers to Shader code.
        rows = _compact_legacy_rows(_annotated_rows(index, records, paths, controls))
    if not rows:
        rows = _main_rows(index, entry_id, records, calls, controls)
    elif overview is None:
        derived = _main_rows(index, entry_id, records, calls, controls)
        rows = _complete_annotations(index, rows, derived, records, calls, paths, controls)
    for ordinal, row in enumerate(rows):
        row['spans'].sort(key=lambda span: paths.get(span['functionId'], ()) + (span['startLine'],))
        primary = row['spans'][0]
        row.update(order=ordinal + 1, functionId=primary['functionId'], functionName=primary['functionName'],
                   line=primary['startLine'], sourceSha256=index.source_sha256,
                   functionIds=list(dict.fromkeys(span['functionId'] for span in row['spans'])))
        if not row.get('callSpans'):
            row.setdefault('callSpans', []).extend(
                dict(span) for key in row['functionIds'] for span in call_sites.get(key, []))
        unique_calls = {}
        for span in row['callSpans']:
            unique_calls.setdefault((span['functionId'], span['startLine'], span['endLine']), span)
        row['callSpans'] = list(unique_calls.values())
        reachable = set(row['functionIds'])
        reachable.update(key for key in row.get('helperFunctionIds', []) if key in paths)
        stack = [call['functionId'] for span in row['spans']
                 for call in calls.get(span['functionId'], [])
                 if span['startLine'] <= call['startLine'] <= span['endLine']]
        while stack:
            key = stack.pop()
            if key in reachable:
                continue
            reachable.add(key)
            stack.extend(call['functionId'] for call in calls.get(key, []))
        row['reachableFunctions'] = sorted(reachable)
    return rows


def outline_state(index, entry, overview=None, stages=None):
    """Distinguish semantic delivery from source-derived navigation only.

    Structural parsing must never promote a direct-call fallback to a reviewed
    calculation overview. Reanchoring preserves navigation, not its assessment.
    """
    rows = stages if stages is not None else build_stage_outline(index, entry, overview)
    state = {'state': 'provisional' if rows else 'missing', 'complete': False,
             'source': 'source-derived', 'stageCount': len(rows), 'unresolvedCount': 0}
    if not isinstance(overview, dict) or not overview.get('stages'):
        if any(row.get('provenance') == 'source-annotation' for row in rows):
            state['source'] = 'legacy-annotation'
        return state
    state['source'] = 'semantic-overview'
    resolved = resolve_semantic_overview(index, overview)
    state['unresolvedCount'] = len(resolved['unresolved'])
    hashes = (overview.get('sourceSha256'), overview.get('normalizedSourceSha256'))
    if index.source_sha256 not in hashes or state['unresolvedCount']:
        state['state'] = 'stale'
        return state
    review = overview.get('analysisReview')
    review = review if isinstance(review, dict) else {}
    if (review.get('schema') == 'bread-shader-semantic-overview-review/v1'
            and review.get('sourceSha256') == overview.get('sourceSha256')
            and review.get('author') == 'agent' and review.get('hostSemanticApproval') is False
            and review.get('scopeMappings') and review.get('granularity_evidence')
            and review.get('data_flow_evidence') and review.get('remainingWork') == []):
        state.update(state='reviewed', complete=True)
    return state


def stage_for_line(index, stages, line, preferred=None):
    """Prefer the smallest real calculation range, not a whole function bucket."""
    candidates = []
    for row in stages:
        matches = [span for span in row.get('spans', []) + row.get('callSpans', [])
                   if span['startLine'] <= line <= span['endLine']]
        if matches:
            candidates.append((min(span['endLine'] - span['startLine'] for span in matches), row))
    if candidates:
        smallest = min(size for size, row in candidates)
        rows = [row for size, row in candidates if size == smallest]
    else:
        owners = [key for key, record in index.functions.items() if record is not None
                  and _span(index, record)[0] <= line <= _span(index, record)[1]]
        rows = [row for row in stages if len(owners) == 1 and owners[0] in row['reachableFunctions']
                and owners[0] not in row['functionIds']]
    if preferred and any(row['id'] == preferred for row in rows):
        return preferred
    return rows[0]['id'] if rows else None


def navigation_span(index, stage, current_line=0):
    """A cross-function calculation opens the relevant span, then its function graph."""
    spans = stage.get('spans') or []
    containing = [span for span in spans if span['startLine'] <= current_line <= span['endLine']]
    if containing:
        return min(containing, key=lambda span: span['endLine'] - span['startLine'])
    owners = [key for key, record in index.functions.items() if record is not None
              and _span(index, record)[0] <= current_line <= _span(index, record)[1]]
    same_function = [span for span in spans if span['functionId'] in owners]
    return (same_function or spans or [None])[0]


__all__ = ['build_stage_outline', 'resolve_semantic_overview', 'outline_state', 'stage_for_line', 'navigation_span']
