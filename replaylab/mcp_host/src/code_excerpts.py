"""Structural checks for explanatory code; no claim of compilation/semantic proof."""

import re


def _remaining(text):
    stack=[];state='code';quote='';i=0
    while i<len(text):
        c=text[i];n=text[i:i+2]
        if state=='line':
            if c=='\n':state='code'
        elif state=='block':
            if n=='*/':state='code';i+=1
        elif state=='string':
            if c=='\\':i+=1
            elif c==quote:state='code'
        elif n=='//':state='line';i+=1
        elif n=='/*':state='block';i+=1
        elif c in ('"',"'"):state='string';quote=c
        elif c in '([{':stack.append(c)
        elif c in ')]}':
            if not stack or stack.pop()!={')':'(',']':'[','}':'{'}[c]:
                raise ValueError('Code excerpt has a mismatched delimiter or starts inside a block; select a complete calculation')
        i+=1
    return stack or state in ('block','string')


def validate_fragment(text):
    if not text.strip() or _remaining(text):
        raise ValueError('Code excerpt is incomplete: close its functions, brackets, strings and comments before publishing')
    return text


def complete_source_excerpt(source,start,end,maximum_lines=120):
    lines=source.splitlines()
    if not 1<=start<=end<=len(lines):raise ValueError('Code range is outside the saved source')
    limit=min(len(lines),start+maximum_lines-1)
    while end<=limit:
        text='\n'.join(lines[start-1:end])
        if not _remaining(text):return text,end
        end+=1
    raise ValueError('The selected function/block exceeds the code-card line budget; choose a smaller complete calculation')


def presentation_excerpt(source, start, end, *, ranges=(), example='', mappings=()):
    """Keep sparse, exact source coordinates separate from explanatory display rows."""
    lines=source.splitlines()
    locations=[]
    if ranges:
        if example:raise ValueError('Choose sourceRanges or exampleCode, not both')
        selected=[];previous=0
        for span in ranges:
            a,b=span.startLine,span.endLine
            if not 1<=a<=b<=len(lines) or a<=previous:
                raise ValueError('Source ranges must be ordered, disjoint saved-source lines')
            if previous and a>previous+1:
                selected.append('// ****');locations.append(0)
            selected.extend(lines[a-1:b]);locations.extend(range(a,b+1));previous=b
        text='\n'.join(selected);end=previous
        validate_fragment(text)
    else:
        excerpt,end=complete_source_excerpt(source,start,end)
        text=validate_fragment(example) if example else excerpt
        locations=[0]*len(text.splitlines()) if example else list(range(start,end+1))
    if len(text)>16000 or len(text.splitlines())>120:
        raise ValueError('Code display exceeds 120 lines or 16000 characters; select key calculations')
    displayed=text.splitlines();targets=[];seen=set()
    for mapping in mappings:
        row,line=mapping.displayLine,mapping.sourceLine
        if not 1<=row<=len(displayed) or not 1<=line<=len(lines):
            raise ValueError('Code mapping is outside the displayed or saved source')
        key=(row,mapping.symbol)
        if key in seen:raise ValueError('Code mapping duplicates a display line/symbol')
        seen.add(key)
        target=dict(displayLine=row,sourceLine=line,column=1)
        if mapping.symbol:
            if not re.search(r'\b'+re.escape(mapping.symbol)+r'\b',displayed[row-1]):
                raise ValueError('Mapped symbol is absent from its display line')
            source_symbol=mapping.sourceSymbol or mapping.symbol
            match=re.search(r'\b'+re.escape(source_symbol)+r'\b',lines[line-1])
            if not match:raise ValueError('Mapped source symbol is absent from its saved-source line')
            target.update(symbol=mapping.symbol,sourceSymbol=source_symbol,column=match.start()+1)
        else:locations[row-1]=line
        targets.append(target)
    exact=[]
    for index,value in enumerate(displayed):
        if example and not locations[index] and value.strip() and '****' not in value:
            matches=[i+1 for i in range(start-1,end) if lines[i].strip()==value.strip()]
            if len(matches)==1:locations[index]=matches[0]
            else:
                mapped={target['sourceLine']for target in targets if target['displayLine']==index+1}
                if len(mapped)==1:locations[index]=mapped.pop()
        line=locations[index]
        exact.append(bool(line and lines[line-1].strip()==value.strip()))
    return dict(text=text,startLine=start,endLine=end,example=bool(example),lineNumbers=locations,
                exactSourceLines=exact,
                mappings=targets,structurallyChecked=True)
