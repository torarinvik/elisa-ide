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
controls.

The current preview path serializes the unsaved form and constructs the
supported Elisa-ui controls in an isolated AppKit worker. The SDL3 framework
also has a retained RGBA image upload/draw bridge. The product still needs to
connect the worker frame to the shell canvas and route canvas selection back to
the document host. The source and test are in progress; the UI integration
test currently reports that its spawned preview worker did not exit, although
the worker exits successfully when launched directly with the same snapshot.

Code generation and the headless CLI form a foundation for full projects, not
a complete project build/run workflow. Elisa LSP, debugger, profiler, compiler,
and package-manager connections are required parts of the IDE plan. The local
tool interfaces and capability limits are recorded in
[`docs/product-vision.md`](docs/product-vision.md); unsupported functionality
is never treated as available merely because it appears in a protocol spec.

## Dependencies

| Dependency | Purpose | Default resolution |
| --- | --- | --- |
| elisa-ui | IDE interface and generated application UI | `../elisa-ui`, also exposed as the `framework` symlink |
| wasm-sdk-compiler | Elisa compiler and runtime used by the current native build | `../wasm-sdk-compiler` |
| SDL3 + SDL3_ttf | Native window, input, and text rasterization | Homebrew (`/opt/homebrew/lib` on Apple silicon) |
| clang | Links compiler objects, native services, and runtime | `clang` on `PATH` |
| python3 | Compiler wrapper and development tooling | `python3` on `PATH` |
| Elisa-LSP | Planned language service process | Local tool discovery; see product vision |
| elisa-debugger | Planned DAP debug adapter | Local tool discovery; EDIR support is currently restricted |
| elisa-profiler | Planned profiling command-line tool | Local tool discovery; runs as a managed job |
| elisa-pkg | Planned project/package task backend | Local tool discovery; local dependency graphs currently gate build/run/test |

Current build environment overrides:

- `ELISA_UI_ROOT` — elisa-ui checkout root (default `../elisa-ui`).
- `ELISA_UI_STAGE1` — compiler checkout root (default `../wasm-sdk-compiler`).
- `ELISA_UI_SDL_LIB` — directory holding `libSDL3` and `libSDL3_ttf`
  (default `/opt/homebrew/lib`).
- `ELISA_UI_FONT` — font file for the native backend; the framework default is
  used when unset.
- `ELISA_IDE_CONFIG` — explicit Elisa IDE preferences file. The former
  `ELISA_UI_DESIGNER_CONFIG` name remains a compatibility fallback.

When stage1 is stale, rebuild it from the compiler checkout:

```sh
(cd ../wasm-sdk-compiler && scripts/elisac_stage1.sh --seed)
```

Build scripts reject stale compiler products. Do not set
`ELISA_ALLOW_STALE_STAGE1=1` for acceptance builds. The `framework` symlink
keeps source includes stable and `scripts/doctor.sh` checks its target. For a
different checkout layout, set `ELISA_UI_ROOT` and repoint the symlink; do not
commit personal absolute paths.

## Build and run

```sh
scripts/doctor.sh          # resolve dependencies and write build provenance
scripts/build_native.sh    # build document host and preview worker, then build/elisa_ide
./build/elisa_ide
```

Headless native smoke check:

```sh
SDL_VIDEODRIVER=dummy ELISA_UI_SMOKE_FRAMES=1 ./build/elisa_ide
```

The `elisa_ide_cli` currently validates and migrates form/project files and
generates the first view source:

```sh
scripts/run_core_tests.sh
scripts/run_tests.sh
./build/elisa_ide_cli validate test/fixtures/settings-form.elisaform.json
./build/elisa_ide_cli migrate <input.json> <output.json>
scripts/run_spikes.sh
```

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
src/shell/        menus, toolbar, panes, status, command routing
src/panels/       hierarchy, palette, inspector, diagnostics, tools
src/canvas/       transforms, gestures, overlays, drop intent
src/preview/      supervisor, protocol, scene correlation
src/language/     planned source buffers, LSP sessions, code navigation
src/debug/        planned DAP session and debug-state adapters
src/profile/      planned profile jobs and result readers
src/lowering/     canonical runtime-ready intermediate representation
src/codegen/      Elisa emitter, symbols, source maps, output manifest
src/build/        toolchain discovery, jobs, diagnostics, run supervision
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
