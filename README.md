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
The ignored `compiler` symlink points at the newest available stage1 checkout,
`../Elisa-compiler`, revision
`b903bd1e700aa2acda4925b59efd1ee60389431a` on `main`, verified against the
upstream `origin/main`. Its compiler product, runtime, and current source
fingerprint pass the toolchain freshness check. The latest upstream commit only
changes the differential-corpus timeout scaling; the compiler-source
fingerprint is unchanged. The verified source fingerprint is
`f085098e24fbf4f94f67a07afb124bc74eee45b1551f14f71c18628a27ed3335`, stage1
SHA256 `734fad7984b0c6de3b50e6d57585e3c8573f975c33e4560f647a1209f9ed828d`,
and runtime SHA256 `0db509f791ec1b075049dadc51a44f6e51e93e7c8fa9e6db4acbc336b7de022e`.
The current AppKit + Skia bundle builds and signs with that compiler at `-O0`.
The core and Skia doctor checks pass; the Skia check reports that the sibling
`elisa-ui` checkout is dirty, so its build evidence is not fully reproducible.
The generated-project shell integration passes all 163 assertions, including
Build/Run job handling and source-map selection; focused command and codec tests
also pass. Live Skia preview image binding has been verified. In the running
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

Advanced source editing and debugger/profiler connections remain
planned; the DAP transport and profiler artifact reader now provide the bounded
process/data foundations. `src/lsp/server_resolution.py` selects and hashes the
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
| Elisa-LSP | Language service process for the planned source workspace | Local tool discovery; transport foundation is in `src/lsp/` |
| elisa-debugger | Planned DAP debug adapter | Local tool discovery; EDIR support is currently restricted |
| elisa-profiler | Planned profiling command-line tool | Local tool discovery; runs as a managed job |
| elisa-pkg | Planned project/package task backend | Local tool discovery; local dependency graphs currently gate build/run/test |

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
