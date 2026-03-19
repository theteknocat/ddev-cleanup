#!/usr/bin/env python3
"""Docker volume and image cleanup tool for ddev environments.

Categorises volumes by actual usage rather than name patterns:
  active   — currently mounted by a running container
  ddev     — ddev-* system volumes (global or per-project services)
  project  — claimed by a known ddev project (read from .ddev/config.yaml)
  empty    — 0 bytes and unclaimed: safe to delete
  orphaned — has data but unclaimed: flagged for manual review
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── ANSI colours ──────────────────────────────────────────────────────────────

RED    = '\033[0;31m'
GREEN  = '\033[0;32m'
YELLOW = '\033[1;33m'
BLUE   = '\033[0;34m'
NC     = '\033[0m'

def success(msg: str) -> None: print(f"{GREEN}✓{NC} {msg}")
def warn(msg: str)    -> None: print(f"{YELLOW}⚠{NC} {msg}")
def error(msg: str)   -> None: print(f"{RED}✗{NC} {msg}", file=sys.stderr)

def header(title: str) -> None:
    bar = "━" * 54
    print(f"\n{BLUE}{bar}{NC}")
    print(f"{BLUE}{title}{NC}")
    print(f"{BLUE}{bar}{NC}\n")

ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*m')

def visible_len(s: str) -> int:
    return len(ANSI_ESCAPE.sub('', str(s)))

def table(rows: list[tuple], indent: int = 2) -> None:
    """Print rows as an aligned table. First row is the header."""
    if not rows:
        return
    widths = [max(visible_len(r[i]) for r in rows) for i in range(len(rows[0]))]
    pad = " " * indent
    for row in rows:
        print(pad + "  ".join(str(cell).ljust(widths[i] + len(str(cell)) - visible_len(str(cell))) for i, cell in enumerate(row)))

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Volume:
    name: str
    links: int          # containers currently mounting this volume
    size_bytes: int
    size_str: str
    claimed_by: Optional[str] = None  # ddev project name, if applicable

def parse_size(s: str) -> int:
    """Convert Docker size string (e.g. '506.9MB', '0B') to bytes."""
    units = {'B': 1, 'kB': 1_000, 'MB': 1_000**2, 'GB': 1_000**3, 'TB': 1_000**4}
    m = re.match(r'^([\d.]+)([a-zA-Z]+)$', s)
    if not m:
        return 0
    return int(float(m.group(1)) * units.get(m.group(2), 1))

# ── Docker data fetching ───────────────────────────────────────────────────────

def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True)

def get_volumes() -> dict[str, Volume]:
    """Return all Docker volumes with link count and size."""
    r = run('docker', 'system', 'df', '-v', '--format', 'json')
    data = json.loads(r.stdout)
    return {
        v['Name']: Volume(
            name=v['Name'],
            links=int(v.get('Links', 0)),
            size_bytes=parse_size(v['Size']),
            size_str=v['Size'],
        )
        for v in data.get('Volumes', [])
    }

def get_ddev_projects() -> list[dict]:
    """Return raw ddev project list, or empty list if ddev is unavailable."""
    if run('which', 'ddev').returncode != 0:
        return []
    r = run('ddev', 'list', '-j')
    if r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout).get('raw') or []
    except (json.JSONDecodeError, AttributeError):
        return []

def get_ddev_claimed_volumes(projects: list[dict]) -> dict[str, str]:
    """
    For each ddev project, determine its expected database volume by reading
    .ddev/config.yaml. Returns {volume_name: project_name}.
    """
    claimed: dict[str, str] = {}
    for p in projects:
        name    = p.get('name', '')
        approot = p.get('approot', '')
        if not name or not approot:
            continue

        dbtype = 'mariadb'  # ddev default when no database section exists
        config = Path(approot) / '.ddev' / 'config.yaml'
        if config.exists():
            in_db = False
            for line in config.read_text().splitlines():
                if re.match(r'^database:', line):
                    in_db = True
                elif in_db:
                    m = re.match(r'\s+type:\s*(\S+)', line)
                    if m:
                        dbtype = m.group(1)
                        break
                    if not line.startswith(' '):
                        break  # left the database section without finding type

        claimed[f"{name}-{dbtype}"] = name
    return claimed

# ── Categorisation ────────────────────────────────────────────────────────────

def categorize(
    volumes: dict[str, Volume],
    claimed: dict[str, str],
) -> tuple[dict, dict, dict, dict, dict]:
    """
    Split volumes into five mutually exclusive buckets:
      active   – links > 0 (a running container has it mounted)
      ddev     – ddev-* prefix (system and service volumes)
      project  – claimed by a known ddev project config
      empty    – 0 bytes, unclaimed: safe to delete
      orphaned – has data, unclaimed: needs manual review
    """
    active:   dict[str, Volume] = {}
    ddev:     dict[str, Volume] = {}
    project:  dict[str, Volume] = {}
    empty:    dict[str, Volume] = {}
    orphaned: dict[str, Volume] = {}

    for name, vol in volumes.items():
        if vol.links > 0:
            active[name] = vol
        elif name.startswith('ddev-'):
            ddev[name] = vol
        elif name in claimed:
            vol.claimed_by = claimed[name]
            project[name] = vol
        elif vol.size_bytes == 0:
            empty[name] = vol
        else:
            orphaned[name] = vol

    return active, ddev, project, empty, orphaned

# ── Status views ──────────────────────────────────────────────────────────────

def get_last_started(approot: str) -> Optional[datetime]:
    """Return when the project was last started, via the mtime of ddev's generated compose file."""
    compose = Path(approot) / '.ddev' / '.ddev-docker-compose-base.yaml'
    if compose.exists():
        return datetime.fromtimestamp(compose.stat().st_mtime)
    return None

def format_age(dt: datetime) -> tuple[str, bool]:
    """
    Return a human-readable age string and a boolean indicating whether
    it should be highlighted as stale (> 90 days).
    """
    days = (datetime.now() - dt).days
    if days == 0:
        return "today", False
    elif days < 7:
        return f"{days}d ago", False
    elif days < 30:
        return f"{days // 7}w ago", False
    elif days < 365:
        months = days // 30
        return f"{months}mo ago", months >= 6
    else:
        years = days // 365
        return f"{years}y ago", True

def show_disk_usage() -> None:
    header("Docker Disk Usage")
    print(run('docker', 'system', 'df').stdout)

def show_ddev_projects(projects: list[dict]) -> None:
    header("ddev Projects")
    if not projects:
        print("  No ddev projects found\n")
        return

    # Collect rows with last-started datetime for sorting
    entries = []
    for p in projects:
        last_started = get_last_started(p.get('approot', ''))
        entries.append((p, last_started))

    # Sort: never-started first, then oldest to newest
    entries.sort(key=lambda x: x[1] or datetime.min)

    rows: list[tuple] = [("NAME", "STATUS", "LAST STARTED", "LOCATION")]
    for p, last_started in entries:
        if last_started:
            age_str, stale = format_age(last_started)
            age_display = f"{YELLOW}{age_str}{NC}" if stale else age_str
        else:
            age_display, stale = f"{YELLOW}never{NC}", True
        rows.append((
            p.get('name', ''),
            p.get('status', ''),
            age_display,
            p.get('shortroot', p.get('approot', '')),
        ))

    table(rows)
    print()

def show_volumes_report(volumes: dict[str, Volume], claimed: dict[str, str]) -> None:
    header("Volume Analysis")
    active, ddev, project, empty, orphaned = categorize(volumes, claimed)
    print(f"  Total volumes: {BLUE}{len(volumes)}{NC}\n")

    def names_only(label: str, color: str, vols: dict[str, Volume]) -> None:
        print(f"{color}{label}:{NC}")
        if vols:
            for v in sorted(vols.values(), key=lambda x: x.name):
                print(f"  • {v.name}")
        else:
            print("  None")
        print()

    names_only("Active — mounted by running container", GREEN, active)
    names_only("ddev system volumes (protected)", GREEN, ddev)

    print(f"{GREEN}Project database volumes (protected):{NC}")
    if project:
        rows: list[tuple] = [("VOLUME", "SIZE", "PROJECT")]
        for v in sorted(project.values(), key=lambda x: x.name):
            rows.append((v.name, v.size_str, v.claimed_by or ''))
        table(rows)
    else:
        print("  None")
    print()

    print(f"{YELLOW}Empty volumes — safe to delete ({len(empty)}):{NC}")
    if empty:
        for v in sorted(empty.values(), key=lambda x: x.name):
            print(f"  • {v.name}")
    else:
        print("  None")
    print()

    if orphaned:
        print(f"{YELLOW}Unclaimed volumes with data — review manually ({len(orphaned)}):{NC}")
        rows = [("VOLUME", "SIZE")]
        for v in sorted(orphaned.values(), key=lambda x: x.name):
            rows.append((v.name, v.size_str))
        table(rows)
        print()

# ── Cleanup ───────────────────────────────────────────────────────────────────

def cleanup_volumes(volumes: dict[str, Volume], claimed: dict[str, str], dry_run: bool) -> None:
    header("Volume Cleanup")
    _, _, _, empty, orphaned = categorize(volumes, claimed)

    if not empty and not orphaned:
        success("No volumes to clean up — system is clean!")
        return

    # Empty volumes — list and confirm once
    if empty:
        print(f"  Empty volumes ({YELLOW}{len(empty)}{NC}) — will be removed:")
        for v in sorted(empty.values(), key=lambda x: x.name):
            print(f"  • {v.name}")
        print()

    # Orphaned volumes — show for prompting
    if orphaned:
        print(f"  Unclaimed volumes with data ({YELLOW}{len(orphaned)}{NC}) — you will be prompted for each:")
        rows: list[tuple] = [("VOLUME", "SIZE")]
        for v in sorted(orphaned.values(), key=lambda x: x.name):
            rows.append((v.name, v.size_str))
        table(rows, indent=4)
        print()

    if dry_run:
        warn("DRY RUN: Run with --execute to actually delete volumes")
        return

    # Delete empty volumes in bulk
    if empty:
        answer = input(f"Delete {len(empty)} empty volume(s)? (yes/no): ").strip()
        if answer == 'yes':
            print()
            for v in sorted(empty.values(), key=lambda x: x.name):
                r = run('docker', 'volume', 'rm', v.name)
                if r.returncode == 0:
                    success(f"Deleted: {v.name}")
                else:
                    error(f"Failed: {v.name} — {r.stderr.strip()}")
            print()
        else:
            warn("Empty volume deletion skipped")
            print()

    # Prompt individually for orphaned volumes
    if orphaned:
        print("Reviewing unclaimed volumes with data:\n")
        for v in sorted(orphaned.values(), key=lambda x: x.name):
            answer = input(f"  Delete {v.name} ({v.size_str})? (yes/no/quit): ").strip()
            if answer == 'quit':
                warn("Stopped at user request")
                break
            elif answer == 'yes':
                r = run('docker', 'volume', 'rm', v.name)
                if r.returncode == 0:
                    success(f"Deleted: {v.name}")
                else:
                    error(f"Failed: {v.name} — {r.stderr.strip()}")
            else:
                print(f"  Skipped: {v.name}")
        print()

    success("Volume cleanup complete!")

def cleanup_images(dry_run: bool) -> None:
    header("Image Cleanup")

    r = run('docker', 'images', '-f', 'dangling=true', '--format', 'json')
    dangling = [json.loads(line) for line in r.stdout.splitlines() if line.strip()]

    print("  'docker image prune -a' removes ALL images not referenced by any container,")
    print("  including images for stopped ddev projects — they will re-pull on next 'ddev start'.\n")

    print(f"  Dangling images ({YELLOW}{len(dangling)}{NC}):")
    if dangling:
        rows = [("IMAGE ID", "SIZE", "CREATED")] + [(d['ID'], d['Size'], d['CreatedSince']) for d in dangling]
        table(rows, indent=4)
    else:
        print("    None")
    print()

    if dry_run:
        warn("DRY RUN: Run with --execute to actually clean up images")
        return

    answer = input("Run 'docker image prune -a'? (yes/no): ").strip()
    if answer != 'yes':
        warn("Image cleanup cancelled")
        return

    print()
    subprocess.run(['docker', 'image', 'prune', '-a', '-f'])
    print()
    success("Image cleanup complete!")

def cleanup_stale_projects(projects: list[dict], dry_run: bool) -> None:
    header("Stale Project Cleanup")

    stale = []
    for p in projects:
        last_started = get_last_started(p.get('approot', ''))
        if last_started is None:
            stale.append((p, None, "never started"))
        else:
            days = (datetime.now() - last_started).days
            if days >= 180:
                age_str, _ = format_age(last_started)
                stale.append((p, last_started, age_str))

    if not stale:
        success("No projects idle for 6+ months")
        return

    stale.sort(key=lambda x: x[1] or datetime.min)

    print(f"  Projects not started in 6+ months ({YELLOW}{len(stale)}{NC}):\n")
    rows: list[tuple] = [("PROJECT", "LAST STARTED", "LOCATION")]
    for p, _, age_str in stale:
        rows.append((p.get('name', ''), age_str, p.get('shortroot', p.get('approot', ''))))
    table(rows)
    print()

    if dry_run:
        warn("DRY RUN: Run with --execute to be prompted for each project")
        print("  Note: 'ddev delete' removes containers and database but not your codebase.")
        return

    print("  'ddev delete' removes containers and database — your codebase is untouched.\n")
    for p, _, age_str in stale:
        name = p.get('name', '')
        answer = input(f"  Delete project '{name}' (last started: {age_str})? (yes/no/quit): ").strip()
        if answer == 'quit':
            warn("Stopped at user request")
            break
        elif answer == 'yes':
            r = run('ddev', 'delete', '--omit-snapshot', '--yes', name)
            if r.returncode == 0:
                success(f"Deleted project: {name}")
            else:
                error(f"Failed: {name} — {r.stderr.strip()}")
        else:
            print(f"  Skipped: {name}")
    print()

# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Docker cleanup for ddev environments.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s                  full cleanup — volumes, images, stale projects (with prompts)
  %(prog)s --dry-run        show what would be done without making any changes
  %(prog)s --status         show disk usage, project list, and volume analysis
  %(prog)s --volumes        volumes only
  %(prog)s --projects       stale projects only (idle 6+ months)
  %(prog)s --images         images only
""",
    )
    parser.add_argument('-s', '--status',   action='store_true', help='Show status report (no cleanup)')
    parser.add_argument('-v', '--volumes',  action='store_true', help='Clean up volumes only')
    parser.add_argument('-i', '--images',   action='store_true', help='Clean up images only')
    parser.add_argument('-p', '--projects', action='store_true', help='Clean up stale projects only (idle 6+ months)')
    parser.add_argument('-a', '--all',      action='store_true', help='Clean up volumes, images, and stale projects')
    parser.add_argument('-d', '--dry-run',  action='store_true', help='Show what would be done without making changes')
    args = parser.parse_args()

    dry_run = args.dry_run

    if dry_run:
        warn("DRY RUN MODE — no changes will be made\n")

    # Fetch everything once upfront
    volumes  = get_volumes()
    projects = get_ddev_projects()
    claimed  = get_ddev_claimed_volumes(projects)

    if args.status:
        show_disk_usage()
        show_ddev_projects(projects)
        show_volumes_report(volumes, claimed)
        return

    # Default (no flags) runs everything, otherwise run what was requested
    no_action = not any([args.volumes, args.images, args.projects, args.all])
    run_volumes  = args.volumes  or args.all or no_action
    run_images   = args.images   or args.all or no_action
    run_projects = args.projects or args.all or no_action

    show_disk_usage()

    if run_volumes:
        cleanup_volumes(volumes, claimed, dry_run)

    if run_images:
        cleanup_images(dry_run)

    if run_projects:
        cleanup_stale_projects(projects, dry_run)

    if not dry_run:
        header("Final Disk Usage")
        print(run('docker', 'system', 'df').stdout)

if __name__ == '__main__':
    main()
