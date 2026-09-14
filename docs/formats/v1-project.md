# Project manifest format (v1)

A project manifest lists the documents that make up an application and the
generation/build settings shared by them. The file extension is
`.elisaproject.json`; the `format` field is the stable discriminator.

```json
{
  "format": "elisa-ide-project",
  "schemaVersion": 1,
  "projectId": "project-settings",
  "name": "Settings Example",
  "applicationId": "org.example.settings",
  "framework": {"dependency": "elisa-ui"},
  "forms": [{"id": "form-settings", "path": "forms/settings.elisaform.json"}],
  "entryForm": "form-settings",
  "source": {"roots": ["src"]},
  "generation": {"namespace": "SettingsExample", "outputRoot": "generated"},
  "targets": [{"id": "desktop", "backend": "appkit-canvas", "renderer": "skia"}]
}
```

## Fields

| Field | Required | Meaning |
| --- | --- | --- |
| `format` | yes | Always `elisa-ide-project` |
| `schemaVersion` | yes | Integer schema version; `1` is current |
| `projectId` | yes | Stable project identity |
| `name` | no | Human display name |
| `applicationId` | no | Target/package identity for generated builds |
| `framework` | no | Required framework dependency information |
| `forms` | yes | Ordered `{id, path}` form references (project-relative) |
| `entryForm` | yes | Form ID of the startup form; must be listed in `forms` |
| `source` | no | User-owned source roots (`roots` array) |
| `generation` | no | `namespace` and `outputRoot` for generated code |
| `targets` | no | Declarative `{id, backend, renderer}` build profiles |
| `extensions` | no | Unknown top-level fields, preserved verbatim |

## Rules

- Form references use the form's stable `id`, never its display name or path.
- `entryForm` must resolve to a listed form (`missing-entry-form`).
- Duplicate form IDs are rejected (`duplicate-form`).
- Empty form paths are rejected (`missing-form-path`).
- Unknown top-level fields are preserved and re-emitted.
- Local workspace state (recent files, window positions, compiler paths) is
  deliberately not part of this file.

Limits: 128 forms, 16 source roots, 16 targets, 8 MiB input.

The encoding rules, unknown-field policy, and migration behavior are the same
as the form document format; see `v1-form.md`.
