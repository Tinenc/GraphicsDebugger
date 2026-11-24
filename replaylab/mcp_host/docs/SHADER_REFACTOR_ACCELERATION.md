# Shader refactoring latency and symbol ownership

Bread AI decides what a Shader calculation means, where a complete computation
begins and ends, and which descriptive names the evidence supports. Host tools
bind declarations, apply token edits, preserve the proposal ledger and validate
the operation. The editor presents source-bound navigation and progress. An
opened editor is not required for ACP, API or background Host work.

## Source-bound symbol views

`get_shader_workspace_outline(section="symbol-table")` pages global declarations.
Add `function="Name"` to inspect a single function's parameters, locals and
referenced globals. Rows include declaration identity, lexical scope, bound
reference counts, a safe rename selector when the binder can establish one,
simple read/write sites and direct-assignment input candidates. Every page
carries `sourceSha256`; a row from an older source revision must not be used
for a later edit. The editor's own index is a fast navigation cache, not a
second authority for Host renames or ABI.

Lexical visibility is **not** runtime liveness. A read at a call site may be
an `out` write, macros may alter bindings, and branches/loops may change which
definitions reach a use. The current table marks these cases uncertain. A
future precise dependency feature needs a function control-flow graph and
definition/use analysis, with an explicit unknown result when preprocessing or
aliasing prevents proof. It must not infer an old-to-new declaration identity
from matching names alone; organization receipts need to bind that relation
to both exact source revisions.

## Validation cost

For an ordinary whole-Shader organization, each bounded draft mutation gets
its own static binding/scope/structure check and durable source hash. The Agent
does not publish a function stage or recompile the entire candidate merely
because another reading window is complete. One final unscoped review runs
the required whole-source compiler, ABI and behavior checks against the exact
candidate before publication and replay replacement. A changed rejected
candidate is reviewed again after a concrete correction; unchanged validation
receipts are reused. An explicitly requested function-only result is the
exception and still validates the whole candidate before publication.

Independent evidence-backed renames should be submitted in bounded batches.
The Host's binding index, collision checks and token replacement handle the
mechanical work. The Agent should first simplify or extract calculations so it
does not spend transactions naming temporary values that will disappear.
Texture display names, CBuffer panel aliases and source identifiers have
separate evidence and receipts; lack of one does not stall independent source
work. Background final reviews support a bounded wait for their exact job ID,
so an Agent does not issue many unchanged status queries.

## Next performance boundary

Exact-source and operation-prefix caches make repeated reads and late-bound
renames cheap. A rename bound to the original baseline still invalidates every
subsequent structural stage. Before replacing the existing proof chain, profile
that path with a real saved session. A safe next design is a versioned function
index and proposal checkpoint: invalidate only edited functions and affected
callers, retain original-to-proposal proof receipts, and fall back to whole-file
analysis when global declarations, macros, interfaces or uncertain effects
change. Never treat a function-local preview as a successfully compiled or
replay-applied Shader.

Design references: [Language Server Protocol](https://microsoft.github.io/language-server-protocol/)
separates editor features from the language service; [Clang refactoring](https://clang.llvm.org/docs/RefactoringEngine.html)
uses source-bound occurrences and replacements; [Tree-sitter incremental
parsing](https://tree-sitter.github.io/tree-sitter/using-parsers/3-advanced-parsing.html)
shows how edited syntax trees can reuse unchanged structure; and [OpenAI's
long-horizon agent loop](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex)
uses durable state and real feedback rather than repeated context loading.
These are design references, not a claim that this Shader binder implements
their full language semantics.
