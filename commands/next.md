---
description: Continue from HANDOFF.md (type /clear first; a command can't clear)
argument-hint: "[what to focus on]"
---
Continue from the handoff: read `HANDOFF.md` in the project root (if it isn't there, look one folder down, such as `private/HANDOFF.md`, and in the project memory) and the memory files it names, say in one line which step you are starting, then do it. Then check that it is current: N = commits since its mtime (`git log --oneline --since="$(stat -c %y HANDOFF.md)"`), M = changed, staged and untracked files (`git status --short`). If either is above 0, open with "handoff predates N commits / M dirty files" and read those commit subjects and `git diff HEAD --stat` before acting. If there is no handoff, say so and ask what to work on; don't reconstruct the state from old transcripts. If you found it one folder down, say so in that first line, and offer in the item's closing picker to move it to the root and list it in `.git/info/exclude`.

$ARGUMENTS
