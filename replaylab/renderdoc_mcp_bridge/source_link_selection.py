"""Exact UTF-8 selections for verified source citations, independent of Qt."""
import re


def selection_range(text, line, column=1, symbol='', select_line=False):
    lines=text.splitlines(keepends=True)
    if not 1<=line<=len(lines):return None
    row=lines[line-1].rstrip('\r\n')
    start=sum(len(v.encode('utf-8'))for v in lines[:line-1])
    if symbol:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}',symbol):return None
        matches=list(re.finditer(r'\b'+re.escape(symbol)+r'\b',row))
        if not matches:return None
        match=min(matches,key=lambda m:abs(m.start()-(column-1)))
        return start+len(row[:match.start()].encode('utf-8')),start+len(row[:match.end()].encode('utf-8'))
    if select_line:
        return start+len(row[:len(row)-len(row.lstrip())].encode('utf-8')),start+len(row.encode('utf-8'))
    return None
