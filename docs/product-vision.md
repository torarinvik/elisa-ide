# Elisa IDE product vision

Elisa IDE is the integrated development environment for the Elisa language. It
brings source editing, language intelligence, project and package tools,
diagnostics, build/test/run, debugging, profiling, and visual Elisa-ui authoring
into one workspace. The form designer is a defining part of the IDE, rather
than the whole product.

The application shell and its controls are written with elisa-ui. The visual
designer uses the same runtime and layout rules as the applications it creates.
Source files and form documents remain ordinary project files with explicit
ownership, stable identities, deterministic generation, and version-control-
friendly changes.

## Developer workflow

A developer opens an Elisa workspace and can move between source files and
forms without losing selection, edits, diagnostics, or task context. The source
editor provides Elisa-aware navigation and compiler-backed diagnostics through
Elisa-LSP. The form editor provides the palette, hierarchy, canvas, inspector,
and event wiring for Elisa-ui applications. Both feed a shared Problems view
and project model.

Build, test, run, debug, and profile actions use resolved Elisa tools and
structured process arguments. Each task records its executable, arguments,
working directory, environment additions, input revision, and output. The IDE
shows the actual capabilities exposed by the selected toolchain and reports
unsupported operations before presenting controls that cannot work.

Opening a project never executes it. Run, debug, and profile actions are
explicit. Child processes are supervised, cancellable where their interfaces
allow it, and reaped on shutdown. Logs omit inherited secrets and retain a
bounded in-memory view with a full task log when practical.

## Confirmed local tool interfaces

The following inventory was checked against the adjacent Elisa tool
checkouts on 2026-09-14. These are integration facts for the current local
tool versions, not permanent guarantees; startup discovery and runtime
capabilities must be checked for every installation.

| Tool | Current integration seam | Current boundary to show honestly |
| --- | --- | --- |
| Elisa-LSP | Long-lived LSP 3.17 JSON-RPC process over stdio with `Content-Length` framing. `src/lsp/lsp_client.py` provides bounded transport and versioned notifications; `src/lsp/workspace_service.py` connects unsaved source buffers, restart replay, and source-mapped Problems rows; `scripts/smoke_lsp.py` exercises that path against the real local server. | Embedded source-editor and Problems-panel presentation, semantic-token rendering, completion, rename, formatting, and workspace-wide/cross-file analysis are not currently complete. Read advertised capabilities from `initialize` and `docs/feature-manifest.json`; do not enable a feature from a design note alone. |
| Elisa compiler | Invoke the selected compiler wrapper with explicit compiler root, runtime, target, output, and arguments. Preserve stdout, stderr, status, and provenance for build tasks. | Freshness checks are part of the contract. Different local tool builds and isolated worktrees may use different compiler/runtime revisions. |
| elisa-pkg | Use the `elisapkg` CLI for manifest validation, local dependency resolution, build/run/test, cache, and package tasks. | Current build/run/test accepts validated local v2 dependency graphs. Registry lockfiles can be inspected or cached, but registry dependencies are rejected before compiler launch and network acquisition/TUF are not production-ready. There is no documented library ABI. |
| elisa-debugger | Launch `elisa-debugger-dap-server` and use DAP `Content-Length` framing for the supported source breakpoint, thread/stack/locals, memory, pause/continue, and forward/reverse-step flow. | The adapter consumes a verified EDIR artifact and does not build the program; launch arguments, working directory, and environment are ignored today. EDIR emission is limited to a small effect-free `main` subset. Compiler columns are one-based UTF-8 bytes while the adapter expects zero-based UTF-16, so source positions must be normalized before normal project debugging is claimed. Unsupported DAP requests remain unavailable. |
| elisa-profiler | Run `profile`, `record`, `report`, `recover`, `compare`, `benchmark`, `doctor`, or `pgo` as structured child jobs. Read its atomic progress file and versioned profile/artifact JSON; open text, folded, Speedscope, or HTML reports. | No editor streaming protocol was found. Treat capture as a managed job, not a live sampling socket. Samples currently attribute Elisa-instrumented stacks, not native unwind or attach. Build/runtime provenance and host capability must travel with results. |

Relevant local contract documents include `Elisa-LSP/README.md` and
`Elisa-LSP/docs/feature-manifest.json`,
`elisa-debugger/docs/integration.md` and
`elisa-debugger/docs/compiler-integration.md`,
`elisa-profiler/README.md`, `elisa-profiler/docs/profile.schema.json`,
`elisa-profiler/docs/profile-artifact.schema.json`,
`elisa-profiler/docs/host-capabilities.md`, and
`elisa-pkg/README.md` with its manifest and lockfile specifications.

## Shared IDE architecture

The IDE owns workspace discovery and a toolchain registry. A resolved tool
record contains its canonical executable path, version or content identity,
supported host/target capabilities, compiler/runtime pairing, and evidence
source. Each LSP/debugger/profiler/package task has a lifecycle separate from
the project document so a crash or restart cannot discard unsaved editor or
form state.

LSP messages use a dedicated protocol reader and writer. Protocol bytes never
share stdout with user-facing progress. Open-document versions are monotonic;
diagnostics, semantic tokens, and navigation results with stale versions are
discarded. Server lifecycle includes initialize/capabilities, workspace
change, shutdown, bounded restart, stderr capture, and graceful termination.
Capabilities gate commands and menus.

Debug tasks bind to an immutable build artifact and source-map/EDIR identity.
Breakpoints and stops map to the user's source only when the file identity and
coordinate convention match. A launch configuration records every DAP field
the adapter actually honors. The debugger panel presents threads, stack,
scopes, variables, and supported stepping operations; unavailable evaluate,
attach, conditional-breakpoint, or launch settings are not simulated.

Profiler tasks record the exact binary, source revision, compiler/runtime,
target, capture arguments, and profiler identity. Progress snapshots are
polled without blocking the shell. A completed artifact is validated against
its schema and provenance before the report viewer presents samples or
comparisons. Missing telemetry stays missing; it is never drawn as zero.

The source editor and visual editor share project file identity, navigation,
diagnostics, and task output, while keeping source-buffer undo separate from
form-document undo. Generated files and handwritten source have explicit
ownership. Regeneration reports conflicts and does not silently overwrite
handwritten work.

## Delivery sequence

The first validated vertical slice remains a real visual Elisa-ui workflow:
create a form, insert a button, edit its caption, see that edit in the isolated
preview, select the matching stable ID, save/reopen, generate code, and build
or run the output. That slice proves the shared document, process, UI, and
source ownership boundaries.

The current generated-project slice is exercised by
`scripts/smoke_project_generation.sh`. It stages every project form (with the
entry form as the application root) and the application composition root,
records the generated-file hashes and source-map ownership in
`.elisa-ide-generation.json`, preserves a handwritten handler file, rejects a
manually edited generated file, and runs the emitted SDL3 application under a
headless driver. This is a project-generation/build smoke, not yet the
in-IDE Build/Run job UI. The desktop shell now exposes Build/Run/Stop controls
backed by the revision-aware `BuildJob` policy kernel, while
`scripts/build_project.sh` and `scripts/run_project.sh` remain the structured
headless build/run entrypoints; the desktop host continues to use AppKit + Skia.

The full Elisa IDE then adds integrated source editing on top of the new bounded
LSP transport and diagnostics bridge, followed
by a unified compiler/package/test task manager, profiler job and result views,
and DAP debugging. The debugger track includes compiler-side EDIR expansion
and coordinate normalization as explicit prerequisites. Tool capability
discovery, compatibility tests, failure recovery, and documentation ship with
each integration. The IDE is not considered complete while these developer
workflows are only external commands with disconnected state.
