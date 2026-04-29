# Contributing

Thanks for helping build the pack. Read this once before your first PR.

## Branch naming

Use `<type>/<short-kebab-description>`:

| Type      | Use for                                          | Example                          |
|-----------|--------------------------------------------------|----------------------------------|
| `mod`     | Adding or removing a mod                         | `mod/add-create`                 |
| `update`  | Bumping a mod or loader version                  | `update/sodium-0.6.5`            |
| `config`  | Editing a mod config in `config/`                | `config/tweak-mob-spawns`        |
| `pack`    | Changes to `pack.toml`, MC/loader version bumps  | `pack/bump-neoforge-21.1.96`     |
| `ci`      | CI / workflow changes                            | `ci/fix-curseforge-export`       |
| `docs`    | README / contributing / docs                     | `docs/clarify-launcher-setup`    |

## Before you push

1. Run `packwiz refresh` — this is required, CI will fail without it.
2. Boot the pack locally at least once if you added/updated a mod. "It compiled" is not "it works."
3. Check the JVM flags / launcher memory haven't changed.

## Commit messages

Short imperative subject, optional body. Examples:

```
mod: add Create 6.0.4

Adds Create + Flywheel deps. Bumps minor version to 0.2.0.
```

```
config: reduce phantom spawns

phantoms.json maxNearbyEntities 8 -> 4
```

## PR review rules

- **Adding a mod**: include a one-line "why" in the PR body. Link the mod page.
- **Updating a mod**: link the changelog. Note any breaking changes (config reset, world incompatibility).
- **Configs**: describe gameplay impact, not just the diff.
- **MC/loader bump**: open early, expect a long discussion. Coordinate so we don't lose a save.

## Merge rights

Only maintainers listed in `.github/CODEOWNERS` (if present) can merge to `main`.
At least one approval required. Squash-merge preferred so `main` stays linear.

## Conflicts

`index.toml` will conflict often when two PRs add mods. To resolve:

```sh
git checkout main -- index.toml
packwiz refresh
git add index.toml
```

Don't try to hand-merge `index.toml`. Always regenerate it.
