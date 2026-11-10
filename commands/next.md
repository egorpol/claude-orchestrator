---
description: Continue from HANDOFF.md (type /clear first; a command can't clear)
argument-hint: "[what to focus on]"
---
Continue from the handoff: read `HANDOFF.md` in the project root (if it isn't there, look one folder down, such as `private/HANDOFF.md`, and in the project memory) and only the memory files it names, sizes first (`wc -c`; read one over ~4 KB by its head, and offer in the closing picker to cut it to the resume pointer and binding facts), say in one line which step you are starting, then do it. If `HANDOFF.md` is over 10 KB, give its size in that line, and when you refresh it at the end of the item, cut superseded sections first: an old next step, and each committed item to one line with its commit and the decisions that still bind. Then check that it is current: N = commits since its mtime (`git log --oneline --since="$(stat -c %y HANDOFF.md)"`), M = changed, staged and untracked files (`git status --short`). If either is above 0, open with "handoff predates N commits / M dirty files" and read those commit subjects and `git diff HEAD --stat` before acting. If there is no handoff, say so and ask what to work on; don't reconstruct the state from old transcripts. If you found it one folder down, say so in that first line, and offer in the item's closing picker to move it to the root and list it in `.git/info/exclude`.

$ARGUMENTS
