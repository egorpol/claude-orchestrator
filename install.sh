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
#                           that asks before a commit or push that would expose secrets, and
#                           denies a sticking cd/pushd into a project subdirectory
#   ./install.sh --attention  also register hooks/attention.py (Stop, AskUserQuestion,
#                           permission/elicitation notifications) for Cursor/KDE desktop attention
#   ./install.sh --git-hook=DIR
#                           link hooks/secret-guard.py as DIR's pre-push and pre-commit hooks
#                           (respects core.hooksPath / worktrees); refuses a different existing
#                           hook unless --force (then backs it up per hook like a skill)
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
#                           Also writes and enables an hourly timer for default delegate rate
#                           (no backlog on systemd 257+).
#                           Undo: delete the session-mode.py entries from settings.json, then --rules;
#                           remove rate timer: systemctl --user disable --now claude-delegate-rate.timer,
#                           then delete claude-delegate-rate.{service,timer} from
#                           ${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/
#   ./install.sh --timer    install an activity-gated quota timer (every 10 minutes)
#                           remove: systemctl --user disable --now claude-delegate-quota.timer
#                           then delete claude-delegate-quota.{service,timer} from
#                           ${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/
#   ./install.sh --check    only check prerequisites and the private-name deny list
#   ./install.sh --uninstall  remove this product's hooks, rules imports, skill/command links,
#                           delegate PATH link and timer units; copied skills/commands are moved
#                           to skill-backups/ (including user edits), never deleted
#                           add --git-hook=DIR to remove this product's git hook links there
#   ./install.sh --force    replace an existing non-symlinked skill dir or command file (it is
#                           moved to skill-backups/ in the Claude config directory)
#   Links to the current source stay in place. Recorded links can be replaced with a notice.
#   Other skill/command links and existing foreign --bin targets are moved to skill-backups/.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SKILLS_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
COMMANDS_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/commands"
BACKUP_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skill-backups"
SETTINGS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
USER_MD="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/CLAUDE.md"
# Ownership and content hashes stay outside the skill/command text Claude loads.
COPY_MANIFEST="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.claude-orchestrator-copies.json"
INSTALL_STATE="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.claude-orchestrator-install.json"
RULES_LINE="@$REPO/rules/orchestrator.md"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mode=link bin=0 hooks=0 attention=0 experiment=0 rules=0 timer=0 check_only=0 uninstall=0 force=0 failed=0 git_hook_dir=
for arg in "$@"; do
  case "$arg" in
    --copy) mode=copy ;;
    --bin) bin=1 ;;
    --hooks) hooks=1 ;;
    --attention) attention=1 ;;
    --experiment) experiment=1 ;;
    --rules) rules=1 ;;
    --timer) timer=1 ;;
    --check) check_only=1 ;;
    --uninstall) uninstall=1 ;;
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
  if command -v claude >/dev/null; then
    ok "claude $(claude --version 2>/dev/null | head -1)"
  else
    miss "claude: needed by hybrid 'delegate quota' (Claude Code CLI with a claude.ai login)"
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
  local aa_key_file="${XDG_CONFIG_HOME:-$HOME/.config}/claude-orchestrator/aa-api-key"
  if [ -n "${ARTIFICIAL_ANALYSIS_API_KEY:-}" ]; then ok "AA API key in ARTIFICIAL_ANALYSIS_API_KEY (ratings from AA's free API)"
  elif grep -q '[^[:space:]]' "$aa_key_file" 2>/dev/null; then ok "AA API key in $aa_key_file (ratings from AA's free API)"
  else warn "optional: no AA API key in ARTIFICIAL_ANALYSIS_API_KEY or $aa_key_file: without a usable ratings cache, route ranks by quota only"; fi
  if hook_registered context-guard.py UserPromptSubmit; then ok "context-guard hook registered in $SETTINGS"
  else warn "optional: context-guard hook not registered (./install.sh --hooks)"; fi
  if hook_registered secret-guard.py PreToolUse; then ok "secret-guard hook registered in $SETTINGS"
  else warn "optional: secret-guard hook not registered (./install.sh --hooks)"; fi
  if hook_registered attention.py Stop; then ok "attention hook registered in $SETTINGS"
  else warn "optional: attention hook not registered (./install.sh --attention)"; fi
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
  if command -v python3 >/dev/null; then check_private_names; fi
}

check_private_names() {
  python3 - "$REPO/hooks/secret-guard.py" <<'PY_PRIVATE_NAMES'
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

spec = importlib.util.spec_from_file_location("secret_guard", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
guard = module.Guard()
path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "claude-orchestrator/private-names.txt"
try:
    guard.require_private_names()
except module.GitError:
    print(guard.redact(f"  warn  private-name deny list unreadable or invalid UTF-8: {path}"))
else:
    if path.exists():
        print(guard.redact(f"  ok    private-name deny list: {path} ({len(guard._private_entries)} entries)"))
    else:
        print(guard.redact(f"  warn  private-name deny list missing (rule off): {path}"))
if path.exists():
    try:
        probe = subprocess.run(["git", "-C", str(path.resolve().parent), "rev-parse", "--is-inside-work-tree"],
                               capture_output=True, text=True)
        if probe.returncode == 0 and probe.stdout.strip() == "true":
            print(guard.redact(f"  warn  private-name deny list is inside a git work tree: {path}; one git add could publish it"))
    except OSError:
        pass
PY_PRIVATE_NAMES
}

# Shared ownership logic for hooks, imports and links. Keep earlier roots until
# uninstall; only the installer-specific guarded command can prove an unknown root.
IFS= read -r -d '' OWNERSHIP_PY <<'PY_OWNERSHIP' || true
import copy, json, os, pathlib, re, shutil, sys, time

HOOK_NAMES = {"context-guard.py", "secret-guard.py", "session-mode.py", "attention.py"}

def quoted(value):
    # Fish treats backticks literally inside double quotes; escape them outside.
    return '"' + ''.join('"\\`"' if c == '`' else '\\' + c if c in '\\"$' else c
                         for c in str(value)) + '"'

def guarded(script):
    return f'[ ! -f {quoted(script)} ] || python3 {quoted(script)}'

def guarded_script(command):
    if not isinstance(command, str):
        return None
    match = re.fullmatch(r'\[ ! -f (".*") \] \|\| python3 \1', command, flags=re.DOTALL)
    if not match:
        return None
    # Decode only quoted()'s syntax. shlex retains a backslash before $ inside
    # double quotes, so it cannot round-trip all paths which these shells accept.
    encoded, value, i = match[1][1:-1], [], 0
    while i < len(encoded):
        if encoded.startswith('"\\`"', i):
            value.append('`')
            i += 4
        elif encoded[i] == "\\":
            if i + 1 == len(encoded) or encoded[i + 1] not in '\\"$':
                return None
            value.append(encoded[i + 1])
            i += 2
        elif encoded[i] == '"':
            return None
        else:
            value.append(encoded[i])
            i += 1
    script = pathlib.Path("".join(value))
    if (script.is_absolute() and script.parent.name == "hooks" and
            script.name in HOOK_NAMES and command == guarded(script)):
        return script
    return None

def inner_hooks(settings):
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    for groups in hooks.values() if isinstance(hooks, dict) else []:
        for group in groups if isinstance(groups, list) else []:
            inner = group.get("hooks") if isinstance(group, dict) else None
            yield from inner if isinstance(inner, list) else []

def backup_file(path):
    backup = pathlib.Path(f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    suffix = 0
    while backup.exists():
        suffix += 1
        backup = pathlib.Path(f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{suffix}")
    shutil.copy2(path, backup)

class Ownership:
    def __init__(self, state, repo, settings):
        self.path, self.repo, settings = map(pathlib.Path, (state, repo, settings))
        self.original = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        try:
            data = json.loads(self.original) if self.original else {"checkouts": [], "links": {}}
            if (not isinstance(data, dict) or not isinstance(data.get("checkouts"), list) or
                    any(not isinstance(root, str) or not pathlib.Path(root).is_absolute()
                        for root in data["checkouts"]) or not isinstance(data.get("links"), dict) or
                    any(not pathlib.Path(dest).is_absolute() or not isinstance(target, str)
                        for dest, target in data["links"].items())):
                raise ValueError("expected checkout paths and link records")
        except (ValueError, TypeError) as error:
            sys.exit(f"  miss  {self.path} is not valid ownership state ({error}); fix it and re-run")
        self.roots = set(data["checkouts"]) | {str(self.repo)}
        self.links = data["links"]
        try:
            settings_data = json.loads(settings.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings_data = {}
        inferred = set()
        for hook in inner_hooks(settings_data):
            script = guarded_script(hook.get("command")) if isinstance(hook, dict) else None
            if script:
                inferred.add(str(script.parent.parent))
        self.roots.update(inferred)
        # Upgrade installations without state: 0.3.0 and older (unguarded hooks,
        # links into this checkout) and round-one ones (guarded hooks prove a root).
        # Only adopt their standard destinations, never arbitrary user links.
        if not self.original:
            config = settings.parent
            for root in self.roots:
                for relative in ("skills/delegate", "skills/field-report", "commands/next.md"):
                    dest = config / relative
                    if dest.is_symlink() and self.link_target(dest) == pathlib.Path(root) / relative:
                        self.remember_link(dest)
            binary = pathlib.Path.home() / ".local/bin/delegate"
            delegate = config / "skills/delegate"
            if (self.link_key(delegate) in self.links and binary.is_symlink() and
                    self.link_target(binary) == delegate.absolute() / "scripts/delegate"):
                self.remember_link(binary)

    def hook_name(self, command):
        script = guarded_script(command)
        if script:
            return script.name
        if isinstance(command, str):
            for root in self.roots:
                for name in HOOK_NAMES:
                    script = pathlib.Path(root) / "hooks" / name
                    if command in (f'python3 "{script}"', f'python3 {quoted(script)}'):
                        return name
        return None

    def rule_lines(self):
        return {f"@{root}/rules/orchestrator.md" for root in self.roots}

    @staticmethod
    def link_key(dest):
        return str(dest.parent.resolve() / dest.name)

    @staticmethod
    def link_target(dest):
        return pathlib.Path(os.path.abspath(dest.parent / os.readlink(dest)))

    def remember_link(self, dest):
        self.links[self.link_key(dest)] = os.readlink(dest)

    def owns_link(self, dest):
        return dest.is_symlink() and self.links.get(self.link_key(dest)) == os.readlink(dest)

    def save(self):
        text = json.dumps({"checkouts": sorted(self.roots), "links": self.links}, indent=2) + "\n"
        if text != self.original:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(text, encoding="utf-8")
            self.original = text

ownership = Ownership(*sys.argv[1:4])
PY_OWNERSHIP

# Bodies receive their own arguments from sys.argv[4:] after ownership's paths.
installer_python() {
  local body
  IFS= read -r -d '' body || true
  python3 -c "$OWNERSHIP_PY"$'\n'"$body" "$INSTALL_STATE" "$REPO" "$SETTINGS" "$@"
}

hook_registered() {
  [ -f "$SETTINGS" ] && installer_python "$1" "$2" <<'PY_REGISTERED'
name, event = sys.argv[4:]
try:
    settings = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
    found = any(isinstance(h, dict) and ownership.hook_name(h.get("command")) == name
                for h in inner_hooks({"hooks": {event: settings["hooks"][event]}}))
except (OSError, ValueError, KeyError, TypeError):
    found = False
sys.exit(0 if found else 1)
PY_REGISTERED
}

session_mode_registered() { hook_registered session-mode.py SessionStart; }
rules_imported() { manage_rules check; }

check_timer() {
  if command -v systemctl >/dev/null; then
    if systemctl --user is-enabled "claude-delegate-quota.timer" >/dev/null 2>&1; then
      ok "quota timer enabled"
    else
      warn "optional: quota timer not enabled (./install.sh --timer)"
    fi
  elif [ -f "$UNIT_DIR/claude-delegate-quota.timer" ]; then
    ok "quota timer unit exists (systemctl unavailable)"
  else
    warn "optional: quota timer unit not installed (./install.sh --timer)"
  fi
  if session_mode_registered; then
    if command -v systemctl >/dev/null; then
      if systemctl --user is-enabled "claude-delegate-rate.timer" >/dev/null 2>&1; then
        ok "rate timer enabled"
      else
        warn "optional: rate timer not enabled (./install.sh --experiment; only needed while the experiment draws sessions)"
      fi
    elif [ -f "$UNIT_DIR/claude-delegate-rate.timer" ]; then
      ok "rate timer unit exists (systemctl unavailable)"
    else
      warn "optional: rate timer unit not installed (./install.sh --experiment)"
    fi
  elif [ -f "$UNIT_DIR/claude-delegate-rate.timer" ] || [ -f "$UNIT_DIR/claude-delegate-rate.service" ] ||
       { command -v systemctl >/dev/null && systemctl --user is-enabled "claude-delegate-rate.timer" >/dev/null 2>&1; }; then
    warn "rate timer is installed but the session-mode hook is not registered, so no new sessions reach it; remove it with 'systemctl --user disable --now claude-delegate-rate.timer', then delete claude-delegate-rate.{service,timer} from $UNIT_DIR"
  fi
}

# Write and enable the named kinds (quota and/or rate). Leaves any other kind's units alone.
install_timer() {
  local kinds=("$@") want_quota=0 want_rate=0 kind
  for kind in "${kinds[@]}"; do
    case "$kind" in
      quota) want_quota=1 ;;
      rate) want_rate=1 ;;
      *) echo "install_timer: unknown kind $kind" >&2; return 1 ;;
    esac
  done
  local python unit_path agent_bin rate_path
  python="$(python3 -c 'import os, sys; print(os.path.abspath(sys.executable))')"
  unit_path="/usr/local/bin:/usr/bin:/bin"
  rate_path="/usr/local/bin:/usr/bin:/bin"
  if [ "$want_quota" = 1 ]; then
    if agent_bin="$(command -v codex)"; then
      agent_bin="$(cd "$(dirname "$agent_bin")" && pwd -P)"
      case ":$unit_path:" in
        *":$agent_bin:"*) ;;
        *) unit_path="$agent_bin:$unit_path" ;;
      esac
    else
      warn "codex not found; quota timer needs it installed on its PATH"
    fi
    if agent_bin="$(command -v claude)"; then
      agent_bin="$(cd "$(dirname "$agent_bin")" && pwd -P)"
      case ":$unit_path:" in
        *":$agent_bin:"*) ;;
        *) unit_path="$agent_bin:$unit_path" ;;
      esac
    else
      warn "claude not found; quota timer needs it installed on its PATH"
    fi
  fi
  if agent_bin="$(command -v cursor-agent)"; then
    agent_bin="$(cd "$(dirname "$agent_bin")" && pwd -P)"
    if [ "$want_quota" = 1 ]; then
      case ":$unit_path:" in
        *":$agent_bin:"*) ;;
        *) unit_path="$agent_bin:$unit_path" ;;
      esac
    fi
    if [ "$want_rate" = 1 ]; then
      case ":$rate_path:" in
        *":$agent_bin:"*) ;;
        *) rate_path="$agent_bin:$rate_path" ;;
      esac
    fi
  else
    if [ "$want_quota" = 1 ] && [ "$want_rate" = 1 ]; then
      warn "cursor-agent not found; quota and rate timers need it installed on their PATH"
    elif [ "$want_quota" = 1 ]; then
      warn "cursor-agent not found; quota timer needs it installed on its PATH"
    elif [ "$want_rate" = 1 ]; then
      warn "cursor-agent not found; rate timer needs it installed on its PATH"
    fi
  fi
  mkdir -p "$UNIT_DIR"
  # Quote and escape systemd arguments, including paths containing spaces or %.
  python3 - "$UNIT_DIR" "$python" "$SKILLS_DIR/delegate/scripts/delegate" "$unit_path" "$rate_path" \
    "$want_quota" "$want_rate" <<'PY_TIMER'
from pathlib import Path
import os
import sys

directory, python, delegate, path, rate_path, want_quota, want_rate = sys.argv[1:]
def quoted(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
# The timer must watch the same transcripts and write the same log as the session hook.
if want_quota == "1":
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
if want_rate == "1":
    # A oneshot prevents overlap; DeferReactivation (systemd 257+) skips overdue fires after a long catch-up.
    # Discard stdout's per-session item counts so the journal cannot expose by-arm outcomes before the freeze.
    # Keep stderr in the journal so failures remain visible through systemctl --user status.
    rate_assignments = ["PATH=" + rate_path] + [
        f"{key}={os.path.abspath(os.path.expanduser(os.environ[key]))}"
        for key in ("CLAUDE_CONFIG_DIR", "XDG_CACHE_HOME", "CLAUDE_SESSION_LOG") if os.environ.get(key)]
    Path(directory, "claude-delegate-rate.service").write_text(
        "[Unit]\nDescription=Claude delegate session ratings\n\n"
        "[Service]\nType=oneshot\nNice=10\nTimeoutStartSec=infinity\n"
        "StandardOutput=null\nStandardError=journal\n"
        f"ExecStart={quoted(python)} {quoted(str(Path(delegate).absolute()))} rate\n"
        f"Environment={' '.join(quoted(a) for a in rate_assignments)}\n", encoding="utf-8")
    Path(directory, "claude-delegate-rate.timer").write_text(
        "[Unit]\nDescription=Rate finished Claude experiment sessions hourly\n\n"
        "[Timer]\nOnCalendar=hourly\nAccuracySec=1min\nPersistent=false\nDeferReactivation=true\n\n"
        "[Install]\nWantedBy=timers.target\n", encoding="utf-8")
PY_TIMER
  for kind in "${kinds[@]}"; do
    ok "$kind timer units written to $UNIT_DIR"
  done
  if command -v systemctl >/dev/null; then
    if ! systemctl --user daemon-reload; then
      warn "systemctl daemon-reload failed. Retry with 'systemctl --user daemon-reload' when the user bus is available"
    fi
    local unit
    for kind in "${kinds[@]}"; do
      unit="claude-delegate-$kind.timer"
      if systemctl --user enable --now "$unit"; then
        ok "$kind timer enabled"
      else
        warn "could not enable the $kind timer. Enable it manually with 'systemctl --user enable --now $unit'"
      fi
    done
  else
    for kind in "${kinds[@]}"; do
      warn "systemctl not found; $kind timer units written but not enabled"
    done
  fi
}

# Manage copies using a content/type/mode snapshot. Only a recorded, unedited copy
# can be refreshed without --force or a backup. Edited/unowned copies are backed up.
manage_items() {
  installer_python "$COPY_MANIFEST" "$REPO" "$BACKUP_DIR" "$SKILLS_DIR" "$COMMANDS_DIR" "$HOME/.local/bin/delegate" "$@" <<'PY_ITEMS'
import hashlib, json, os, pathlib, shutil, stat, sys, time
manifest, repo, backups, skills, commands, binary = map(pathlib.Path, sys.argv[4:10])
action, *args = sys.argv[10:]
original = manifest.read_text(encoding="utf-8") if manifest.exists() else ""
try:
    copies = json.loads(original) if original else {}
    if not isinstance(copies, dict) or any(
            len(pathlib.PurePosixPath(key).parts) != 2 or
            pathlib.PurePosixPath(key).parts[0] not in ("skills", "commands") or
            pathlib.PurePosixPath(key).parts[1] in (".", "..") or
            not isinstance(value, dict) for key, value in copies.items()):
        raise ValueError("expected skill/command copy records")
except (ValueError, TypeError) as error:
    sys.exit(f"  miss  {manifest} is not a valid copy manifest ({error}); fix it and re-run")


def snapshot(path):
    result = {}
    def visit(current, name):
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            result[name] = ["link", os.readlink(current)]
        elif stat.S_ISREG(mode):
            result[name] = ["file", stat.S_IMODE(mode), hashlib.sha256(current.read_bytes()).hexdigest()]
        elif stat.S_ISDIR(mode):
            result[name] = ["dir", stat.S_IMODE(mode)]
            for child in sorted(current.iterdir()):
                if child.name == "__pycache__" and child.is_dir() and not child.is_symlink():
                    continue  # Python runtime caches are not user edits or shipped content.
                visit(child, f"{name}/{child.name}")
        else:
            raise ValueError(f"unsupported file type: {current}")
    visit(path, ".")
    return result


def backup(target, kind="copy"):
    backups.mkdir(parents=True, exist_ok=True)
    dest = backups / f"{target.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    suffix = 0
    while dest.exists() or dest.is_symlink():
        suffix += 1
        dest = backups / f"{target.name}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{suffix}"
    shutil.move(str(target), str(dest))
    print(f"  warn  {target.name}: previous {kind} moved to {dest}")


def resolved(target):
    try:
        return target.resolve(strict=True)
    except (OSError, RuntimeError):
        return None  # Broken or looping link.


def points_at(target, src):
    if not target.is_symlink():
        return False
    source = resolved(src)
    return source is not None and resolved(target) == source


def replace_link(target):
    if ownership.owns_link(target):
        old = os.readlink(target)
        target.unlink()
        print(f"  ok    {target}: removed recorded link to {old}")
    else:
        backup(target, "link")


if action == "uninstall":
    # Recorded link targets work even when an earlier checkout is gone.
    # Preserve user replacements whose targets differ from the install receipt.
    if ownership.owns_link(binary):
        binary.unlink()
        print(f"  ok    removed {binary}")
    for parent in (skills, commands):
        if not parent.is_dir():
            continue
        for target in parent.iterdir():
            key = f"{parent.name}/{target.name}"
            if target.is_symlink():
                if ownership.owns_link(target):
                    target.unlink()
                    print(f"  ok    removed {target}")
            elif key in copies and target.exists():
                backup(target)  # Preserve even edited copies on uninstall.
    copies.clear()
elif action == "install-bin":
    src = (skills / "delegate/scripts/delegate").absolute()
    binary.parent.mkdir(parents=True, exist_ok=True)
    if not points_at(binary, src):
        if binary.is_symlink():
            replace_link(binary)
        elif binary.exists():
            backup(binary, "target")
        binary.symlink_to(src)
    ownership.remember_link(binary)
    ownership.save()
    sys.exit(0)
else:
    src, parent = map(pathlib.Path, args[:2])
    mode, force = args[2:]
    target = parent / src.name
    key = f"{parent.name}/{src.name}"
    if target.is_symlink():
        if mode == "link" and points_at(target, src):
            ownership.remember_link(target)
            ownership.save()
            print(f"  ok    {src.name} (already linked)")
            sys.exit(0)
        replace_link(target)
    elif target.exists():
        current = None
        if mode == "copy" and key in copies:
            try:
                current = snapshot(target)
            except (OSError, ValueError):
                pass  # Unreadable/special files count as edits; --force still backs them up.
        clean = current is not None and current == copies.get(key)
        if mode == "copy" and clean and current == snapshot(src):
            print(f"  ok    {src.name} (copy unchanged)")
            sys.exit(0)
        if force != "1" and not (mode == "copy" and clean):
            print(f"  miss  {src.name}: {target} exists and is not a symlink; re-run with --force to back it up and replace it")
            sys.exit(1)
        if mode == "copy" and clean:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        else:
            backup(target)
    if mode == "link":
        target.symlink_to(src)
        ownership.remember_link(target)
        copies.pop(key, None)
        print(f"  ok    {src.name} -> {src}")
    else:
        if src.is_dir():
            shutil.copytree(src, target, symlinks=True, ignore=lambda directory, names: [
                name for name in names if name == "__pycache__" and
                (pathlib.Path(directory) / name).is_dir() and
                not (pathlib.Path(directory) / name).is_symlink()])
        else:
            shutil.copy2(src, target)
        copies[key] = snapshot(target)
        ownership.links.pop(ownership.link_key(target), None)
        print(f"  ok    {src.name} copied to {target}")

if copies:
    new = json.dumps(copies, indent=2) + "\n"
    if new != original:
        manifest.write_text(new, encoding="utf-8")
elif manifest.exists():
    manifest.unlink()
if action != "uninstall":
    ownership.save()
PY_ITEMS
}

install_item() {
  if ! manage_items install "$1" "$2" "$mode" "$force"; then failed=1; fi
}

# Register context-guard.py + secret-guard.py (guard), session-mode.py (session), or
# attention.py (attention) in settings.json.
register_hooks() {
  # merge, never replace: keep every other key and hook; back the file up before a change
  installer_python "$SETTINGS" "$REPO/hooks" "$1" <<'PY_HOOK'
import json, os, pathlib, shutil, sys, time
path, hooks, which = pathlib.Path(sys.argv[4]), pathlib.Path(sys.argv[5]), sys.argv[6]
text = path.read_text(encoding="utf-8") if path.exists() else ""
if which == "uninstall" and not text:
    sys.exit(0)
try:
    s = json.loads(text) if text.strip() else {}
except ValueError as error:
    sys.exit(f"  miss  {path} is not valid JSON ({error}); fix it and re-run")
if not isinstance(s, dict) or not isinstance(s.get("hooks", {}), dict):
    sys.exit(f"  miss  {path}: expected an object with a \"hooks\" object; fix it and re-run")

previous = copy.deepcopy(s)
scripts = [hooks / name for name in (
    "context-guard.py", "secret-guard.py", "session-mode.py", "attention.py")]
commands = [guarded(script) for script in scripts]
guard, secret, mode, attention = commands
# session-mode starts its slow parts (quota reading, summary) detached, so short timeouts do
entries = {"guard": [("UserPromptSubmit", None, guard, 5),
                     ("PreToolUse", "Bash", secret, 30)],
           "session": [("SessionStart", None, mode, 10), ("PreToolUse", "Skill|Agent|Bash", mode, 5),
                       ("SessionEnd", None, mode, 10)],
           "attention": [("Stop", None, attention, 5),
                         ("PreToolUse", "AskUserQuestion", attention, 5),
                         ("Notification", "permission_prompt|elicitation_dialog", attention, 5)],
           "uninstall": []}[which]

def is_ours(hook, matching):
    name = ownership.hook_name(hook.get("command")) if isinstance(hook, dict) else None
    return name is not None and name in matching

def without_commands(groups, matching):
    kept = []
    for group in groups:
        inner = group.get("hooks") if isinstance(group, dict) else None
        if isinstance(inner, list) and any(is_ours(h, matching) for h in inner):
            group = {**group, "hooks": [h for h in inner if not is_ours(h, matching)]}
            if not group["hooks"]:
                continue
        kept.append(group)
    return kept

if which == "uninstall":
    matching = HOOK_NAMES
    for event, groups in list(s.get("hooks", {}).items()):
        if isinstance(groups, list):
            kept = without_commands(groups, matching)
            if groups and not kept:
                del s["hooks"][event]
            else:
                s["hooks"][event] = kept
    # No formatting-only change (or backup) when nothing of ours was present.
    if s == previous:
        sys.exit(0)
else:
    s.setdefault("hooks", {})
    # Retarget already enabled hooks too, without enabling another feature.
    # One current command per event/script prevents two live clones firing twice.
    for event, groups in s["hooks"].items():
        if not isinstance(groups, list):
            continue
        seen, kept = set(), []
        for group in groups:
            inner = group.get("hooks") if isinstance(group, dict) else None
            if isinstance(inner, list):
                updated = []
                for hook in inner:
                    name = ownership.hook_name(hook.get("command")) if isinstance(hook, dict) else None
                    if name:
                        if name in seen:
                            continue
                        seen.add(name)
                        hook = {**hook, "command": guarded(hooks / name)}
                    updated.append(hook)
                if inner and not updated:
                    continue
                group = {**group, "hooks": updated}
            kept.append(group)
        groups[:] = kept
for event, matcher, cmd, timeout in entries:
    ours = {**({"matcher": matcher} if matcher else {}),
            "hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}
    groups = s["hooks"].setdefault(event, [])
    if not isinstance(groups, list):
        sys.exit(f"  miss  {path}: hooks.{event} is not a list; fix it and re-run")
    matching = {ownership.hook_name(cmd)}
    count = sum(is_ours(h, matching) for group in groups
                if isinstance(group, dict) and isinstance(group.get("hooks"), list)
                for h in group["hooks"])
    if ours in groups and count == 1:
        continue
    # Replace both legacy and guarded forms, including stale matcher/timeout
    # variants even if the current registration is already present too.
    groups[:] = without_commands(groups, matching) + [ours]
new = json.dumps(s, indent=2) + "\n"
if new != text:
    if path.exists():
        backup_file(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
# Save inferred roots before uninstall removes the commands that prove them.
ownership.save()
PY_HOOK
}

install_git_hook() {
  local dir="$1" hook
  for hook in pre-push pre-commit; do
    install_one_git_hook "$dir" "$hook"
  done
}

install_one_git_hook() {
  local dir="$1" hook="$2" target dest backup
  if [ -z "$dir" ] || [ ! -d "$dir" ]; then
    miss "--git-hook: not a directory: ${dir:-}"
    failed=1
    return
  fi
  if ! dest="$(git -C "$dir" rev-parse --git-path "hooks/$hook" 2>/dev/null)"; then
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
      record_link "$dest"
      ok "$hook already linked in $dir"
      return
    fi
    if [ "$force" = 0 ]; then
      miss "$hook: $dest exists and is not this secret-guard; re-run with --force to back it up"
      failed=1
      return
    fi
    mkdir -p "$BACKUP_DIR"
    backup="$BACKUP_DIR/$hook-$(date +%Y%m%d-%H%M%S)"
    if [ -e "$backup" ] || [ -L "$backup" ]; then backup="$backup-$$"; fi
    mv "$dest" "$backup"
    warn "$hook: previous link moved to $BACKUP_DIR/"
  elif [ -e "$dest" ]; then
    if [ "$force" = 0 ]; then
      miss "$hook: $dest exists and is not this secret-guard; re-run with --force to back it up"
      failed=1
      return
    fi
    mkdir -p "$BACKUP_DIR"
    backup="$BACKUP_DIR/$hook-$(date +%Y%m%d-%H%M%S)"
    if [ -e "$backup" ] || [ -L "$backup" ]; then backup="$backup-$$"; fi
    mv "$dest" "$backup"
    warn "$hook: previous hook moved to $BACKUP_DIR/"
  fi
  ln -s "$target" "$dest"
  record_link "$dest"
  ok "$hook -> $target (in $dir)"
}

manage_rules() {
  installer_python "$1" "$USER_MD" "$RULES_LINE" <<'PY_RULES'
action, filename, line = sys.argv[4:]
path = pathlib.Path(filename)
text = path.read_bytes().decode("utf-8") if path.exists() else ""
lines = text.splitlines(keepends=True)  # Keep CRLF and unrelated lines byte-for-byte.
wanted = ownership.rule_lines()
found = [i for i, current in enumerate(lines) if current.strip() in wanted]
if action == "check":
    sys.exit(0 if found else 1)
if action == "install" and len(found) == 1 and lines[found[0]].strip() == line:
    ownership.save()
    print(f"  ok    orchestrator rules already imported in {path}")
    sys.exit(0)
kept = []
replaced = False
for i, current in enumerate(lines):
    if current.strip() not in wanted:
        kept.append(current)
    elif action == "install" and not replaced:
        kept.append(current.replace(current.strip(), line, 1))
        replaced = True
    elif kept and not kept[-1].strip() and all(not rest.strip() for rest in lines[i + 1:]):
        kept.pop()  # The blank line --rules put before a trailing import.
new = "".join(kept)
if action == "install" and not replaced:
    new += ("\n" if new else "") + line + "\n"
if new != text:
    if path.exists():
        backup_file(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(new.encode("utf-8"))
    verb = "imported in" if action == "install" else "import removed from"
    print(f"  ok    orchestrator rules {verb} {path}")
if action == "install":
    ownership.save()
PY_RULES
}

remove_rules_import() { manage_rules remove; }

record_link() {
  installer_python "$1" <<'PY_LINK'
ownership.remember_link(pathlib.Path(sys.argv[4]))
ownership.save()
PY_LINK
}

uninstall_timers() {
  local kind changed=0
  for kind in quota rate; do
    if [ -e "$UNIT_DIR/claude-delegate-$kind.timer" ] || [ -L "$UNIT_DIR/claude-delegate-$kind.timer" ] ||
       [ -e "$UNIT_DIR/claude-delegate-$kind.service" ] || [ -L "$UNIT_DIR/claude-delegate-$kind.service" ]; then
      if command -v systemctl >/dev/null; then
        if ! systemctl --user disable --now "claude-delegate-$kind.timer" "claude-delegate-$kind.service"; then
          warn "systemctl could not disable/stop the $kind timer/service; removing unit files anyway"
        fi
      else
        warn "systemctl not found; removing $kind unit files without disabling them"
      fi
      rm -f "$UNIT_DIR/claude-delegate-$kind.timer" "$UNIT_DIR/claude-delegate-$kind.service"
      changed=1
      ok "$kind timer units removed"
    fi
  done
  if [ "$changed" = 1 ] && command -v systemctl >/dev/null; then
    if ! systemctl --user daemon-reload; then warn "systemctl daemon-reload failed"; fi
  fi
}

uninstall_git_hooks() {
  local dir="$1" hook dest
  for hook in pre-push pre-commit; do
    if ! dest="$(git -C "$dir" rev-parse --git-path "hooks/$hook" 2>/dev/null)"; then
      miss "--git-hook: not a git repository: $dir"
      failed=1
      return
    fi
    case "$dest" in
      /*) ;;
      *) dest="$dir/$dest" ;;
    esac
    installer_python "$dest" <<'PY_GIT_REMOVE'
dest = pathlib.Path(sys.argv[4])
if dest.is_symlink() and (ownership.owns_link(dest) or ownership.link_target(dest) in {
        pathlib.Path(root) / "hooks/secret-guard.py" for root in ownership.roots}):
    dest.unlink()
    print(f"  ok    removed {dest}")
PY_GIT_REMOVE
  done
}

# Uninstall never runs prerequisite agents, creates install directories or adds
# registrations. An empty second run leaves files and systemctl untouched.
if [ "$uninstall" = 1 ]; then
  register_hooks uninstall
  remove_rules_import
  manage_items uninstall
  uninstall_timers
  if [ -n "$git_hook_dir" ]; then uninstall_git_hooks "$git_hook_dir"
  else echo "git hook links: remove them with ./install.sh --uninstall --git-hook=DIR"; fi
  if [ "$failed" = 0 ]; then
    installer_python <<'PY_FORGET'
# Plain uninstall leaves git hook links for the explicitly requested DIR cleanup.
# Retain their provenance until that last installed link has been removed.
ownership.links = {dest: target for dest, target in ownership.links.items()
                   if ownership.owns_link(pathlib.Path(dest))}
if ownership.links:
    ownership.save()
elif ownership.path.exists():
    ownership.path.unlink()
PY_FORGET
  fi
  exit "$failed"
fi

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
  manage_items install-bin
  ok "delegate on PATH via ~/.local/bin/delegate"
fi
if [ "$hooks" = 1 ]; then
  register_hooks guard
  ok "context-guard and secret-guard hooks registered in $SETTINGS"
fi
if [ "$attention" = 1 ]; then
  register_hooks attention
  ok "attention hook registered in $SETTINGS"
fi
if [ "$experiment" = 1 ]; then
  register_hooks session
  ok "session-mode hook registered in $SETTINGS (control sessions)"
  # The hook injects the rules for the drawn mode; imports would reach controls too.
  remove_rules_import
fi
if [ "$rules" = 1 ]; then
  if session_mode_registered; then
    warn "rules not imported: the session-mode hook injects them per session, and an import would reach control sessions too"
  else
    manage_rules install
  fi
fi
timer_kinds=()
if [ "$timer" = 1 ]; then timer_kinds+=(quota); fi
if [ "$experiment" = 1 ]; then timer_kinds+=(rate); fi
if [ "${#timer_kinds[@]}" -gt 0 ]; then install_timer "${timer_kinds[@]}"; fi
if [ -n "$git_hook_dir" ]; then install_git_hook "$git_hook_dir"; fi
if [ "$failed" = 1 ]; then echo "some skills were not installed (see above)" >&2; exit 1; fi
installer_python <<'PY_RECORD'
ownership.save()
PY_RECORD
echo "done. Claude Code picks up new skills without a restart."
