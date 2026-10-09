#!/usr/bin/env bash
# Install this repo's Claude Code skills into ~/.claude/skills and check the
# delegate CLIs. Safe to re-run.
#
#   ./install.sh            symlink skills (edits in the repo apply immediately)
#   ./install.sh --copy     copy instead (for machines where the repo may move)
#   ./install.sh --bin      also put `delegate` on PATH via ~/.local/bin
#   ./install.sh --check    only check prerequisites
#   ./install.sh --force    replace an existing non-symlinked skill dir (it is
#                           moved to ~/.claude/skill-backups/, never deleted)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SKILLS_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
BACKUP_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skill-backups"
mode=link bin=0 check_only=0 force=0 failed=0
for arg in "$@"; do
  case "$arg" in
    --copy) mode=copy ;;
    --bin) bin=1 ;;
    --check) check_only=1 ;;
    --force) force=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }
miss() { printf '  \033[31mmiss\033[0m  %s\n' "$*"; }

check_prereqs() {
  echo "prerequisites:"
  if command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
    ok "python3 $(python3 -c 'import platform; print(platform.python_version())')"
  else
    miss "python3 >= 3.9 (the delegate CLI is stdlib-only Python)"
  fi
  command -v git >/dev/null && ok "git" || miss "git"
  if command -v openusage >/dev/null; then
    ok "openusage $(openusage version 2>/dev/null | head -1)"
  else
    miss "openusage: needed by 'delegate quota/route'. See https://github.com/janekbaraniewski/openusage"
  fi
  if command -v codex >/dev/null; then
    if codex login status >/dev/null 2>&1; then ok "codex $(codex --version 2>/dev/null | awk '{print $NF}'), logged in"
    else warn "codex installed but not logged in: run 'codex login'"; fi
  else
    miss "codex: npm i -g @openai/codex (then 'codex login')"
  fi
  if command -v cursor-agent >/dev/null; then
    if cursor-agent status 2>/dev/null | grep -qi 'logged in as'; then ok "cursor-agent $(cursor-agent --version 2>/dev/null), logged in"
    else warn "cursor-agent installed but not logged in: run 'cursor-agent login' and approve in the browser"; fi
  else
    miss "cursor-agent: curl https://cursor.com/install -fsS | bash (read it first), then 'cursor-agent login'"
  fi
  if command -v claude >/dev/null && claude plugin list 2>/dev/null | grep -q 'codex@openai-codex'; then
    ok "codex plugin for Claude Code (/codex:review, /codex:adversarial-review)"
  else
    warn "optional: Codex plugin (needs node >= 18.18): claude plugin marketplace add openai/codex-plugin-cc && claude plugin install codex@openai-codex"
  fi
  [ -n "${ARTIFICIAL_ANALYSIS_API_KEY:-}" ] && ok "ARTIFICIAL_ANALYSIS_API_KEY set (official AA coding index)" \
    || warn "ARTIFICIAL_ANALYSIS_API_KEY unset: ratings come from the AA leaderboard page (coding = proxy)"
}

install_skill() {
  local src="$1" name target
  name="$(basename "$src")"
  target="$SKILLS_DIR/$name"
  if [ -L "$target" ]; then
    if [ "$(readlink -f "$target")" = "$(readlink -f "$src")" ] && [ "$mode" = link ]; then
      ok "$name (already linked)"; return
    fi
    rm "$target"
  elif [ -e "$target" ]; then
    if [ "$force" = 0 ]; then
      miss "$name: $target exists and is not a symlink; re-run with --force to back it up and replace it"
      failed=1
      return
    fi
    mkdir -p "$BACKUP_DIR"
    mv "$target" "$BACKUP_DIR/$name-$(date +%Y%m%d-%H%M%S)"
    warn "$name: previous copy moved to $BACKUP_DIR/"
  fi
  if [ "$mode" = link ]; then
    ln -s "$src" "$target"
    ok "$name -> $src"
  else
    cp -a "$src" "$target"
    find "$target" -name __pycache__ -type d -prune -exec rm -r {} +
    ok "$name copied to $target"
  fi
}

check_prereqs
[ "$check_only" = 1 ] && exit 0

echo "skills ($mode into $SKILLS_DIR):"
mkdir -p "$SKILLS_DIR"
for dir in "$REPO"/skills/*/; do
  [ -f "$dir/SKILL.md" ] && install_skill "${dir%/}"
done

if [ "$bin" = 1 ]; then
  mkdir -p "$HOME/.local/bin"
  ln -sfn "$SKILLS_DIR/delegate/scripts/delegate" "$HOME/.local/bin/delegate"
  ok "delegate on PATH via ~/.local/bin/delegate"
fi
if [ "$failed" = 1 ]; then echo "some skills were not installed (see above)" >&2; exit 1; fi
echo "done. Claude Code picks up new skills without a restart."
