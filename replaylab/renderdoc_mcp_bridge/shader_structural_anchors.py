"""Bounded source-tree correspondence shared by debug and navigation anchors.

Pure Python 3.6, no compiler/GPU/Qt dependency. Inspired by syntax-aware tree
diffing: match statements before matching expression children, retain lexical
owners and declaration bindings, and never resolve ties by old line distance.
This is a deliberately conservative shader syntax subset, not a type checker.
Unsupported syntax leaves the existing lexical resolver in control.
"""
import collections
import functools
import re

MAX_TOKENS = 8192
MAX_STATEMENTS = 1024
MAX_DEPTH = 96
IDENTIFIER = re.compile(r'^[A-Za-z_]\w*$')
NUMBER = re.compile(r'^(?:\d|\.\d)')
ASSIGN = frozenset(('=', '+=', '-=', '*=', '/=', '%=', '&=', '|=', '^=', '<<=', '>>='))
QUALIFIERS = frozenset(('const', 'static', 'precise', 'in', 'out', 'inout', 'uniform',
                        'highp', 'mediump', 'lowp', 'volatile'))
PRECEDENCE = {'=': 1, '+=': 1, '-=': 1, '*=': 1, '/=': 1, '%=': 1,
              '&=': 1, '|=': 1, '^=': 1, '<<=': 1, '>>=': 1,
              '||': 3, '&&': 4, '|': 5, '^': 6, '&': 7, '==': 8, '!=': 8,
              '<': 9, '>': 9, '<=': 9, '>=': 9, '<<': 10, '>>': 10,
              '+': 11, '-': 11, '*': 12, '/': 12, '%': 12}


def normalized(words):
    return tuple('<number>' if NUMBER.match(word) else '<assign>' if word in ASSIGN else word
                 for word in words)


def signature_parts(signature):
    try:
        opening = signature.index('(')
        closing = len(signature) - 1 - signature[::-1].index(')')
        if opening < 1 or closing < opening:
            return None
        prefix = tuple(signature[:opening - 1])
        parameters, current, depth = [], [], 0
        for value in signature[opening + 1:closing] + [',']:
            if value == ',' and depth == 0:
                if current and current != ['void']:
                    # Only simple, explicitly typed parameters. Templates and
                    # semantics remain part of the type; no inferred overloads.
                    colon = current.index(':') if ':' in current else len(current)
                    head = current[:colon]
                    names = [i for i, word in enumerate(head) if IDENTIFIER.match(word)
                             and word not in QUALIFIERS]
                    if len(names) < 2:
                        return None
                    at = names[-1]
                    parameters.append((head[at], tuple(head[:at] + head[at + 1:] + current[colon:])))
                current = []
            else:
                current.append(value)
                depth += (value in ('(', '[', '<')) - (value in (')', ']', '>'))
        return signature[opening - 1], prefix, parameters, tuple(signature[closing + 1:])
    except (ValueError, TypeError):
        return None


class Unsupported(ValueError):
    pass


class Node(object):
    def __init__(self, kind, label, start, end, children=()):
        self.kind, self.label, self.start, self.end = kind, label, start, end
        self.children = tuple(children)

    def walk(self, path=()):
        yield path, self
        for role, child in self.children:
            for item in child.walk(path + (role,)):
                yield item


class Expression(object):
    def __init__(self, words, start, end):
        self.words, self.at, self.end = words, start, end

    def take(self, expected=None):
        if self.at >= self.end or (expected is not None and self.words[self.at] != expected):
            raise Unsupported('incomplete expression')
        value = self.words[self.at]
        self.at += 1
        return value

    def parse(self, minimum=0, depth=0):
        if depth > MAX_DEPTH:
            raise Unsupported('expression depth')
        start = self.at
        value = self.take()
        if value in ('+', '-', '!', '~', '++', '--'):
            child = self.parse(13, depth + 1)
            node = Node('unary', value, start, child.end, (('operand', child),))
        elif value == '(':
            child = self.parse(0, depth + 1)
            self.take(')')
            node = Node('group', '', start, self.at, (('value', child),))
        elif IDENTIFIER.match(value) or NUMBER.match(value):
            node = Node('identifier' if IDENTIFIER.match(value) else 'number', value, start, self.at)
        else:
            raise Unsupported('expression atom')
        while self.at < self.end:
            operator = self.words[self.at]
            if operator == '(':
                self.take()
                children, ordinal = [('callee', node)], 0
                while self.at < self.end and self.words[self.at] != ')':
                    children.append(('argument:%d' % ordinal, self.parse(0, depth + 1)))
                    ordinal += 1
                    if self.at < self.end and self.words[self.at] == ',':
                        self.take()
                    else:
                        break
                self.take(')')
                node = Node('call', '', node.start, self.at, children)
            elif operator == '[':
                self.take()
                child = self.parse(0, depth + 1)
                self.take(']')
                node = Node('index', '', node.start, self.at, (('base', node), ('index', child)))
            elif operator == '.':
                self.take()
                at, name = self.at, self.take()
                if not IDENTIFIER.match(name):
                    raise Unsupported('member name')
                member = Node('member-name', name, at, self.at)
                node = Node('member', '', node.start, self.at, (('base', node), ('member', member)))
            elif operator in ('++', '--'):
                self.take()
                node = Node('postfix', operator, node.start, self.at, (('operand', node),))
            elif operator == '?' and minimum <= 2:
                self.take()
                yes = self.parse(0, depth + 1)
                self.take(':')
                no = self.parse(2, depth + 1)
                node = Node('conditional', '', node.start, no.end,
                            (('condition', node), ('true', yes), ('false', no)))
            elif operator in PRECEDENCE and PRECEDENCE[operator] >= minimum:
                self.take()
                right = self.parse(PRECEDENCE[operator] + (operator not in ASSIGN), depth + 1)
                node = Node('assignment' if operator in ASSIGN else 'binary', operator,
                            node.start, right.end, (('left', node), ('right', right)))
            else:
                break
        return node


def statement_tree(words, start, end):
    stop = end - (end > start and words[end - 1] == ';')
    first = start
    while first < stop and words[first] in QUALIFIERS:
        first += 1
    declaration = (first + 1 < stop and IDENTIFIER.match(words[first])
                   and IDENTIFIER.match(words[first + 1]) and words[first] not in
                   ('return', 'break', 'continue', 'discard', 'else'))
    if declaration:
        name_at = first + 1
        name = Node('declaration-name', words[name_at], name_at, name_at + 1)
        children = [('name', name)]
        cursor = name_at + 1
        if cursor < stop:
            if words[cursor] != '=':
                raise Unsupported('complex declarator')
            parser = Expression(words, cursor + 1, stop)
            children.append(('initializer', parser.parse()))
            if parser.at != stop:
                raise Unsupported('declarator tail')
        return Node('declaration', tuple(words[start:name_at]), start, end, children)
    if first < stop and words[first] == 'return':
        parser = Expression(words, first + 1, stop)
        node = parser.parse()
        if parser.at != stop:
            raise Unsupported('return tail')
        return Node('return', '', start, end, (('value', node),))
    parser = Expression(words, start, stop)
    node = parser.parse()
    if parser.at != stop:
        raise Unsupported('statement tail')
    return Node('statement', '', start, end, (('value', node),))


@functools.lru_cache(maxsize=4)
def statements(words):
    if len(words) > MAX_TOKENS:
        raise Unsupported('function size')
    result, scope, stack, begin, depth = [], (), [], 0, 0
    for at, word in enumerate(words):
        if word in ('(', '['):
            depth += 1
        elif word in (')', ']'):
            depth -= 1
            if depth < 0:
                raise Unsupported('delimiter')
        elif depth == 0 and word == '{':
            header = tuple(words[begin:at])
            stack.append(scope)
            scope = scope + (header,)
            begin = at + 1
        elif depth == 0 and word == '}':
            if not stack or begin != at:
                raise Unsupported('block')
            scope = stack.pop()
            begin = at + 1
        elif depth == 0 and word == ';':
            try:
                tree = statement_tree(words, begin, at + 1)
            except Unsupported:
                tree = None
            result.append((begin, at + 1, scope, tree))
            if len(result) > MAX_STATEMENTS:
                raise Unsupported('statement count')
            begin = at + 1
    if depth or stack or begin != len(words):
        raise Unsupported('incomplete function')
    return tuple(result)


def renamed(words, mapping):
    return tuple(mapping.get(word, word) if (not at or words[at - 1] != '.')
                 and (at + 1 >= len(words) or words[at + 1] != '(') else word
                 for at, word in enumerate(words))


def rename_map(old, new, old_signature=(), new_signature=()):
    """Only accept declaration-proven, one-to-one renames without shadowing."""
    mapping = {}
    before, after = signature_parts(list(old_signature)), signature_parts(list(new_signature))
    if before and after:
        if before[1] != after[1] or before[3] != after[3] or [v[1] for v in before[2]] != [v[1] for v in after[2]]:
            return None
        for (name, _), (replacement, _) in zip(before[2], after[2]):
            if name != replacement:
                if name in new or replacement in old:
                    return None
                mapping[name] = replacement
    old_decls, new_decls = {}, {}
    for words, target in ((old, old_decls), (new, new_decls)):
        for start, end, scope, tree in statements(tuple(words)):
            if tree and tree.kind == 'declaration':
                name = tree.children[0][1].label
                target.setdefault(name, []).append((tree, scope))
    for name in mapping:
        if name in old_decls or mapping[name] in new_decls:
            return None
    for name, declarations in old_decls.items():
        if name in new or len(declarations) != 1:
            continue
        tree, scope = declarations[0]
        if len(tree.children) < 2:
            continue
        init = tree.children[1][1]
        expected = normalized(renamed(old[init.start:init.end], mapping))
        candidates = []
        for other, bindings in new_decls.items():
            if other in old or len(bindings) != 1:
                continue
            candidate, candidate_scope = bindings[0]
            if candidate.label != tree.label or candidate_scope != scope or len(candidate.children) < 2:
                continue
            value = candidate.children[1][1]
            if normalized(new[value.start:value.end]) == expected:
                candidates.append(other)
        if len(candidates) == 1 and old.count(name) >= 2 and new.count(candidates[0]) >= 2:
            mapping[name] = candidates[0]
    if len(set(mapping.values())) != len(mapping):
        return None
    return mapping


def _scope(scope, mapping):
    return tuple(normalized(renamed(header, mapping)) for header in scope)


def _key(words, row):
    tree = row[3]
    if tree is None:
        return None
    if tree.kind == 'declaration':
        return ('declaration', tree.label, tree.children[0][1].label)
    value = tree.children[0][1] if tree.children else tree
    if value.kind == 'assignment':
        left = value.children[0][1]
        return ('assignment', tuple(words[left.start:left.end]))
    if tree.kind == 'return':
        return ('return',)
    return None


def _binding(words, row, node, params):
    names = {child.label for _, child in node.walk() if child.kind in ('identifier', 'declaration-name')
             and (child.end >= len(words) or words[child.end] != '(')}
    # Calls and member names are not local declarations. Unbound names must
    # remain spelled identically; type inference is not fabricated here.
    result = {}
    for name in names:
        found = []
        for start, end, scope, tree in statements(tuple(words)):
            if start <= node.start and tree is None and name in words[start:end] and row[2][:len(scope)] == scope:
                return None
            if start > node.start or not tree or tree.kind != 'declaration':
                continue
            if tree.children[0][1].label != name or row[2][:len(scope)] != scope:
                continue
            found.append((len(scope), tree.label, scope))
        if found:
            deepest = max(value[0] for value in found)
            candidates = [value[1:] for value in found if value[0] == deepest]
            if len(candidates) != 1:
                return None
            result[name] = candidates[0]
        else:
            result[name] = ('parameter', params[name]) if name in params else ('external', name)
    return result


def _same_role(before, after, path):
    """A repeated operand needs unchanged ancestor roles, callee and arity."""
    def shape(node):
        return node.kind, node.label, tuple((role, shape(child)) for role, child in node.children)

    for role in path:
        if before.kind != after.kind or normalized((str(before.label),)) != normalized((str(after.label),)):
            return False
        if before.kind == 'call':
            if len(before.children) != len(after.children) or shape(before.children[0][1]) != shape(after.children[0][1]):
                return False
        left, right = dict(before.children).get(role), dict(after.children).get(role)
        if left is None or right is None:
            return False
        before, after = left, right
    return True


def relocate(old, new, start, end, old_signature=(), new_signature=(), whole_statement=False):
    """Return a unique current token range, or None. No distance-based guesses."""
    try:
        return _relocate(tuple(old), tuple(new), start, end, old_signature, new_signature, whole_statement)
    except (Unsupported, RecursionError, TypeError, ValueError, IndexError):
        return None


def _relocate(old, new, start, end, old_signature, new_signature, whole_statement):
    if not (type(start) is int and type(end) is int and 0 <= start < end <= len(old)):
        return None
    mapping = rename_map(old, new, old_signature, new_signature)
    if mapping is None:
        return None
    translated = renamed(old, mapping)
    old_rows, new_rows = statements(translated), statements(new)
    owners = [row for row in old_rows if row[0] <= start < end <= row[1] and row[3]]
    if len(owners) != 1:
        return None
    owner = owners[0]
    expected_scope = _scope(owner[2], {})
    candidates = [row for row in new_rows if row[3] and _scope(row[2], {}) == expected_scope]
    shape = normalized(translated[owner[0]:owner[1]])
    matches = [row for row in candidates if normalized(new[row[0]:row[1]]) == shape]
    old_matches = [row for row in old_rows if _scope(row[2], {}) == expected_scope
                   and normalized(translated[row[0]:row[1]]) == shape]
    if len(matches) > 1 or len(old_matches) > 1:
        return None
    evidence = 'statement-structure'
    if not matches:
        key = _key(translated, owner)
        matches = [row for row in candidates if key is not None and _key(new, row) == key]
        if len(matches) != 1:
            return None
        # A changed statement needs an independently unchanged neighbor. This
        # is not an ordinal fallback: it may move, and neighbors must be unique.
        selected = matches[0]
        old_at, new_at = old_rows.index(owner), new_rows.index(selected)
        witnesses = 0
        for offset in (-1, 1):
            if 0 <= old_at + offset < len(old_rows) and 0 <= new_at + offset < len(new_rows):
                left, right = old_rows[old_at + offset], new_rows[new_at + offset]
                tokens = tuple(translated[left[0]:left[1]])
                if tokens == tuple(new[right[0]:right[1]]) and tokens and sum(
                        tuple(new[r[0]:r[1]]) == tokens for r in new_rows) == 1:
                    witnesses += 1
        old_key_matches = [row for row in old_rows if _scope(row[2], {}) == expected_scope
                           and _key(translated, row) == key]
        if not witnesses and (key != ('return',) or len(old_key_matches) != 1):
            return None
        evidence = 'statement-context'
    if len(matches) != 1:
        return None
    target = matches[0]
    before, after = signature_parts(list(old_signature)), signature_parts(list(new_signature))
    old_params = {mapping.get(name, name): typ for name, typ in before[2]} if before else {}
    new_params = dict(after[2]) if after else {}
    if whole_statement and (start, end) == owner[:2]:
        old_binding = _binding(translated, owner, owner[3], old_params)
        if old_binding is None or old_binding != _binding(new, target, target[3], new_params):
            return None
        return {'start': target[0], 'end': target[1], 'renames': mapping, 'evidence': evidence}
    selected = [(path, node) for path, node in owner[3].walk() if (node.start, node.end) == (start, end)]
    if len(selected) != 1:
        return None
    path, node = selected[0]
    nodes = list(target[3].walk())
    exact = [candidate for candidate_path, candidate in nodes if candidate.kind == node.kind
             and new[candidate.start:candidate.end] == translated[start:end]]
    same_path = [candidate for candidate_path, candidate in nodes if candidate_path == path and candidate.kind == node.kind]
    if len(exact) == 1:
        candidate = exact[0]
    elif len(same_path) == 1 and (len(exact) > 1 and _same_role(owner[3], target[3], path)
                                or not exact and node.kind not in ('identifier', 'declaration-name', 'member-name')):
        candidate = same_path[0]
        if len(exact) > 1:
            evidence = 'expression-role'
    else:
        return None
    if node.kind == 'member' and [n.label for _, n in node.walk() if n.kind == 'member-name'] != [
            n.label for _, n in candidate.walk() if n.kind == 'member-name']:
        return None
    original_binding = _binding(translated, owner, node, old_params)
    current_binding = _binding(new, target, candidate, new_params)
    if original_binding is None or original_binding != current_binding:
        return None
    return {'start': candidate.start, 'end': candidate.end, 'renames': mapping, 'evidence': evidence}


def function_match(signature, body, functions):
    """Resolve exact signatures first, then unique declaration-consistent renames."""
    exact = [row for row in functions if list(row['signature']) == list(signature)]
    if exact:
        return exact[0] if len(exact) == 1 else None
    if len(functions) > 256:
        return None
    matches = []
    for row in functions:
        try:
            mapping = rename_map(tuple(body), tuple(row['body']), signature, row['signature'])
            if mapping is not None and normalized(renamed(body, mapping)) == normalized(row['body']):
                matches.append(row)
        except (Unsupported, RecursionError, TypeError, ValueError, IndexError):
            continue
    return matches[0] if len(matches) == 1 else None


def relocate_range(old, new, start, end, old_signature=(), new_signature=()):
    """Map a directory annotation spanning one or more complete statements."""
    direct = relocate(old, new, start, end, old_signature, new_signature, True)
    if direct is not None:
        return direct
    try:
        rows = [row for row in statements(tuple(old)) if start <= row[0] < row[1] <= end]
        if not rows or len(rows) > 128 or rows[0][0] != start or rows[-1][1] != end:
            return None
        mapped = [relocate(old, new, row[0], row[1], old_signature, new_signature, True) for row in rows]
        if any(row is None for row in mapped) or any(left['end'] > right['start'] for left, right in zip(mapped, mapped[1:])):
            return None
        return {'start': mapped[0]['start'], 'end': mapped[-1]['end'], 'evidence': 'statement-sequence'}
    except (Unsupported, TypeError, ValueError):
        return None


def supports_range(words, start, end):
    """Whether a complete range has parsed statements for binding validation."""
    try:
        if type(start) is not int or type(end) is not int:
            return False
        rows = [row for row in statements(tuple(words)) if start <= row[0] < row[1] <= end]
        return bool(rows and rows[0][0] == start and rows[-1][1] == end
                    and all(row[3] is not None for row in rows))
    except (Unsupported, RecursionError, TypeError, ValueError):
        return False
