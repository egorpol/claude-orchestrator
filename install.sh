#!/usr/bin/env bash
# Install this repo's Claude Code skills into ~/.claude/skills and the /next command
# into ~/.claude/commands, and check the delegate CLIs. Safe to re-run.
#
#   ./install.sh            symlink skills and commands (edits in the repo apply immediately)
#   ./install.sh --copy     copy instead (for machines where the repo may move)
#   ./install.sh --bin      also put `delegate` on PATH via ~/.local/bin
#   ./install.sh --hooks    also register hooks/context-guard.py as a UserPromptSubmit hook in
#                           settings.json (handoff nudge at item boundaries past ~120k context)
#                           and hooks/secret-guard.py as a PreToolUse (Bash) hook (timeout 30)
#                           that asks before a push that would send secrets
#   ./install.sh --git-hook=DIR
#                           link hooks/secret-guard.py as DIR's git pre-push hook
#                           (respects core.hooksPath / worktrees); refuses a different existing
#                           pre-push unless --force (then backs it up like a skill)
#   ./install.sh --rules    also import rules/orchestrator.md from the user-level CLAUDE.md,
#                           so every session gets the orchestrator rules (stays live from the repo);
#                           skipped while the session-mode hook is registered
#   ./install.sh --experiment  opt in to control sessions: register hooks/session-mode.py
#                           (SessionStart, PreToolUse, SessionEnd). Each new session draws its mode
#                           (50% orchestrator, 25% claude-only, 25% vanilla; controls can't
#                           delegate), gets that mode's rules from the hook and logs one summary
#                           row to ~/.cache/claude-delegate/sessions.jsonl. The hook replaces the
#                           --rules import, so this removes it from CLAUDE.md. Weights and repos
#                           excluded from the draw: ~/.config/claude-orchestrator/session-mode.json.
#                           Undo: delete the session-mode.py entries from settings.json, then --rules
#   ./install.sh --timer    install an activity-gated quota timer (every 10 minutes)
#                           remove: systemctl --user disable --now claude-delegate-quota.timer
#                           then delete claude-delegate-quota.{service,timer} from
#                           ${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/
#   ./install.sh --check    only check prerequisites
#   ./install.sh --force    replace an existing non-symlinked skill dir or command file (it is
#                           moved to ~/.claude/skill-backups/, never deleted)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SKILLS_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
COMMANDS_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/commands"
BACKUP_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skill-backups"
SETTINGS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
USER_MD="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/CLAUDE.md"
RULES_LINE="@$REPO/rules/orchestrator.md"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mode=link bin=0 hooks=0 experiment=0 rules=0 timer=0 check_only=0 force=0 failed=0 git_hook_dir=
for arg in "$@"; do
  case "$arg" in
    --copy) mode=copy ;;
    --bin) bin=1 ;;
    --hooks) hooks=1 ;;
    --experiment) experiment=1 ;;
    --rules) rules=1 ;;
    --timer) timer=1 ;;
    --check) check_only=1 ;;
    --force) force=1 ;;
    --git-hook=*) git_hook_dir="${arg#--git-hook=}" ;;
    -h|--help) awk 'NR == 1 { next } /^#/ { print; next } { exit }' "$0"; exit 0 ;;
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
  if grep -qs 'hooks/secret-guard.py' "$SETTINGS"; then ok "secret-guard hook registered in $SETTINGS"
  else warn "optional: secret-guard hook not registered (./install.sh --hooks)"; fi
  local imported=0
  rules_imported && imported=1
  if session_mode_registered; then
    ok "session-mode hook registered in $SETTINGS (control sessions; rules injected per session)"
    [ "$imported" = 1 ] && warn "orchestrator rules are also imported in $USER_MD, so control sessions see them: re-run ./install.sh --experiment to drop the import"
  else
    warn "optional: control sessions off; session-mode hook not registered (./install.sh --experiment)"
    if [ "$imported" = 1 ]; then ok "orchestrator rules imported in $USER_MD"
    else warn "optional: orchestrator rules not imported (./install.sh --rules)"; fi
  fi
  if [ -e "$COMMANDS_DIR/next.md" ]; then ok "/next command in $COMMANDS_DIR"
  else warn "/next command not installed (./install.sh)"; fi
}

# True if settings.json registers session-mode.py for SessionStart (parsed, not a text match).
session_mode_registered() {
  [ -f "$SETTINGS" ] && python3 - "$SETTINGS" <<'PY_REGISTERED'
import json, sys
try:
    groups = json.load(open(sys.argv[1], encoding="utf-8"))["hooks"]["SessionStart"]
    found = any("hooks/session-mode.py" in h.get("command", "") for g in groups for h in g.get("hooks") or [])
except Exception:
    found = False
sys.exit(0 if found else 1)
PY_REGISTERED
}

# True if a line of CLAUDE.md is exactly the import (surrounding whitespace aside), as --hooks removes it.
rules_imported() {
  [ -f "$USER_MD" ] && awk -v want="$RULES_LINE" '{ gsub(/^[ \t\r]+|[ \t\r]+$/, "") } $0 == want { found = 1 } END { exit !found }' "$USER_MD"
}

# Copy FILE to FILE.bak-<time>, never over an earlier backup.
backup_file() {
  local backup
  backup="$1.bak-$(date +%Y%m%d-%H%M%S)"
  [ -e "$backup" ] && backup="$backup-$$"
  cp "$1" "$backup"
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

# Link or copy a skill dir or command file into a config dir.
install_item() {
  local src="$1" name target
  name="$(basename "$src")"
  target="$2/$name"
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
  [ -f "$dir/SKILL.md" ] && install_item "${dir%/}" "$SKILLS_DIR"
done
echo "commands ($mode into $COMMANDS_DIR):"
mkdir -p "$COMMANDS_DIR"
for file in "$REPO"/commands/*.md; do
  [ -f "$file" ] && install_item "$file" "$COMMANDS_DIR"
done

if [ "$bin" = 1 ]; then
  mkdir -p "$HOME/.local/bin"
  ln -sfn "$SKILLS_DIR/delegate/scripts/delegate" "$HOME/.local/bin/delegate"
  ok "delegate on PATH via ~/.local/bin/delegate"
fi
# Register hooks/context-guard.py + secret-guard.py (guard) or hooks/session-mode.py (session)
# in settings.json.
register_hooks() {
  # merge, never replace: keep every other key and hook; back the file up before a change
  python3 - "$SETTINGS" "$REPO/hooks" "$1" <<'PY_HOOK'
import json, os, pathlib, shutil, sys, time
path, hooks, which = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
text = path.read_text(encoding="utf-8") if path.exists() else ""
try:
    s = json.loads(text) if text.strip() else {}
except ValueError as error:
    sys.exit(f"  miss  {path} is not valid JSON ({error}); fix it and re-run")
if not isinstance(s, dict) or not isinstance(s.setdefault("hooks", {}), dict):
    sys.exit(f"  miss  {path}: expected an object with a \"hooks\" object; fix it and re-run")
guard, secret, mode = (f'python3 "{hooks / name}"'
                       for name in ("context-guard.py", "secret-guard.py", "session-mode.py"))
# session-mode starts its slow parts (quota reading, summary) detached, so short timeouts do
entries = {"guard": [("UserPromptSubmit", None, guard, 5),
                     ("PreToolUse", "Bash", secret, 30)],
           "session": [("SessionStart", None, mode, 10), ("PreToolUse", "Skill|Agent|Bash", mode, 5),
                       ("SessionEnd", None, mode, 10)]}[which]
for event, matcher, cmd, timeout in entries:
    ours = {**({"matcher": matcher} if matcher else {}),
            "hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}
    groups = s["hooks"].setdefault(event, [])
    if not isinstance(groups, list):
        sys.exit(f"  miss  {path}: hooks.{event} is not a list; fix it and re-run")
    if ours in groups:
        continue
    # An older registration of this command (other matcher or timeout) is replaced, not kept beside ours.
    kept = []
    for group in groups:
        inner = group.get("hooks") if isinstance(group, dict) else None
        if isinstance(inner, list) and any(isinstance(h, dict) and h.get("command") == cmd for h in inner):
            group["hooks"] = [h for h in inner if not (isinstance(h, dict) and h.get("command") == cmd)]
            if not group["hooks"]:
                continue
        kept.append(group)
    groups[:] = kept + [ours]
new = json.dumps(s, indent=2) + "\n"
if new != text:
    if path.exists():
        backup = pathlib.Path(f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup if not backup.exists() else f"{backup}-{os.getpid()}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
PY_HOOK
}

install_git_hook() {
  local dir="$1" target dest
  if [ -z "$dir" ] || [ ! -d "$dir" ]; then
    miss "--git-hook: not a directory: ${dir:-}"
    failed=1
    return
  fi
  if ! dest="$(git -C "$dir" rev-parse --git-path hooks/pre-push 2>/dev/null)"; then
    miss "--git-hook: not a git repository: $dir"
    failed=1
    return
  fi
  # rev-parse --git-path can be relative to DIR
  case "$dest" in
    /*) ;;
    *) dest="$dir/$dest" ;;
  esac
  mkdir -p "$(dirname "$dest")"
  target="$REPO/hooks/secret-guard.py"
  if [ -L "$dest" ]; then
    if [ "$(readlink -f "$dest")" = "$(readlink -f "$target")" ]; then
      ok "pre-push already linked in $dir"
      return
    fi
    if [ "$force" = 0 ]; then
      miss "pre-push: $dest exists and is not this secret-guard; re-run with --force to back it up"
      failed=1
      return
    fi
    mkdir -p "$BACKUP_DIR"
    mv "$dest" "$BACKUP_DIR/pre-push-$(date +%Y%m%d-%H%M%S)"
    warn "pre-push: previous link moved to $BACKUP_DIR/"
  elif [ -e "$dest" ]; then
    if [ "$force" = 0 ]; then
      miss "pre-push: $dest exists and is not this secret-guard; re-run with --force to back it up"
      failed=1
      return
    fi
    mkdir -p "$BACKUP_DIR"
    mv "$dest" "$BACKUP_DIR/pre-push-$(date +%Y%m%d-%H%M%S)"
    warn "pre-push: previous hook moved to $BACKUP_DIR/"
  fi
  ln -s "$target" "$dest"
  ok "pre-push -> $target (in $dir)"
}

if [ "$hooks" = 1 ]; then
  register_hooks guard
  ok "context-guard and secret-guard hooks registered in $SETTINGS"
fi
if [ "$experiment" = 1 ]; then
  register_hooks session
  ok "session-mode hook registered in $SETTINGS (control sessions)"
  # The hook injects the rules for the drawn mode; an import would reach control sessions too.
  if rules_imported; then
    backup_file "$USER_MD"
    python3 - "$USER_MD" "$RULES_LINE" <<'PY_RULES'
import pathlib, sys
path, line = pathlib.Path(sys.argv[1]), sys.argv[2]
lines = path.read_bytes().decode("utf-8").splitlines(keepends=True)  # keeps CRLF and other lines as they are
kept = []
for i, current in enumerate(lines):
    if current.strip() != line:
        kept.append(current)
    elif kept and not kept[-1].strip() and all(not rest.strip() for rest in lines[i + 1:]):
        kept.pop()  # the blank line --rules put before an import at the end
path.write_bytes("".join(kept).encode("utf-8"))
PY_RULES
    ok "orchestrator rules import removed from $USER_MD (the session-mode hook injects them)"
  fi
fi
if [ "$rules" = 1 ]; then
  if session_mode_registered; then
    warn "rules not imported: the session-mode hook injects them per session, and an import would reach control sessions too"
  elif rules_imported; then
    ok "orchestrator rules already imported in $USER_MD"
  else
    [ -f "$USER_MD" ] && backup_file "$USER_MD"
    mkdir -p "$(dirname "$USER_MD")"
    { [ -s "$USER_MD" ] && printf '\n'; printf '%s\n' "$RULES_LINE"; } >> "$USER_MD"
    ok "orchestrator rules imported in $USER_MD"
  fi
fi
if [ "$timer" = 1 ]; then install_timer; fi
if [ -n "$git_hook_dir" ]; then install_git_hook "$git_hook_dir"; fi
if [ "$failed" = 1 ]; then echo "some skills were not installed (see above)" >&2; exit 1; fi
echo "done. Claude Code picks up new skills without a restart."
