# Elisa IDE

Elisa IDE is the integrated development environment for Elisa. Its visual RAD
workspace is built with [elisa-ui](../elisa-ui), and it uses that same framework
to design Elisa applications. Source editing, language services, diagnostics,
project tools, build/test/run, debugging, and profiling belong in the same
workspace as the form designer.

The product direction and acceptance roadmap live in the local, Git-ignored
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md). The tracked
[`docs/product-vision.md`](docs/product-vision.md) explains how the editor and
Elisa tooling fit together. This README covers dependencies, commands, and
verified implementation status.

## Current implementation

The repository has a native elisa-ui shell, a separate document-host process,
typed project/form models, validation, stable design IDs, commands with
undo/redo, safe save and recovery primitives, deterministic source generation,
and a headless CLI. The shell already has a palette, hierarchy, inspector,
Problems panel, keyboard commands, split-pane persistence, and form open/save
controls. The inspector includes a descriptor-backed Events section for Button
and TextField connections; handler edits and disconnects are undoable and
accept qualified Elisa symbols. Recovery snapshots are written after semantic
mutations, and the shell exposes Restore, Discard, and external-change Reload
actions without overwriting a newer file. The **New Project** action creates a fresh project folder containing
a relative `forms/main.elisaform.json` and a matching
`<folder-name>.elisaproject.json` manifest, then opens the starter form. Enter
the destination directory in the path field; the parent directory must
already exist, and the destination itself must be new. Paths with spaces and
UTF-8 names are supported. The same action is available in the compact layout.
The left Project files view lists the active manifest, project-relative form,
and virtual generated view/source-map paths without executing project files;
generated entries open the read-only source projection.

The preview worker serializes the unsaved form, constructs the supported
Elisa-ui controls in an isolated process, and renders a PNG. The process wrapper
reaps an exited child even while a descendant keeps its output pipes open, and
a regression test covers that case. The selected desktop host profile is a
native AppKit shell with elisa-ui's retained drawing commands rasterized by the
pinned Skia CPU backend. The Skia pin, archive, and IDE image bridge have been
built and verified. `scripts/build_ide.sh` verifies the compiler/runtime and
Skia provenance, then builds and signs the product. The IDE defaults to Elisa
`-O0` while the current full-shell `-O1`/`-O2` compile stalls in LLVM's
optimization passes; `ELISA_IDE_OPT_LEVEL` remains available for explicit
optimization trials. The preview worker compiles with Elisa `-O2` by default;
`build/provenance-ide.json` records the compiler revision and selected product
optimization level alongside the Skia build identity.
`build/provenance-preview-worker.json` records its compiler and optimization
level.
The embedded compiler checkout is on `codex/edir-host-effects` at
`6b475d894331f0a81c3112167ef7fcf5c642a424`, nine commits ahead of the latest
fetched `origin/main` (`72a752820ab581d46bb17b3fb7158ba3879a16e3`). The local
commits add the bounded EDIR host-effect support used by the debugger, plus
subsequent compiler fixes; `origin/main` is an ancestor of this checkout. The
stage1 wrapper rejects a product when its compiler source or runtime provenance
is stale. Stage1 was freshly seeded from this checkout and its source/runtime
provenance checks pass. The generated application and Skia IDE bundle were then
built without a stale-stage1 override using compiler revision
`6b475d894331f0a81c3112167ef7fcf5c642a424`; the debugger's EDIR path uses `-O2`
and verifies the artifact before launch. The Skia check reports that the sibling
`elisa-ui` checkout is dirty, so its build evidence is not fully reproducible. Live Skia preview image
binding has been verified. In the running
macOS app, inserting a Button from the component palette reached `Preview
ready`, and the preview displayed the Button upright at the top of its Column.
Clicking that rendered Button after selecting the root changed hierarchy
selection and refreshed the Button inspector in the native window. The app now
launches from the repository root without `ELISA_IDE_RUNTIME_ROOT`; the
document host uses a private temporary request/reply directory, avoiding stale
or cross-instance `build/` IPC files. `scripts/test_host_ipc_paths.sh` verifies
session-path uniqueness, owner-only permissions, mismatch rejection, and
scoped cleanup. The PNG decoder's row orientation is
covered by `scripts/test_png_decode_orientation.sh`. The SDL3 integration
harness also covers selection through a validating headless image adapter.
An untouched form explains that a change is needed to start its live preview.
The native app also handles Cocoa's `NSApplicationWillTerminate` notification,
so the host is stopped and its private IPC directory is removed when the user
chooses Quit from the application menu; the observer is covered by
`scripts/test_ide_termination_observer.sh`.

SDL3 remains available for headless and UI-test harnesses and for generated-app
profiles that explicitly select it. It is not the IDE's default desktop host.
The initial Skia build profile is macOS arm64 CPU raster: it does not require a
GPU and does not enable Metal. Other architectures or raster/GPU configurations
need their own pinned and verified build profile.

The project generator and headless scripts cover the standalone workflow, and
the desktop shell now exposes a revision-aware Build/Run/Stop job path that
uses the same structured process service. Build Output is visible in the
bottom pane and receives incremental stdout/stderr without duplicating retained
bytes; the captured log is bounded and remains available after a successful or
failed build. Recognized compiler/package diagnostic lines are also projected
into the paged Problems panel while the raw log remains intact. The bounded LSP
transport foundation
in `src/lsp/lsp_client.py` handles strict Content-Length framing, UTF-8 JSON,
subprocess deadlines, lifecycle messages, document versions, and version-gated
diagnostics; `DiagnosticStore.typed()` preserves version, severity, source, code,
ranges, related information, and raw payloads for future Problems/source views.
`worker/lsp/ide_lsp_host.py` now exposes a bounded host-side protocol for
unsaved source buffers, server lifecycle, asynchronous diagnostic polling, and
versioned diagnostic groups. The native source pane launches the sidecar,
sends the unsaved buffer and edits, and shows server state, document version,
server generation, and current diagnostic count. Versioned LSP messages appear in the shared
Problems panel with severity, source, code, and source location; selecting a
ranged row maps its negotiated UTF-8/16/32 position to the exact source caret.
Editable source rows underline exact diagnostic character ranges in severity
colors and receive matching line rings, while row help shows the exact
start/end range and up to four related-information locations; if the server
reports more, the help text gives the omitted count. **Restart
LSP** restarts the server and replays the current in-memory source buffer.
Select an Elisa-LSP row in Problems and use **Related 1/4** (the button advances
through the retained locations) to open its local `.elisa` file at the reported
line and character. Related navigation decodes percent-escaped file URIs and
keeps the current buffer open when it has unsaved edits. Ordinary Open and Open
Handlers actions also refuse to replace a modified source buffer. In an open
handwritten source file, click the Elisa-LSP status control or press F12 from a
source row to ask Elisa-LSP for the symbol at the caret. It follows standard
Location and LocationLink replies and opens a local `.elisa` target at the
negotiated source position. Cross-file navigation keeps the dirty-source and
form-save guards in place. Linking
LSP diagnostics to generated forms and shared native Problems-model adapters
remain in progress.

When the path field opens a standalone `.elisa` source file, **Build** compiles
that saved file with the resolved Elisa compiler/runtime profile, without
requiring SDL or a RAD project manifest. **Run** is available only for the last
successful build of the same source path and content fingerprint. Unsaved
buffer edits and detected external file changes block Build or Run until the
source is saved or reconciled. The Run process is supervised like a build, and
its stdout/stderr are appended once to the bounded Build Output log.

For a saved source file inside a local `elisapkg` package, press
**Ctrl/Cmd+Shift+T** to inspect declared test targets, the default target,
resolved package-manager identity, exact offline command, and working
directory. That first activation only creates a read-only plan. Press the
shortcut again within 30 seconds to approve the same package manifest, selected
target, and package-manager binary and run that target. The approval also binds
a bounded SHA-256 fingerprint of package files and reachable local path
dependencies. The task runner rechecks those inputs after completion, supervises
the package manager as a process group, and makes **Stop** terminate and reap
its descendants. Output appears in Build Output; failures appear in Problems,
and results are marked stale if either the open source or package inputs change
during the run. The input scan ignores top-level `.git`, `.elisa-ide`, `.cache`,
`build`, `target`, and `__pycache__` directories; it refuses symlinks, special
files, registry dependencies, and non-empty dev-dependencies, and caps the
package graph at 128 local packages, 20,000 files, and 512 MiB. Target selection
is available with **Ctrl/Cmd+Option+Shift+T**; the refreshed preview names the
selected target, and **Ctrl/Cmd+Shift+T** approves/runs it. Recent-run history
is available with **Ctrl/Cmd+Option+Shift+H**. The recent-run list shows up to
eight of at most 64 private, atomic records; **Ctrl/Cmd+Option+Shift+J/K** moves
through run details and back to the list. Each detail includes its target, task
and input identities, command, working directory, and bounded stdout/stderr
captures (6 KiB per stream, with truncation marked). Terminal control bytes are
escaped before display. Records are stored under the package's private
`.elisa-ide/package-test-history/` directory; only environment-variable names
are stored, never values. This is target-level history; test-case-level states
are shown only when `elisapkg` includes them in its output, and a richer
interactive test explorer remains future work.

Press **Ctrl/Cmd+Option+Shift+D** to run **Toolchain Doctor** from the IDE. Its
Build Output report checks the selected Elisa stage1 compiler against its source
and runtime freshness records and lists missing native build dependencies with
repair commands. Project builds run the same SDL-profile preflight before
generation, so an invalid compiler/runtime pair fails quickly with the doctor
report instead of starting a longer build.

### Managed debugger (early)

For a saved standalone `.elisa` file, the Run control becomes **Debug**. It
builds optimized EDIR with the current, freshness-checked stage1 compiler and
launches the managed adapter selected through `ELISA_DEBUGGER_DAP`, a trusted
nearby debugger build, or `PATH`. Right-click a source line to toggle its red
breakpoint marker; changes are sent to the running DAP session and the adapter's
verification appears in Build Output. Breakpoints are scoped to the open source
file and cleared when its path or content changes. F5 starts Debug,
F8 continues, F10 steps to the next source line, F6 pauses, and **Stop** ends the
session. **Ctrl/Cmd+Shift+F5** explicitly restarts debugging by rebuilding EDIR
for the current saved source before starting a new DAP session. It refuses dirty
or externally changed source, so a restart cannot reuse an artifact from an
earlier source revision. While stopped, Build Output shows the stop reason, bounded stack, and
top-frame locals. The shell stops a session if the saved source becomes dirty,
changes externally, or no longer matches the verified EDIR build.

This first debugger path supports only the bounded EDIR shapes currently emitted
by the compiler. Attach, conditional/log breakpoints, selectable threads/frames,
and dedicated structured debugger panes are not implemented yet.

### Managed profiler (initial)

Open a saved standalone `.elisa` file and select **Profile** in the source
header. Elisa IDE resolves `elisa-profiler` from `ELISA_PROFILER`, a nearby
`../Elisa-profiler/bin/elisa-profiler`, or `PATH`, then starts a cancellable
capture job. The default launch records one function-mode repetition with a
30-second target limit. A project can select a saved launch configuration in
`.elisa-ide/profile-launches.json`; its target must resolve to the exact saved
source currently open. Configurations can set collection mode, repetitions,
warmups, target timeout, sample period, working directory, stdin, target
arguments, environment overrides, random seed, and profile output folder.
Unknown fields, invalid paths, profiler transport variables, and configurations
for another open source are rejected before a capture starts. Environment values
in this local JSON file are plain text, so do not put secrets there. The IDE
rejects unsaved or externally changed input and checks the artifact's
`workload.source_sha256` against the captured source. Successful captures are
stored under the user's private Elisa IDE Profiles directory
(`~/Library/Application Support/Elisa IDE/Profiles` on macOS, or the XDG data
directory on Linux) unless `output_directory` is set; the explicit
`ELISA_IDE_PROFILE_ROOT` setting takes precedence. Each run has a unique name.
Each capture also has an adjacent owner-only `.task.json` record with the
configuration, target, terminal state, profiler/compiler hashes, and reported
capability coverage; environment values are omitted.
Build Output shows
capture quality, measured event categories, target execution time when present,
the top reported functions, compiler revision, stage1/runtime hashes, and
artifact/tool hashes.
It also shows the capture's event and capability coverage. A function-mode
launch marks sampling and allocation disabled, task lifecycle unsupported, and
native-frame unwinding and attach unavailable; fields the profiler does not
report remain labeled **not recorded**.
Stopping a capture terminates its process group, and a profiler recovery
manifest is retained when one was written before cancellation.
After a current capture, **Profile** becomes **Open Report**. The report is
rendered from the newest validated capture for the unchanged saved source and
opened with the configured viewer. HTML is the default; text, folded, and
Speedscope formats are selectable with `ELISA_IDE_PROFILE_REPORT_FORMAT`.
If a capture is interrupted and leaves a private, source-matching partial
manifest, the same toolbar control becomes **Recover**. Recovery keeps only the
profiler's complete framed records, validates that the saved source still has
the recorded digest, and writes a separate recovered artifact and task record.
The recovered report is marked incomplete; compiler/runtime hashes, workload
duration, and completed repetitions remain unavailable because offline recovery
does not rerun the workload. Once recovered, the control becomes **Open Report**.

For example, this project-local file selects a sampling run for one source:

```json
{
  "schema_version": 1,
  "active_configuration": "release-sampling",
  "configurations": [
    {
      "id": "release-sampling",
      "target": "src/main.elisa",
      "mode": "sample",
      "repetitions": 5,
      "warmup": 1,
      "timeout_seconds": 45,
      "sample_period_us": 1000,
      "working_directory": ".",
      "arguments": ["--scenario", "large input"],
      "stdin": "fixtures/input.txt",
      "environment": {"APP_PROFILE": "release"},
      "random_seed": 17,
      "output_directory": ".elisa-ide/profiles"
    }
  ]
}
```

The initial workflow still profiles saved source through the profiler's managed
compile path. Reusing a selected successful instrumented build artifact,
allocation telemetry, source-location navigation, comparisons, and a dedicated
profiler report pane remain future work. Offline partial-capture recovery is
available for private manifests whose source path and digest still match. Missing
profiler measurements remain labeled unavailable.

`src/lsp/server_resolution.py` selects and hashes the
configured server, trusted nearby build, `ELISA_LSP`, or `PATH` executable in
that order. Elisa LSP, debugger, profiler, compiler,
and package-manager connections are required parts of the IDE plan. The local
tool interfaces and capability limits are recorded in
[`docs/product-vision.md`](docs/product-vision.md); unsupported functionality
is never treated as available merely because it appears in a protocol spec.
`src/build/build_diagnostics.py` provides a bounded typed projection for
compiler-wrapper and `elisapkg` lines while preserving the raw Build Output.
Clicking a colon-form or parenthesized-location diagnostic navigates to its
line and column when its path exactly matches the open handwritten source
buffer. A generated-source diagnostic selects its owning form node only when
both generated bytes and the source-map sidecar exactly match the current form;
stale or unrelated generated artifacts remain explicitly unmapped.
The shell's Source view displays the generated Elisa view read-only beside the
RAD canvas; generated ownership remains explicit and edits continue to target
the form or handwritten handler source.
The document host also exposes the exact-byte generated source map (opcode
107) and a fail-closed UTF-8 byte navigation request (opcode 108), so source
navigation can verify the map hash before selecting its owning form node. The
Source pane presents a bounded 32-line clickable generated-source projection
that uses this route to refresh the hierarchy and inspector; larger generated
files remain explicitly truncated until the virtualized editor lands. Its
header displays the relative owning form path and states that the generated
view is read-only with a bounded visible-row count.
Handwritten `.elisa` files open in a paged line editor. Visible lines up to
768 bytes can be edited and saved atomically from the Source pane; longer
lines stay locked so clipped text cannot replace hidden bytes. The toolbar's
Save action becomes available when the source buffer is dirty and the opened
file has no unresolved external change. Save checks
that the on-disk file still matches the version opened by the IDE. When the
file changes, the Source pane offers **Compare**, **Keep Buffer**, and
**Discard + Reload**. Keep Buffer explicitly selects the local version for the
next Save; Reload discards local edits and adopts the disk version. If the disk
copy is missing or cannot be read, use **Save As** with a new `.elisa` path in
the document path field; Save As refuses to replace an existing file. Set
`ELISA_IDE_EDITOR` to an executable that accepts `<file-path> <one-based-line>`
to enable **Open in Editor**; the file and line are passed as separate process
arguments without shell parsing. **Reveal** asks Finder to show the open source
file. Use Up and Down to move between editable source rows and PageUp/PageDown
to move by 32 lines; navigation retains the code-column position across pages.
Enter a one-based line number in **Go to Line** and press Enter or **Go** to
move to that line, focus it, and scroll it into view.
Use **Find**, **Prev**, and **Next** to search the complete open source buffer
for an exact UTF-8 query of up to 256 bytes. Search starts from the caret (or
continues past the last match even after focus moves to a search button), wraps
at the file boundary, and moves the matching row into view. A match on a clipped,
read-only row is reported, with **Open in Editor** available for inspection.
**Replace Next** replaces the active match, or finds the next match first, as
one undoable edit; replacement text must stay on one line. **Replace All** applies
the exact UTF-8 query to the complete open buffer, reports the number of
non-overlapping matches, and records the result as one undoable edit while
preserving the file's LF or CRLF convention. Regular-expression search is not
available yet. With an editable source row focused, press Ctrl/Cmd+F to focus
Find and select its current query for replacement.
Press Enter to split the focused line at the caret or replace its selection,
preserving the source file's LF or CRLF convention. Backspace at a line start
and Delete at a line end merge adjacent lines. Undo and Redo use a bounded
source-edit history that stays separate from form-document history; the toolbar
and Command/Ctrl+Z, Command/Ctrl+Shift+Z, or Command/Ctrl+Y use that source history
while a source line has focus. Tab adds a four-space indent after the line's
existing leading whitespace; Shift+Tab removes one indentation level from the
active line.
Command/Ctrl+A selects the current source row's code text and leaves its visible
line-number prefix out of the selection.
Select a Problems row and choose **Copy** to place its displayed
diagnostic text on the system clipboard. For an Elisa-LSP diagnostic with
related locations, **Related** opens the next retained local source location;
the button cycles through the four retained entries and reports the selected
line in the status area.

## Dependencies

| Dependency | Purpose | Default resolution |
| --- | --- | --- |
| elisa-ui | IDE interface and generated application UI | `../elisa-ui`, also exposed as the `framework` symlink |
| Elisa-compiler | Current Elisa compiler and runtime used by the native build | ignored `compiler` symlink (fallback `../Elisa-compiler`) |
| AppKit + pinned Skia | IDE desktop window and custom-canvas rasterization | macOS; see `SKIA_ROOT` and `SKIA_OUT` below |
| SDL3 + SDL3_ttf | Headless/UI-test harnesses and generated-app profiles that select SDL3 | Homebrew (`/opt/homebrew/lib` on Apple silicon) |
| clang | Links compiler objects, native services, and runtime | `clang` on `PATH` |
| python3 | Compiler wrapper and development tooling | `python3` on `PATH` |
| Elisa-LSP | Managed source diagnostics and definition navigation | `ELISA_LSP`, trusted nearby server, or `PATH`; workspace-wide features remain limited |
| elisa-debugger | Managed DAP debugging for supported standalone-source EDIR | `ELISA_DEBUGGER_DAP`, trusted nearby adapter, or `PATH`; emitted EDIR shapes remain limited |
| elisa-profiler | Managed standalone-source function captures | `ELISA_PROFILER`, nearby profiler checkout, or `PATH` |
| elisa-pkg | Local package test planning and default-target execution; broader package tasks are planned | `ELISA_IDE_ELISAPKG`, trusted nearby tool, `ELISAPKG`, or `PATH`; registry dependencies are refused in the IDE test profile |

Current build environment overrides:

- `ELISA_UI_ROOT` — elisa-ui checkout root (default `../elisa-ui`).
- `ELISA_UI_STAGE1` — compiler checkout root (default the ignored `compiler` symlink, fallback `../Elisa-compiler`).
- `ELISA_IDE_OPT_LEVEL` — Elisa optimization level for the IDE executable
  (default `-O0`; accepts `-O0` through `-O3`).
- `ELISA_PREVIEW_WORKER_OPT_LEVEL` — Elisa optimization level for the isolated
  AppKit preview worker (default `-O2`; accepts `-O0` through `-O3`).
- `SKIA_ROOT` — the Skia source checkout at the revision pinned by
  `../elisa-ui/third_party/skia.lock` (currently Skia `chrome/m150`, revision
  `9c7b2dffb2433f5a0cc2b77f06025a09126807ed`).
- `SKIA_OUT` — the isolated GN build output containing `libskia.a`; defaults to
  `$SKIA_ROOT/out/elisa`.
- `ELISA_UI_SDL_LIB` — directory holding `libSDL3` and `libSDL3_ttf` for
  SDL-based harnesses and generated-app profiles (default `/opt/homebrew/lib`).
- `ELISA_UI_FONT` — font file for the native backend; the framework default is
  used when unset.
- `ELISA_IDE_CONFIG` — explicit Elisa IDE preferences file. The former
  `ELISA_UI_DESIGNER_CONFIG` name remains a compatibility fallback.
- `ELISA_IDE_EDITOR` — optional editor executable for the read-only Source pane;
  it receives the selected file path and one-based line as separate arguments.
- `ELISA_PROFILER` — explicit executable path for the Elisa profiler; when unset,
  the IDE checks a nearby profiler checkout and then `PATH`.
- `ELISA_IDE_ELISAPKG` — explicit package-manager executable for local package
  test planning and execution; when unset, the IDE checks a trusted nearby tool,
  then `ELISAPKG`, then `PATH`.
- `ELISA_IDE_PROFILE_ROOT` — optional root directory for profile artifacts. Relative
  paths resolve from the source project's root; the default uses private per-user
  Elisa IDE application data storage.
- `ELISA_IDE_PROFILE_REPORT_FORMAT` — report format rendered by **Open Report**:
  `html` (default), `text`, `folded`, or `speedscope`.
- `ELISA_IDE_VIEWER` — optional viewer executable. The IDE passes the generated
  report path as its only argument; when unset it uses macOS `open` or `xdg-open`.

When stage1 is stale, rebuild it from the compiler checkout:

```sh
(cd compiler && scripts/elisac_stage1.sh --seed)
```

Build scripts reject stale compiler products. Do not set
`ELISA_ALLOW_STALE_STAGE1=1` for acceptance builds. The `framework` symlink
keeps source includes stable and `scripts/doctor.sh` checks its target. For a
different checkout layout, set `ELISA_UI_ROOT` and repoint the symlink; do not
commit personal absolute paths. The ignored `compiler` symlink must resolve to
the same compiler checkout selected by `ELISA_UI_STAGE1`, because Elisa source
files include standard-library modules through it.

Each provenance JSON also records `stage1_sources_sha256`, a deterministic hash
of every `.elisa` and `.elisai` source under the compiler's `src/` and
`elisacore_std/` trees, including uncommitted and untracked files. Build scripts
compare that fingerprint and the stage1/runtime binary hashes again after
compilation, so a compiler or source change during a build rejects the result.
The IDE build permits the intentionally dirty sibling compiler checkout while
keeping the stale-source gate enabled; its source fingerprint makes that exact
local compiler snapshot reviewable.

The Skia dependency is not vendored. On macOS arm64, provision the pinned
CPU-raster archive with the framework's lockfile-driven build script, then run
its renderer checks:

```sh
export SKIA_ROOT=/path/to/elisa-skia
export SKIA_OUT="$SKIA_ROOT/out/elisa"
bash ../elisa-ui/scripts/build_skia.sh
SKIA_ROOT="$SKIA_ROOT" SKIA_OUT="$SKIA_OUT" bash ../elisa-ui/scripts/check_appkit_skia.sh
```

The pin currently builds `target_os="mac"`, `target_cpu="arm64"`, with both
OpenGL and Metal disabled. `build_skia.sh` needs Git, Python 3, and Ninja
(`ninja` or `autoninja`); it fetches Skia's GN tool and dependencies. These
commands provision and check the framework's Skia backend; they do not by
themselves build or verify the Elisa IDE host. The IDE's AppKit+Skia build and
interactive preview path remain acceptance work.

## Build and run

```sh
scripts/doctor.sh       # verify the default macOS AppKit + Skia IDE profile
scripts/build_ide.sh    # build and bundle Elisa IDE (requires the pinned Skia archive)
open "build/Elisa IDE.app"
scripts/run_core_tests.sh
scripts/run_ui_tests.sh # SDL3-backed UI and headless harness checks
python3 scripts/smoke_lsp.py # exercise the discovered local Elisa-LSP
```

`scripts/build_native.sh` is reserved for SDL3 tests and generated-app profiles.
For one of those products, a headless smoke check is:

```sh
scripts/build_native.sh test/ui/designer_shell_test.elisa designer_shell_test
scripts/smoke_native.sh designer_shell_test
```

The `elisa_ide_cli` validates and migrates form/project files and generates a
deterministic view source. Project-level generation is available through the
staged Python publisher; it resolves every project form (with the entry form
as the application root), emits view modules and an SDL3 application
composition root, writes an ownership manifest, and refuses to
replace modified generated files or collide with unowned output:

```sh
scripts/run_core_tests.sh
scripts/run_tests.sh
./build/elisa_ide_cli validate test/fixtures/settings-form.elisaform.json
./build/elisa_ide_cli migrate <input.json> <output.json>
./build/elisa_ide_cli source <generated.elisa> <generated.map.json> <byte-offset>
python3 scripts/generate_project.py <project.elisaproject.json> \
  --handlers <path/to/handwritten_handlers.elisa> \
  --framework-include <path/to/elisa-ui/src/platform/sdl3/ui_sdl3_flat.elisa>
scripts/build_project.sh <project-folder-or-project.elisaproject.json> \
  --handlers <path/to/handwritten_handlers.elisa> \
  --framework-include <path/to/elisa-ui/src/platform/sdl3/ui_sdl3_flat.elisa>
scripts/run_project.sh <project-folder-or-project.elisaproject.json> \
  --handlers <path/to/handwritten_handlers.elisa> \
  --framework-include <path/to/elisa-ui/src/platform/sdl3/ui_sdl3_flat.elisa>
scripts/smoke_project_generation.sh
scripts/smoke_counter_generation.sh
scripts/run_spikes.sh
```

The generated directory contains one `<namespace>_*.elisa` view per project
form, a matching `.map.json` source map for each view,
`<namespace>_app.elisa`, a generated README, and `.elisa-ide-generation.json`.
The `source` CLI command validates a map against exact generated bytes and
resolves a byte offset to its form/node/property/event origin; stale maps are
rejected. Handler source stays outside that ownership manifest and is included
by path. The generated app is
the SDL3/headless profile used by the current compile/run smoke; the IDE
desktop itself remains AppKit + Skia.

Build, run, test, LSP, debugger, and profiler controls will converge on a
shared job and toolchain manager as those integrations land. Opening a project
does not implicitly run project code.

## Repository layout

```text
src/app/          startup, application callbacks, composition root
src/model/        projects, forms, nodes, typed values, references
src/schema/       decode, encode, versions, migrations
src/registry/     component and property metadata
src/commands/     transactions, operations, undo/redo
src/validation/   graph, property, target, diagnostic checks
src/workspace/    projects, source documents, preferences, recovery
src/lsp/          bounded Elisa-LSP framing, lifecycle, and document diagnostics
src/source/       UTF-8-preserving source buffer, positions, and local undo
src/debug/        bounded DAP transport and build/source session identity
src/profile/      strict profile artifact validation and compiler/runtime identity
src/toolchain/    hashed executable selection and compiler/runtime pairing records
src/shell/        menus, toolbar, panes, status, command routing
src/panels/       hierarchy, palette, inspector, diagnostics, tools
src/canvas/       transforms, gestures, overlays, drop intent
src/preview/      supervisor, protocol, scene correlation
src/language/     planned source buffers, LSP sessions, code navigation
src/lowering/     canonical runtime-ready intermediate representation
src/codegen/      Elisa emitter, symbols, source maps, output manifest
src/build/        revision-aware build-job policy and later toolchain supervision
src/platform/     narrow desktop service adapters
src/cli/          validation, generation, and later build/run entrypoints
worker/           isolated preview and document-host processes
test/             model, schema, command, codegen, preview, UI, integration
scripts/          doctor, build, smoke, test entrypoints
docs/adr/         architecture decision records
docs/formats/     project/form schema references
build/            ignored products, separated by target/configuration
```

Directories are added with their first real module; empty scaffolding is not
created ahead of implementation.

## Plan maintenance

`IMPLEMENTATION_PLAN.md` remains ignored by Git as requested by the project
owner. Stable architecture, protocol, and user-workflow decisions move into
tracked `docs/` files as they are verified. Plan checkboxes are evidence-based,
and the plan distinguishes current features from the larger Elisa IDE roadmap.
