"""Bounded inert document model, Python 3.6 compatible."""
import html
from html.parser import HTMLParser
from pathlib import Path
import re

from .vendor import mistune


def text_diagnostics(text):
    return {"replacementCharacters": text.count("\ufffd"),
            "questionMarkRuns": len(re.findall(r"\?{3,}", text)),
            "nonAsciiCharacters": sum(ord(c) > 127 for c in text)}


class CleanHTML(HTMLParser):
    tags = set("p h1 h2 h3 h4 h5 h6 div span pre code table thead tbody tr th td ul ol li blockquote a img hr br b strong i em del details summary".split())
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.output, self.blocked = [], 0
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "iframe", "object"):
            self.blocked += 1
        if self.blocked or tag not in self.tags:
            return
        safe = []
        for key, value in attrs:
            value = value or ""
            if key in ("href", "src") and not re.match(r"\s*(?:javascript|data)\s*:", value, re.I) and not (key == "src" and re.match(r"https?:", value, re.I)):
                safe.append('%s="%s"' % (key, html.escape(value, quote=True)))
            elif key in ("id", "name", "colspan", "rowspan", "title", "alt"):
                if key in ("colspan", "rowspan") and (not value.isdigit() or not 1 <= int(value) <= 128):
                    continue
                safe.append('%s="%s"' % (key, html.escape(value, quote=True)))
            elif key == "style" and len(value) <= 1024 and not re.search(r"url\s*\(|expression|@import|javascript",value,re.I):
                declarations = []
                for declaration in value.split(";"):
                    property_name = declaration.partition(":")[0].strip().lower()
                    if property_name in {"color","background-color","font-weight","font-size","font-family","text-align","border","padding","margin"}:
                        declarations.append(declaration)
                if declarations:
                    safe.append('style="%s"' % html.escape(";".join(declarations),quote=True))
        self.output.append("<%s%s>" % (tag, " " + " ".join(safe) if safe else ""))
    def handle_endtag(self, tag):
        if tag in ("script", "style", "iframe", "object"):
            self.blocked = max(0, self.blocked - 1)
        elif not self.blocked and tag in self.tags:
            self.output.append("</%s>" % tag)
    def handle_data(self, data):
        if not self.blocked:
            self.output.append(html.escape(data))


def flowchart(source):
    """Common Mermaid flowchart nodes/arrows, rendered by the native graph view.

    Full Mermaid scripting/styles are not evaluated. Unparsed syntax remains in
    the source tab and is reported rather than represented as a complete graph.
    """
    nodes, edges, skipped = {}, [], []
    node = r'([A-Za-z_][\w:-]*)(?:\s*[\[({]([\s\S]*?)[\])}])?'
    for line in re.split(r"[\n;]", source):
        line = line.strip()
        if not line or line.startswith(("graph ", "flowchart ", "%%")):
            continue
        if line.startswith(("subgraph ", "end", "style ", "classDef ", "class ", "click ")):
            skipped.append(line)
            continue
        parts = re.split(r'\s*(?:-->|==>|-\.->)(?:\|[^|]*\|)?\s*', line)
        ids = []
        for part in parts:
            match = re.fullmatch(node, part.strip())
            if not match:
                skipped.append(line)
                break
            key, label = match.groups()
            nodes.setdefault(key, {"id": key, "kind": "function", "label": (label or key).strip('"\''), "description": ""})
            if label:
                nodes[key]["label"] = label.strip('"\'')
            nodes[key]["description"] = key
            ids.append(key)
        else:
            edges.extend({"source": a, "target": b} for a, b in zip(ids, ids[1:]))
        if len(nodes) >= 256 or len(edges) >= 512:
            skipped.append("bounded graph preview")
            break
    return {"nodes": list(nodes.values())[:256], "edges": edges[:512]}, skipped


def load_document(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Document exceeds 8 MiB")
    encoding = "utf-8"
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gb18030")
        encoding = "gb18030"
    text = text.replace("\r\r\n", "\n").replace("\r\n", "\n")
    preview = text[:512 * 1024]
    truncated = len(preview) < len(text)
    diagrams = []
    def diagram(match):
        graph, skipped = flowchart(match.group(1))
        diagrams.append({"source": match.group(1), "graph": graph, "unparsedLines": skipped})
        return '\n\n[↗ %d](#diagram-%d)\n\n' % (len(diagrams), len(diagrams))
    preview = re.sub(r"```mermaid\s*\n([\s\S]*?)```", diagram, preview, flags=re.I)
    if path.suffix.lower() in (".md", ".markdown"):
        markup = mistune.create_markdown(escape=True, plugins=["table", "strikethrough", "task_lists"])(preview)
    elif path.suffix.lower() in (".html", ".htm"):
        markup = preview
    else:
        markup = "<pre>" + html.escape(preview) + "</pre>"
    clean = CleanHTML(); clean.feed(markup)
    return {"path": str(path), "text": text, "html": "".join(clean.output), "diagrams": diagrams,
            "encoding": encoding, "previewTruncated": truncated, "diagnostics": text_diagnostics(text)}
