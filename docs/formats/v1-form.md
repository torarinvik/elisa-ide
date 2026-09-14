# Form document format (v1)

A form document describes one designed view. The file extension is
`.elisaform.json`; the `format` field is the stable discriminator.

```json
{
  "format": "elisa-ide-form",
  "schemaVersion": 1,
  "id": "form-settings",
  "name": "Settings",
  "moduleSymbol": "settings_view",
  "viewport": {"width": 640, "height": 480},
  "root": "node-root",
  "nodes": [
    {
      "id": "node-root",
      "type": "elisa.ui.column",
      "name": "Settings layout",
      "symbol": "settings_layout",
      "properties": {"padding": 16, "spacing": 8, "background": "#181b22ff"},
      "children": ["node-save"],
      "events": {}
    },
    {
      "id": "node-save",
      "type": "elisa.ui.button",
      "name": "Save",
      "symbol": "save_button",
      "properties": {"text": "Save", "minimumWidth": 96},
      "children": [],
      "events": {"click": {"handler": "SettingsHandlers::save_clicked"}}
    }
  ]
}
```

## Fields

| Field | Required | Meaning |
| --- | --- | --- |
| `format` | yes | Always `elisa-ide-form` |
| `schemaVersion` | yes | Integer schema version; `1` is current |
| `id` | yes | Stable form identity (`DesignId` rules below) |
| `name` | no | Human display name; independent of the generated symbol |
| `moduleSymbol` | no | Lowercase Elisa identifier for the generated view module |
| `viewport` | no | Logical design size (`width`, `height`); defaults 800x600 |
| `root` | yes | Node ID of the single visual root |
| `nodes` | yes | Ordered node array; child order comes from `children` |
| `extensions` | no | Unknown top-level fields, preserved verbatim |

## Nodes

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Stable node identity, unique within the form |
| `type` | yes | Registry type key, e.g. `elisa.ui.button` |
| `name` | no | Display name |
| `symbol` | no | Generated Elisa symbol (lowercase, digits, underscores) |
| `properties` | no | Typed property object (below) |
| `children` | no | Ordered child node IDs |
| `events` | no | Event key to `{"handler": "..."}` connections |
| `runtimeHidden` | no | Runtime visibility flag, distinct from editor hiding |
| `editorLocked`, `editorHidden` | no | Design-only state; never executable |

Parent links are derived from `children` on load; they are not serialized, so
there is exactly one source of structural truth.

## Identities and names

IDs are opaque, immutable, and never derived from display names, array indexes,
or runtime handles. Canonical form: lowercase ASCII letters, digits, hyphens;
starts with a letter; at most 24 bytes. Generated IDs are deterministic
(`n1`, `n2`, ... per namespace), so encoding the same document twice is
byte-identical.

A display rename never changes an ID. A generated symbol is a separate,
explicitly renamed concept.

## Typed property values

| JSON shape | Type tag | Notes |
| --- | --- | --- |
| `true` / `false` | `bool` | |
| number | `integer`, `scalar`, or `length` | Chosen by the registry descriptor |
| string | `text`, `enumKey`, `nodeRef` | Chosen by the registry descriptor |
| `"#rrggbb"` / `"#rrggbbaa"` | `color` | Hex channels, alpha optional |
| any JSON | `unsupported` | Preserved raw; never reinterpreted silently |

A value whose JSON shape does not match its descriptor is preserved raw and
reported as `invalid-property-type` by validation. Range violations are
reported as `property-out-of-range`.

## Unknown data

- Unknown component types load as editable placeholders
  (`unknown-component` warning) and encode unchanged.
- Unknown property keys are preserved as raw JSON (`unknown-property`).
- Unknown node and top-level fields are preserved in an extension object.
- Duplicate object keys are rejected (`duplicate-key`); duplicate IDs are
  rejected (`duplicate-id`).

## Limits

Input 8 MiB, 4096 nodes, 512 children per node, 64 properties per node,
16 events per node, 64 KiB per document string pool, 16 KiB per extension
payload, 128 diagnostics. Exceeding a limit fails with a diagnostic rather
than truncating silently.

## Canonical encoding

Compact JSON, fixed member order, locale-independent numbers, no timestamps or
random values, no trailing newline. Decode → encode is stable, and
encode → decode → encode is byte-identical. Unknown fields are emitted after
known fields in their preserved order.

## Migration

Version `0` used `title` for the display name; the `0 -> 1` migration renames
it and reports `migration-applied`. Future versions are refused with
`unsupported-schema-version` and leave the file untouched.
