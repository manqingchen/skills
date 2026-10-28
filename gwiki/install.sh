#!/usr/bin/env bash
# Install the gwiki bundle (gwiki + gwiki-plus + gwiki-requirement-review +
# gwiki-update-skill + gwiki-lock + gwiki-unlock) like llm-wiki-skill/install.sh.
#
# Bundle layouts (auto-detected from the directory containing this script):
#   repo layout     <bundle>/skills/{gwiki,<secondaries>}   (git checkout)
#   flat new layout <bundle> IS the main gwiki skill (SKILL.md + bin/ + lib/ +
#                   ... at the root) with secondary skills at depth 1 — this
#                   is the grapehub zip layout since the install-layout fix;
#                   a grapehub install puts it at <target>/.zcode/skills/gwiki
#                   and this script spreads the secondaries next to it.
#   flat old layout <bundle>/{SKILL.md,gwiki/,<secondaries>} (grapehub zips
#                   <= 2.3.0; the real main skill lives in the nested gwiki/,
#                   the root SKILL.md is a stale copy — we install from gwiki/).
#
# gwiki-daily-report and gwiki-test are local-only: the release zip never
# ships them. Note that when this script runs from a repo checkout (skills/
# layout) it installs EVERY secondary skill directory it finds under skills/,
# including those two local-only entries (use install-gwiki-test.sh to
# install only the test entry).
set -euo pipefail

MODE="--copy"
DRY_RUN=0
FORCE=0
BACKUP=0
MISSING_ONLY=0
SYNC=0
CLIENT="auto"
DEST_OVERRIDE=""
BACKUP_DIR_OVERRIDE=""
BUNDLE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -d "$BUNDLE_ROOT/skills" ]]; then
  SRC_DIR="${GWIKI_SKILL_SRC_DIR:-$BUNDLE_ROOT/skills}"
else
  # Flat GrapeHub bundle layout: main skill + secondaries at zip root
  SRC_DIR="${GWIKI_SKILL_SRC_DIR:-$BUNDLE_ROOT}"
fi
DEPRECATED_SKILLS=("llm-wiki-cloud")

ALL_CLIENTS=("codex" "zcode" "claude" "cursor" "qoder" "trae" "workbuddy" "codebuddy" "minimax" "opencode")

usage() {
  cat <<'EOF' >&2
Usage: install.sh [--copy|--link] [--dry-run] [--force|--backup] [--missing-only|--sync]
                  [--client auto|zcode|codex|claude|cursor|qoder|trae|workbuddy|codebuddy|minimax|opencode|all]
                  [--dest <skills_dir>]
                  [--backup-dir <dir>]

Spreads the gwiki main skill plus every secondary skill as sibling
directories: <skills_dir>/gwiki, <skills_dir>/gwiki-plus, ...

--missing-only  only install skills whose target directory does not exist
                yet (never replaces an existing install)

--sync          version-aware spread: install missing skills, REPLACE
                outdated ones (installed version != bundle VERSION, old copy
                backed up first), keep up-to-date ones untouched. Covers both
                the first-use spread and re-alignment after
                `grapehub-cli update gwiki`: update only replaces
                <skills>/gwiki itself, never the spread secondary skills.

Agent attribution: with an EXPLICIT --client <key>, every copied SKILL.md
gets its baked `--agent <key>` usage-report flag re-baked to match the
destination client (mirrors grapehub-cli's own agent baking), so a bundle
installed under zcode does not keep reporting `--agent zcode` after being
spread to ~/.codex etc. With --client auto|all the copies stay byte-identical
to the source bundle.

Client default destinations (user-level scope):
  zcode     -> ${ZCODE_HOME:-$HOME/.zcode}/skills
  codex     -> ${CODEX_HOME:-$HOME/.codex}/skills
  claude    -> ${CLAUDE_HOME:-$HOME/.claude}/skills
  cursor    -> ${CURSOR_HOME:-$HOME/.cursor}/skills
  qoder     -> ${QODER_HOME:-$HOME/.qoder}/skills
  trae      -> ${TRAE_HOME:-$HOME/.trae}/skills
  workbuddy -> ${WORKBUDDY_HOME:-$HOME/.workbuddy}/skills
  codebuddy -> ${CODEBUDDY_HOME:-$HOME/.codebuddy}/skills
  minimax   -> ${MINIMAX_HOME:-$HOME/.minimax}/skills
  opencode  -> ${OPENCODE_HOME:-$HOME/.opencode}/skills

ZCode also supports a project-level scope: <project>/.zcode/skills. When the
bundle was installed there by grapehub (for example
<project>/.zcode/skills/gwiki), re-run this script from inside the bundle
with --dest pointing at that skills directory:
  bash <project>/.zcode/skills/gwiki/install.sh --copy --sync \
       --client zcode --dest <project>/.zcode/skills
EOF
}

contains() {
  local needle="$1"
  shift
  local item
  for item in "$@"; do
    [[ "$item" == "$needle" ]] && return 0
  done
  return 1
}

# Best-effort path normalization: absolute, no "./", symlinks in existing
# components resolved (macOS bash 3.2 safe; no realpath dependency).
norm_path() {
  local p="$1" parent base out
  p="${p%/}"
  if [[ -d "$p" ]]; then
    if out="$(cd -- "$p" 2>/dev/null && pwd)"; then
      printf '%s\n' "$out"
      return
    fi
  fi
  parent="$(dirname -- "$p")"
  base="$(basename -- "$p")"
  if [[ -d "$parent" ]]; then
    if out="$(cd -- "$parent" 2>/dev/null && pwd)"; then
      printf '%s\n' "$out/$base"
      return
    fi
  fi
  printf '%s\n' "$p"
}

# Best-effort extract of the version a SKILL.md claims to be. Covers both
# repo-authored files (`- version: X` self-managed section line,
# `currentVersion=X` version-check URL) and platform-injected installs
# (`--version X` baked report commands).
skill_md_version() {
  local md="$1" v=""
  if [[ -f "$md" ]]; then
    v="$(grep -oE -- '- version: [0-9][0-9A-Za-z._-]*|--version [0-9][0-9.-]*|currentVersion=[0-9][0-9.-]*' "$md" 2>/dev/null | head -1 | grep -oE '[0-9]+(\.[0-9]+)+' || true)"
  fi
  printf '%s' "$v"
}

# Re-bake the platform-injected `--agent <key>` usage-report flag in a copied
# SKILL.md to match the destination client. Mirrors grapehub-cli's own
# bakeAgentIntoSkillFiles (idempotent token rewrite; nothing else touched).
rebake_agent_in_skill_md() {
  local md="$1" client="$2"
  if ! command -v python3 >/dev/null 2>&1; then
    echo "warning: python3 not found; keeping existing --agent flag in $md" >&2
    return 0
  fi
  python3 - "$md" "$client" <<'PY'
import re
import sys

path, client = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as handle:
    text = handle.read()
new = re.sub(r"--agent [A-Za-z0-9_-]+", f"--agent {client}", text)
if new != text:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(new)
PY
}

default_dest_for_client() {
  local client="$1"
  case "$client" in
    zcode)     printf '%s\n' "${ZCODE_HOME:-$HOME/.zcode}/skills" ;;
    codex)     printf '%s\n' "${CODEX_HOME:-$HOME/.codex}/skills" ;;
    claude)    printf '%s\n' "${CLAUDE_HOME:-$HOME/.claude}/skills" ;;
    cursor)    printf '%s\n' "${CURSOR_HOME:-$HOME/.cursor}/skills" ;;
    qoder)     printf '%s\n' "${QODER_HOME:-$HOME/.qoder}/skills" ;;
    trae)      printf '%s\n' "${TRAE_HOME:-$HOME/.trae}/skills" ;;
    workbuddy) printf '%s\n' "${WORKBUDDY_HOME:-$HOME/.workbuddy}/skills" ;;
    codebuddy) printf '%s\n' "${CODEBUDDY_HOME:-$HOME/.codebuddy}/skills" ;;
    minimax)   printf '%s\n' "${MINIMAX_HOME:-$HOME/.minimax}/skills" ;;
    opencode)  printf '%s\n' "${OPENCODE_HOME:-$HOME/.opencode}/skills" ;;
    *) echo "Unsupported client: $client" >&2; exit 2 ;;
  esac
}

resolve_clients() {
  local clients=()
  case "$CLIENT" in
    auto)
      [[ -n "${ZCODE_HOME:-}" || -d "$HOME/.zcode" ]] && clients+=("zcode")
      [[ -n "${CODEX_HOME:-}" || -d "$HOME/.codex" ]] && clients+=("codex")
      [[ -n "${CLAUDE_HOME:-}" || -d "$HOME/.claude" ]] && clients+=("claude")
      [[ -n "${CURSOR_HOME:-}" || -d "$HOME/.cursor" ]] && clients+=("cursor")
      [[ -n "${QODER_HOME:-}" || -d "$HOME/.qoder" ]] && clients+=("qoder")
      [[ -n "${TRAE_HOME:-}" || -d "$HOME/.trae" ]] && clients+=("trae")
      [[ -n "${WORKBUDDY_HOME:-}" || -d "$HOME/.workbuddy" ]] && clients+=("workbuddy")
      [[ -n "${CODEBUDDY_HOME:-}" || -d "$HOME/.codebuddy" ]] && clients+=("codebuddy")
      [[ -n "${MINIMAX_HOME:-}" || -d "$HOME/.minimax" ]] && clients+=("minimax")
      [[ -n "${OPENCODE_HOME:-}" || -d "$HOME/.opencode" ]] && clients+=("opencode")
      ((${#clients[@]} == 0)) && clients=("codex")
      ;;
    all) clients=("${ALL_CLIENTS[@]}") ;;
    zcode|codex|claude|cursor|qoder|trae|workbuddy|codebuddy|minimax|opencode) clients=("$CLIENT") ;;
    *) echo "Unsupported --client value: $CLIENT" >&2; exit 2 ;;
  esac
  printf '%s\n' "${clients[@]}"
}

resolve_destinations() {
  local resolved=()
  local client dest
  while IFS= read -r client; do
    [[ -n "$client" ]] || continue
    if [[ -n "$DEST_OVERRIDE" ]]; then
      dest="$DEST_OVERRIDE"
    else
      dest="$(default_dest_for_client "$client")"
    fi
    if ((${#resolved[@]} == 0)) || ! contains "$dest" "${resolved[@]}"; then
      resolved+=("$dest")
    fi
  done < <(resolve_clients)
  if ((${#resolved[@]} > 0)); then
    printf '%s\n' "${resolved[@]}"
  fi
}

while (($#)); do
  case "$1" in
    --copy|--link) MODE="$1" ;;
    --dry-run) DRY_RUN=1 ;;
    --force) FORCE=1 ;;
    --backup) BACKUP=1 ;;
    --missing-only) MISSING_ONLY=1 ;;
    --sync) SYNC=1 ;;
    --client)
      shift
      [[ $# -gt 0 ]] || { echo "Missing value for --client" >&2; exit 2; }
      CLIENT="$1"
      ;;
    --dest)
      shift
      [[ $# -gt 0 ]] || { echo "Missing value for --dest" >&2; exit 2; }
      DEST_OVERRIDE="$1"
      ;;
    --backup-dir)
      shift
      [[ $# -gt 0 ]] || { echo "Missing value for --backup-dir" >&2; exit 2; }
      BACKUP_DIR_OVERRIDE="$1"
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
  shift
done

if [[ "$FORCE" -eq 1 && "$BACKUP" -eq 1 ]]; then
  echo "Choose only one of --force or --backup." >&2
  exit 2
fi

if [[ "$MISSING_ONLY" -eq 1 && "$SYNC" -eq 1 ]]; then
  echo "Choose only one of --missing-only or --sync." >&2
  exit 2
fi

# Explicit --client <key>: copied SKILL.md files get their baked --agent flag
# re-baked to that key (cross-client attribution fix). auto/all keep copies
# byte-identical because a single run may target several clients.
REBAKE_CLIENT=""
if [[ "$CLIENT" != "auto" && "$CLIENT" != "all" ]]; then
  REBAKE_CLIENT="$CLIENT"
fi

if [[ -n "$DEST_OVERRIDE" && ! -d "$DEST_OVERRIDE" ]]; then
  # Allow not-yet-existing dest (we mkdir below), but reject empty values.
  [[ -n "${DEST_OVERRIDE// /}" ]] || { echo "Empty --dest value." >&2; exit 2; }
fi

if [[ ! -d "$SRC_DIR" ]]; then
  echo "Missing skills directory: $SRC_DIR" >&2
  exit 1
fi

# Bundle version (authoritative "what version are we spreading" anchor).
BUNDLE_VERSION="$(sed -n 's/^version: //p' "$BUNDLE_ROOT/VERSION" 2>/dev/null | head -1 | tr -d '\r\n' || true)"

# --- Layout detection: where does the MAIN gwiki skill live? ---------------
# Prefer a directory that actually carries the runtime (bin/gwiki.js).
if [[ -f "$SRC_DIR/gwiki/SKILL.md" && -f "$SRC_DIR/gwiki/bin/gwiki.js" ]]; then
  MAIN_SRC="$SRC_DIR/gwiki"          # repo layout or legacy flat bundle
elif [[ -f "$SRC_DIR/SKILL.md" && -f "$SRC_DIR/bin/gwiki.js" ]]; then
  MAIN_SRC="$SRC_DIR"                # root-as-main flat bundle (new layout)
else
  echo "Cannot locate the main gwiki skill (SKILL.md + bin/gwiki.js) under: $SRC_DIR" >&2
  exit 1
fi

# --- Collect secondary skills (depth-1 SKILL.md dirs, excluding the main) --
SKILL_NAMES=("gwiki")
SKILL_SRCS=("$MAIN_SRC")
SECONDARY_SRCS=()
entry_dir=""
entry_name=""
for entry_dir in "$SRC_DIR"/*; do
  [[ -d "$entry_dir" ]] || continue
  entry_name="$(basename "$entry_dir")"
  [[ "$entry_name" == .* ]] && continue
  [[ -f "$entry_dir/SKILL.md" ]] || continue
  [[ "$(norm_path "$entry_dir")" == "$(norm_path "$MAIN_SRC")" ]] && continue
  SECONDARY_SRCS+=("$entry_dir")
  SKILL_NAMES+=("$entry_name")
  SKILL_SRCS+=("$entry_dir")
done

is_secondary_source() {
  local candidate="$1" other
  for other in "${SECONDARY_SRCS[@]:-}"; do
    [[ -n "$other" && "$other" == "$candidate" ]] && return 0
  done
  return 1
}

# Copy the main skill when the bundle root itself is the main skill: copy the
# root's entries but keep secondary skills, hidden install metadata
# (.grapehub-install.json etc.) and OS noise out of the target.
copy_main_from_root() {
  local dest_dir="$1"
  local target="$dest_dir/gwiki"
  local entry base
  mkdir -p "$target"
  for entry in "$MAIN_SRC"/*; do
    base="$(basename "$entry")"
    [[ "$base" == .* ]] && continue
    if [[ -d "$entry" ]] && is_secondary_source "$entry"; then
      continue
    fi
    cp -R "$entry" "$target/"
  done
}

install_skill() {
  local skill_dir="$1"
  local name="$2"
  local dest_dir="$3"
  local target="$dest_dir/$name"
  case "$MODE" in
    --copy)
      if [[ "$name" == "gwiki" && "$skill_dir" == "$MAIN_SRC" && "$MAIN_SRC" == "$SRC_DIR" ]]; then
        copy_main_from_root "$dest_dir"
      else
        cp -R "$skill_dir" "$target"
      fi
      ;;
    --link) ln -s "$skill_dir" "$target" ;;
  esac
  if [[ -n "$REBAKE_CLIENT" && "$MODE" == "--copy" && -f "$target/SKILL.md" ]]; then
    rebake_agent_in_skill_md "$target/SKILL.md" "$REBAKE_CLIENT"
  fi
  if [[ "$name" == "gwiki" && -f "$target/bin/gwiki.js" ]]; then
    chmod +x "$target/bin/gwiki.js"
  fi
}

backup_target_for() {
  local name="$1"
  local backup_dir="$2"
  local timestamp candidate counter=2
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  candidate="$backup_dir/$name-$timestamp"
  while [[ -e "$candidate" ]]; do
    candidate="$backup_dir/$name-$timestamp-$counter"
    counter=$((counter + 1))
  done
  echo "$candidate"
}

backup_dir_for_dest() {
  if [[ -n "$BACKUP_DIR_OVERRIDE" ]]; then
    printf '%s\n' "$BACKUP_DIR_OVERRIDE"
  else
    printf '%s\n' "${GWIKI_SKILL_BACKUP_DIR:-$HOME/.gwiki-skill-backups}"
  fi
}

validate_skill() {
  local target="$1"
  if [[ -f "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" ]]; then
    python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" "$target"
  fi
}

install_for_dest() {
  local dest_dir="$1"
  local backup_dir name target backup_target skill_dir target_norm src_norm
  mkdir -p "$dest_dir"
  backup_dir="$(backup_dir_for_dest "$dest_dir")"

  local dest_norm src_norm
  dest_norm="$(norm_path "$dest_dir")"
  src_norm="$(norm_path "$SRC_DIR")"

  # Self-conflict guards (feedback problem 6):
  # 1) dest inside (or equal to) the bundle source would nest the install
  #    inside the very files being copied -> refuse.
  if [[ "$dest_norm" == "$src_norm" || "$dest_norm" == "$src_norm"/* ]]; then
    echo "Refusing to install into $dest_dir: it is inside the bundle source $SRC_DIR." >&2
    exit 1
  fi
  if [[ "$dest_norm" == "/" ]]; then
    echo "Refusing to install into /." >&2
    exit 1
  fi
  # 2) --link into an ancestor of the bundle source (e.g. the in-place
  #    grapehub spread) would leave symlinks pointing into the bundle that
  #    a later `grapehub-cli update` (rm + cp of the whole bundle dir)
  #    breaks -> refuse, use --copy.
  if [[ "$MODE" == "--link" && ( "$src_norm" == "$dest_norm" || "$src_norm" == "$dest_norm"/* ) ]]; then
    echo "Refusing --link into $dest_dir: the bundle source $SRC_DIR lives inside it (in-place spread); use --copy." >&2
    exit 1
  fi

  for deprecated in "${DEPRECATED_SKILLS[@]}"; do
    target="$dest_dir/$deprecated"
    [[ -e "$target" ]] || continue
    if [[ "$FORCE" -eq 1 ]]; then
      rm -rf "$target"
      echo "removed deprecated $deprecated"
    elif [[ "$BACKUP" -eq 1 ]]; then
      backup_target="$(backup_target_for "$deprecated" "$backup_dir")"
      mkdir -p "$(dirname "$backup_target")"
      mv "$target" "$backup_target"
      echo "backed up deprecated $deprecated to $backup_target"
    fi
  done

  local i
  local installed_ver
  for i in "${!SKILL_NAMES[@]}"; do
    name="${SKILL_NAMES[$i]}"
    skill_dir="${SKILL_SRCS[$i]}"
    target="$dest_dir/$name"
    target_norm="$(norm_path "$target")"
    src_norm="$(norm_path "$skill_dir")"

    if [[ "$MISSING_ONLY" -eq 1 && -e "$target" ]]; then
      echo "keep existing $name (missing-only)"
      continue
    fi

    # 3) never move/rm/copy onto the source itself (in-place spread: e.g.
    #    .zcode/skills/gwiki installed by grapehub, dest=.zcode/skills — the
    #    main skill target IS the bundle root). Skipping keeps it in place.
    if [[ "$target_norm" == "$src_norm" ]]; then
      echo "skip $name: already in place at $target (target == source)"
      continue
    fi

    # 4) version-aware spread (--sync): up-to-date targets are skipped;
    #    outdated ones fall through to the backup/replace branch below.
    #    Covers post-`grapehub-cli update` realignment: update replaces only
    #    <skills>/gwiki, never the spread secondary skills.
    if [[ "$SYNC" -eq 1 && -e "$target" ]]; then
      installed_ver=""
      if [[ "$name" == "gwiki" && -f "$target/VERSION" ]]; then
        installed_ver="$(sed -n 's/^version: //p' "$target/VERSION" | head -1 | tr -d '\r\n' || true)"
      fi
      if [[ -z "$installed_ver" ]]; then
        installed_ver="$(skill_md_version "$target/SKILL.md")"
      fi
      if [[ -z "$BUNDLE_VERSION" ]]; then
        echo "keep $name (cannot read bundle VERSION; --sync cannot compare)"
        continue
      fi
      if [[ -z "$installed_ver" ]]; then
        echo "keep $name (cannot determine installed version; pass --force or --backup to replace)"
        continue
      fi
      if [[ "$installed_ver" == "$BUNDLE_VERSION" ]]; then
        echo "up to date $name ($installed_ver)"
        continue
      fi
      echo "outdated $name ($installed_ver -> $BUNDLE_VERSION)"
    fi

    if [[ -e "$target" ]]; then
      if [[ "$FORCE" -eq 1 ]]; then
        rm -rf "$target"
        install_skill "$skill_dir" "$name" "$dest_dir"
        echo "replaced $name"
      elif [[ "$BACKUP" -eq 1 ]]; then
        backup_target="$(backup_target_for "$name" "$backup_dir")"
        mkdir -p "$(dirname "$backup_target")"
        mv "$target" "$backup_target"
        echo "backed up $name to $backup_target"
        install_skill "$skill_dir" "$name" "$dest_dir"
      fi
    else
      install_skill "$skill_dir" "$name" "$dest_dir"
    fi
    validate_skill "$target"
  done
}

DEST_DIRS=()
while IFS= read -r dest_dir; do
  [[ -n "$dest_dir" ]] || continue
  DEST_DIRS+=("$dest_dir")
done < <(resolve_destinations)

if ((${#DEST_DIRS[@]} == 0)); then
  echo "No install destination resolved." >&2
  exit 2
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  mode_note="replace-if-exists"
  [[ "$MISSING_ONLY" -eq 1 ]] && mode_note="missing-only"
  [[ "$SYNC" -eq 1 ]] && mode_note="sync (install missing, replace outdated, keep up-to-date)"
  for dest_dir in "${DEST_DIRS[@]}"; do
    echo "dry-run: would install ${#SKILL_NAMES[@]} gwiki-family skills (main from $MAIN_SRC, mode: $mode_note) into $dest_dir"
  done
  exit 0
fi

if [[ "$FORCE" -eq 0 && "$BACKUP" -eq 0 && "$MISSING_ONLY" -eq 0 ]]; then
  BACKUP=1
fi

for dest_dir in "${DEST_DIRS[@]}"; do
  install_for_dest "$dest_dir"
done

echo "Installed gwiki skills into:"
for dest_dir in "${DEST_DIRS[@]}"; do
  echo "  - $dest_dir"
done
