# warn-rebuild

Rebuilds the national WARN combined file from the per-state exports in
[biglocalnews/warn-github-flow](https://github.com/biglocalnews/warn-github-flow) with pinned
[warn-transformer](https://pypi.org/project/warn-transformer/) 1.3.423, tolerating the few rows the
stock transformer cannot parse (logged in `rescued.json`), so one malformed row in one state no
longer stops the whole national file.

- `tools/warn_rebuild/rebuild.py`: the rebuild. Guards: never fewer rows than the previous build,
  no previous `hash_id` dropped; a state needing more than 50 tolerated rows reuses its previous export.
- `.github/workflows/warn-rebuild.yml`: runs at 14:40, 19:40 and 23:40 UTC, after upstream's exports.
- Branch `warn-rebuild-data`: `integrated.csv`, `manifest.json` (per-state export commits, counts,
  transformer version, sha256) and `rescued.json`.

Public data only; no credentials. Each row's provenance is the upstream per-state export commit in the manifest.
