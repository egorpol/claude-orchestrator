#!/usr/bin/env bash
# Install this repo's Claude Code skills into ~/.claude/skills and check the
# delegate CLIs. Safe to re-run.
#
#   ./install.sh            symlink skills (edits in the repo apply immediately)
#   ./install.sh --copy     copy instead (for machines where the repo may move)
#   ./install.sh --bin      also put `delegate` on PATH via ~/.local/bin
#   ./install.sh --hooks    also register hooks/context-guard.py as a UserPromptSubmit
#                           hook in settings.json (handoff nudge at item boundaries past ~120k)
#   ./install.sh --rules    also import rules/orchestrator.md from the user-level CLAUDE.md,
#                           so every session gets the orchestrator rules (stays live from the repo)
#   ./install.sh --timer    install an activity-gated quota timer (every 10 minutes)
#                           remove: systemctl --user disable --now claude-delegate-quota.timer
#                           then delete claude-delegate-quota.{service,timer} from
#                           ${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/
#   ./install.sh --check    only check prerequisites
#   ./install.sh --force    replace an existing non-symlinked skill dir (it is
#                           moved to ~/.claude/skill-backups/, never deleted)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SKILLS_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
BACKUP_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skill-backups"
SETTINGS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
USER_MD="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/CLAUDE.md"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mode=link bin=0 hooks=0 rules=0 timer=0 check_only=0 force=0 failed=0
for arg in "$@"; do
  case "$arg" in
    --copy) mode=copy ;;
    --bin) bin=1 ;;
    --hooks) hooks=1 ;;
    --rules) rules=1 ;;
    --timer) timer=1 ;;
    --check) check_only=1 ;;
    --force) force=1 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
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
  if grep -qs 'hooks/context-guard.py' "$SETTINGS"; then ok "context-guard hook registered in $SETTINGS"
  else warn "optional: context-guard hook not registered (./install.sh --hooks)"; fi
  if grep -qsF "@$REPO/rules/orchestrator.md" "$USER_MD"; then ok "orchestrator rules imported in $USER_MD"
  else warn "optional: orchestrator rules not imported (./install.sh --rules)"; fi
}

check_timer() {
  if command -v systemctl >/dev/null; then
    if systemctl --user is-enabled claude-delegate-quota.timer >/dev/null 2>&1; then
      ok "quota timer enabled"
    else
      warn "optional: quota timer not enabled (./install.sh --timer)"
    fi
  elif [ -f "$UNIT_DIR/claude-delegate-quota.timer" ]; then
    ok "quota timer unit exists (systemctl unavailable)"
  else
    warn "optional: quota timer unit not installed (./install.sh --timer)"
  fi
}

install_timer() {
  local python provider_bin unit_path
  python="$(python3 -c 'import os, sys; print(os.path.abspath(sys.executable))')"
  unit_path="/usr/local/bin:/usr/bin:/bin"
  if provider_bin="$(command -v openusage)"; then
    unit_path="$(cd "$(dirname "$provider_bin")" && pwd -P):$unit_path"
  else
    warn "openusage not found; quota timer PATH uses system directories only"
  fi
  mkdir -p "$UNIT_DIR"
  # Quote and escape systemd arguments, including paths containing spaces or %.
  python3 - "$UNIT_DIR" "$python" "$SKILLS_DIR/delegate/scripts/delegate" "$unit_path" <<'PY_TIMER'
from pathlib import Path
import os
import sys

directory, python, delegate, path = sys.argv[1:]
def quoted(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
# The timer must watch the same transcripts and write the same log as the session hook.
assignments = ["PATH=" + path] + [
    f"{key}={os.path.abspath(os.path.expanduser(os.environ[key]))}"
    for key in ("CLAUDE_CONFIG_DIR", "XDG_CACHE_HOME", "CLAUDE_DELEGATE_QUOTA_LOG") if os.environ.get(key)]
Path(directory, "claude-delegate-quota.service").write_text(
    "[Unit]\nDescription=Claude delegate quota snapshot\n\n"
    "[Service]\nType=oneshot\nNice=10\n"
    f"ExecStart={quoted(python)} {quoted(str(Path(delegate).absolute()))} quota --snapshot --if-active\n"
    f"Environment={' '.join(quoted(a) for a in assignments)}\n", encoding="utf-8")
Path(directory, "claude-delegate-quota.timer").write_text(
    "[Unit]\nDescription=Poll quota while Claude Code is active\n\n"
    "[Timer]\nOnCalendar=*:0/10\nAccuracySec=1min\nPersistent=false\n\n"
    "[Install]\nWantedBy=timers.target\n", encoding="utf-8")
PY_TIMER
  ok "quota timer units written to $UNIT_DIR"
  if command -v systemctl >/dev/null; then
    systemctl --user daemon-reload
    systemctl --user enable --now claude-delegate-quota.timer
    ok "quota timer enabled"
  else
    warn "systemctl not found; quota timer units written but not enabled"
  fi
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
if [ "$check_only" = 1 ]; then check_timer; exit 0; fi

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
if [ "$hooks" = 1 ]; then
  # merge, never replace: keep every existing key and hook; back the file up first
  [ -f "$SETTINGS" ] && cp "$SETTINGS" "$SETTINGS.bak-$(date +%Y%m%d-%H%M%S)"
  python3 - "$SETTINGS" "$REPO/hooks/context-guard.py" <<'PY_HOOK'
import json, pathlib, sys
path, script = pathlib.Path(sys.argv[1]), sys.argv[2]
s = json.loads(path.read_text()) if path.exists() else {}
cmd = f'python3 "{script}"'
groups = s.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
if not any(h.get("command") == cmd for g in groups for h in g.get("hooks", [])):
    groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(s, indent=2) + "\n")
PY_HOOK
  ok "context-guard hook registered in $SETTINGS"
fi
if [ "$rules" = 1 ]; then
  line="@$REPO/rules/orchestrator.md"
  if grep -qsF "$line" "$USER_MD"; then
    ok "orchestrator rules already imported in $USER_MD"
  else
    [ -f "$USER_MD" ] && cp "$USER_MD" "$USER_MD.bak-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$(dirname "$USER_MD")"
    { [ -s "$USER_MD" ] && printf '\n'; printf '%s\n' "$line"; } >> "$USER_MD"
    ok "orchestrator rules imported in $USER_MD"
  fi
fi
if [ "$timer" = 1 ]; then install_timer; fi
if [ "$failed" = 1 ]; then echo "some skills were not installed (see above)" >&2; exit 1; fi
echo "done. Claude Code picks up new skills without a restart."
