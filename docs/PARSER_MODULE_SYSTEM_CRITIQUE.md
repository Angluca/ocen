# Parser — Module / Import system: critique & refactor recommendations ⚙️

Summary
-------
- The module/import logic in `compiler/parser.oc` mixes parsing, filesystem I/O, project detection, namespace-graph mutation and library resolution in one place. This makes the code hard to reason about, brittle to change, and difficult to unit-test while preserving end-to-end behavior.
- Below I give a targeted, actionable redesign that preserves existing e2e semantics while moving toward a much cleaner, testable, and maintainable architecture.

Quick findings (high level)
---------------------------
- Single Responsibility violation: `Parser` both *parses* and *resolves/loads* imports (e.g. `load_single_import_part`, `try_load_mod_for_namespace`, `import_external_lib`, `create_namespaces_for_initial_file`).
- Fragile heuristics: project-vs-standalone detection and `main.oc`/`mod.oc` special-cases are ad-hoc and duplicated across code paths.
- Side effects during parse: parser performs filesystem I/O and mutates `Namespace` graph while parsing (hard to unit-test / leads to ordering issues & cycles).
- Duplication: stdlib/library resolution logic is spread across several functions (`import_external_lib`, `find_or_import_stdlib`, `include_prelude_only`).
- Complex and overloaded import grammar: many syntactic forms map to ad-hoc internal semantics (Global, Project, Parent, Current) — resolution is scattered and fragile.

Where to look (important symbols & locations)
---------------------------------------------
- Parsing & import AST: `Parser::parse_import`, `Parser::parse_import_path` (`compiler/parser.oc`).
- Import resolution & file loading: `Parser::load_import_path`, `Parser::load_import_path_from_base`, `Parser::load_single_import_part`.
- Project detection / namespace bootstrapping: `Parser::create_namespaces_for_initial_file`.
- Library & stdlib handling: `Parser::import_external_lib`, `Parser::find_or_import_stdlib`, `Parser::include_prelude_only`.
- Side-effect helpers: `Parser::try_load_mod_for_namespace`, `Parser::load_file`.

Problems (detailed)
-------------------
1) Parser responsibilities are entangled
   - Parsing code should be deterministic, side-effect free (w.r.t. FS and global symbol graph). Right now it performs file loads, filesystem checks, and creates/inserts `Namespace` entries during parsing.
   - Consequence: tests must replicate FS state, circular import handling is implicit and fragile, and refactors risk breaking runtime ordering.

2) Fragile project/standalone detection logic
   - `create_namespaces_for_initial_file` walks up the filesystem looking for `main.oc`, and has many special-cases (stdlib, `mod.oc`, `single_file` flag). This heuristic is brittle and duplicated.
   - No public API / CLI flag to explicitly mark project vs standalone; developers cannot easily opt into deterministic behavior.

3) Import grammar vs. semantic mapping is unclear
   - Multiple import forms (absolute vs project vs current vs parent) are parsed and resolved inline with ad-hoc semantics.
   - `parse_import_path` returns `ImportPart`s, but resolution logic lives in `load_import_path_from_base` which mixes filesystem, namespace lookup, and module loading. This separation is leaky.

4) I/O and namespace mutation during parse
   - `try_load_mod_for_namespace`, `load_single_import_part` and `load_file` create namespaces and parse additional files while the initial file is still being parsed.
   - This makes the *parse phase* non-idempotent and couples parsing order to filesystem reads.

5) Duplicated stdlib/external lib handling
   - stdlib and library discovery/registration are sprinkled in `Parser`. There is no single authoritative `LibraryRegistry` or `ModuleIndex`.

6) Partial/inconsistent namespace initialization
   - `Namespace`/`Symbol` objects are sometimes created in a partially-initialized state (spans, full_name, internal_project_root set later). That increases mental burden and risk of invariant violations.

7) Error handling and diagnostic clarity
   - Diagnostics for import resolution and project-detection are ad-hoc (many `jump_back(1)` calls), which complicates recovery and testing of error messages.

Design goals for a refactor
---------------------------
- Single Responsibility: parser only builds AST; a separate resolver handles module semantics + filesystem.
- Deterministic & testable: module resolution should be a distinct, testable unit (no FS calls during parsing unit tests).
- Small, incremental changes that preserve existing e2e behaviour by default.
- Clear, documented import semantics and canonical internal representation for import paths.
- Centralized library/stdlib discovery and caching.

Recommended architecture (core components)
-------------------------------------------
1) ModuleResolver (new)
   - Responsibility: convert `Import` ASTs into `Namespace` links, load source files, populate `Program` namespace graph.
   - Responsibilities moved out of `Parser`: `load_import_path`, `load_single_import_part`, `load_import_path_from_base`, `try_load_mod_for_namespace`, `import_external_lib`, `find_or_import_stdlib`, `include_prelude_only`.
   - Key features: caching, cycle detection, deterministic load order, and unit-testable APIs.
   - Example API (pseudocode):
     - `ModuleResolver::resolve_import(program, current_ns, import_ast) -> Result<ResolvedNamespace, Diagnostic>`
     - `ModuleResolver::resolve_all_unhandled(program) -> Result<(), Diagnostics>`

2) ProjectDetector (new)
   - Responsibility: determine project root and mode (standalone vs project) via configuration/CLI or clear policies.
   - Replace the heuristic inside `Parser::create_namespaces_for_initial_file` with a single-call API.
   - Provide explicit flags to force standalone/project (backwards-compatible fallback: keep heuristic, but behind a well-documented function).

3) LibraryRegistry (new)
   - Responsibility: centralize library path lookups, stdlib path, prelude handling and caching.
   - Functions: `find_library(name)`, `load_library(name)`, `get_stdlib_root()`.

4) NamespaceFactory / Namespace invariants
   - Provide helper to create fully-initialized `Namespace`+`Symbol` objects.
   - Ensure invariants (path normalization, span initialization, `internal_project_root` when applicable).

5) Two-phase pipeline (parse -> resolve -> typecheck -> codegen)
   - Parser records imports (AST). After parsing is complete, call `ModuleResolver` to resolve all recorded imports. This eliminates FS side-effects during parsing and centralizes resolution.

Canonical import representation (parser -> resolver contract)
-------------------------------------------------------------
- Parser should parse imports into a normalized Import AST structure with these canonical forms:
  - Absolute library import: `std::io` or `foo::bar` -> ImportKind::Library("foo::bar")
  - Relative project import: `@foo::bar` or `./foo` -> ImportKind::ProjectRelative(path)
  - Relative file import: `..::baz` or `../baz` -> ImportKind::Relative(../..)
  - Current-scope import: `::name` -> ImportKind::CurrentScope
- Normalization occurs at parse-time so `ModuleResolver` receives a consistent, minimal set of cases to implement.

Practical migration plan (incremental, low risk)
------------------------------------------------
1) Invent `ModuleResolver` interface + unit tests (no changes to `Parser` yet). Implement *empty shim* that delegates to current `Parser` logic.
   - Tests: unit tests for `ModuleResolver.resolve_import` using current behavior (filesystem fixtures / virtual FS if available).

2) Move library discovery & stdlib handling into `LibraryRegistry` and switch `Parser` to call `LibraryRegistry` through the shim.
   - Keep behavior identical; tests ensure no regressions.

3) Encapsulate project-detection into `ProjectDetector` and expose CLI/Program option to force mode. Replace direct calls in `Parser` with `ProjectDetector` shim.

4) Refactor parser to stop performing file loads: change `Parser::parse_import` to only emit normalized Import AST and push the import into `ns.unhandled_imports` (already happens), but remove any immediate `load_*` calls.
   - Add `ModuleResolver::resolve_all_unhandled(program)` — call it immediately after `parse_toplevel` (but before typechecking).

5) Reimplement `load_*` functions inside `ModuleResolver` and remove I/O from `Parser`. Add caching/cycle detection.

6) Clean up `Namespace`/`Symbol` lifecycle with `NamespaceFactory` and tighten invariants.

7) Write regression & new unit tests: import grammar coverage, project-vs-standalone, `mod.oc`/`main.oc`, stdlib/library behavior, wildcard/multi import semantics, aliasing, error diagnostics.

API / pseudocode examples
-------------------------
ModuleResolver (sketch)

```ocen
struct ModuleResolver {
  program: &Program
  library_registry: &LibraryRegistry
  resolved_cache: Map<ImportKey, Namespace>
}

def ModuleResolver::resolve_import(&this, cur_ns: &Namespace, imp: &AST) -> bool {
  // canonicalize imp -> ImportKey
  // if resolved_cache contains -> link symbol
  // else: do filesystem checks, load files (Parser::load_file can be used as a helper but not call it inside Parser), update namespaces
  // detect cycles and return diagnostics
}
```

ProjectDetector (sketch)

```ocen
def ProjectDetector::detect_root(path: str, options: ProgramOptions) -> ProjectRootInfo {
  // Prefer explicit Program/project flag. If not set, use a single helper that implements the current heuristic.
  // Return { mode: Standalone|Project, root_path: str|null }
}
```

LibraryRegistry (sketch)

```ocen
struct LibraryRegistry { library_paths: Vector<str> }
fn find(name) -> LibraryInfo
fn get_stdlib_root() -> str|null
```

Compatibility strategy (keep e2e behaviour)
-------------------------------------------
- Default behavior after refactor should match current behavior. Implement the new components as wrappers over the existing code first (adapter approach).
- Add configuration/CLI flags to explicitly choose project vs standalone so callers can opt-out of heuristics.
- Add comprehensive regression tests (use `tests/*` for imports and a few integration tests matching current outputs).

Tests to add or expand
----------------------
- Unit tests for `ModuleResolver::resolve_import` (normal, wildcard, multi-path, aliasing).
- Behavioural tests for `create_namespaces_for_initial_file` migration: ensure `main.oc` detection still works.
- Error tests: missing module, wildcard-from-non-module, importing a `this` module, ambiguous project root.
- LSP-mode tests: importing stdlib files without codegen.

Concrete places to refactor (move logic from -> to)
---------------------------------------------------
- Move `Parser::load_import_path` + `Parser::load_import_path_from_base` -> `ModuleResolver::resolve_import`.
- Move `Parser::import_external_lib`, `Parser::find_or_import_stdlib`, and `Parser::include_prelude_only` -> `LibraryRegistry`.
- Move `Parser::create_namespaces_for_initial_file` -> `ProjectDetector` (or `Program` bootstrap helper).
- Replace in-parser FS checks (`fs::file_exists`, `directory_exists`) with resolver/registry calls.
- Keep `Parser::parse_import` but reduce it to normalizing import ASTs only.

Risks & mitigation
------------------
- Risk: subtle behavioral changes for edge-case imports. Mitigation: keep the original logic behind compat shims while incrementally replacing it and run full test-suite after each step.
- Risk: LSP / `cli` code paths that currently rely on partial namespace initialization. Mitigation: add adapter layer that mimics the old `Namespace` creation semantics during migration.

Estimate & suggested incremental PRs
-----------------------------------
- PR 1 (small): Add `ModuleResolver` interface + tests that call existing parser-based logic (adapter). ~1–2 days.
- PR 2 (small): Extract `LibraryRegistry`. ~1 day.
- PR 3 (medium): Extract `ProjectDetector` and wiring in `parse_toplevel`. ~2 days.
- PR 4 (medium): Remove I/O from `Parser` and reroute through `ModuleResolver`. Add tests + fixes. ~3–5 days.
- PR 5 (small): Cleanup `Namespace` factories and tighten invariants. ~2 days.

Next steps (recommended immediate actions)
-----------------------------------------
1) Add `ModuleResolver` interface and tests (non-invasive).  
2) Add a `Program`/CLI flag to explicitly set project vs standalone mode.  
3) Run the full test suite and add regression tests for import edge-cases.

---

If you want, I can:
- Draft the `ModuleResolver` + `LibraryRegistry` interfaces and a set of unit tests next. ✅
- Produce an incremental PR plan with exact changelist (function-by-function) and tests to update. 🔧

Tell me which of those you want me to do first and I will prepare the patch plan (no code changes until you confirm).