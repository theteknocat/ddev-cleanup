# ddev-cleanup

A Docker cleanup tool for [ddev](https://ddev.com/) development environments. Recovers disk space by removing empty volumes, unused images, and stale project data — while protecting anything still in active use.

## Background

ddev-managed projects accumulate Docker cruft over time: phantom volumes created by older ddev versions but never written to, dangling images from past builds, and project databases for sites that haven't been touched in months. On a busy machine this adds up fast.

Rather than matching volumes by name patterns, this tool determines what's safe to remove by inspecting actual usage — volume sizes, container links, and project configuration files — so it works correctly regardless of how your projects are named or which ddev version created them.

## Requirements

- Python 3.9+
- Docker (tested with OrbStack on macOS)
- ddev

No additional Python packages required.

## Usage

```bash
ddev-cleanup.py [OPTIONS]
```

Running with no options performs a full cleanup (volumes, images, and stale projects), prompting for confirmation at each step.

### Options

| Flag               | Description                                                          |
| ------------------ | -------------------------------------------------------------------- |
| `-s`, `--status`   | Show disk usage, project list, and volume analysis — no changes made |
| `-v`, `--volumes`  | Volume cleanup only                                                  |
| `-i`, `--images`   | Image cleanup only                                                   |
| `-p`, `--projects` | Stale project cleanup only                                           |
| `-a`, `--all`      | Run all cleanup (same as no flags)                                   |
| `-d`, `--dry-run`  | Show what would be done without making any changes                   |

### Examples

```bash
# See what's taking up space before doing anything
ddev-cleanup.py --status

# Preview what a full cleanup would do
ddev-cleanup.py --dry-run

# Full cleanup with prompts
ddev-cleanup.py

# Just volumes
ddev-cleanup.py --volumes
```

## What it does

### Status report (`--status`)

Shows three sections:

- **Docker disk usage** — overall summary from `docker system df`
- **ddev projects** — all projects sorted by last started date, with stale projects (6+ months idle) highlighted
- **Volume analysis** — all volumes categorised (see below)

### Volume cleanup (`--volumes`)

Volumes are placed into one of five categories:

| Category         | Description                                                                | Action                                    |
| ---------------- | -------------------------------------------------------------------------- | ----------------------------------------- |
| Active           | Currently mounted by a running container                                   | Protected                                 |
| ddev system      | `ddev-*` prefixed volumes (SSH agent, global cache, per-project services)  | Protected                                 |
| Project database | Claimed by a known ddev project, determined by reading `.ddev/config.yaml` | Protected                                 |
| Empty            | 0 bytes and unclaimed — never had data written                             | Deleted in bulk after single confirmation |
| Orphaned         | Has data but not claimed by any known project                              | Prompted individually                     |

Categorisation uses actual data (`docker system df --format json` and `.ddev/config.yaml`) rather than name patterns, so volumes from renamed or deleted projects surface correctly as orphaned rather than being silently skipped.

### Image cleanup (`--images`)

Runs `docker image prune -a`, which removes all images not referenced by any container. This includes images for stopped ddev projects — they will be re-pulled automatically on the next `ddev start`, though the first start will be slower.

Prompts for confirmation before running.

### Stale project cleanup (`--projects`)

Lists ddev projects that haven't been started in 6 or more months, sorted oldest first. Last-started time is read from the modification time of `.ddev/.ddev-docker-compose-base.yaml`, which ddev regenerates on every `ddev start`.

Each stale project is prompted individually (`yes` / `no` / `quit`). Confirmed projects are removed with:

```bash
ddev delete --omit-snapshot --yes <project>
```

This removes containers and the database volume but **does not touch your codebase or the `.ddev` folder**. The project can be re-added to ddev at any time.
