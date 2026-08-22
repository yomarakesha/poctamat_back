# Session memory (checked in for machine-to-machine continuity)

These files are a copy of the Claude Code project memory that normally lives outside
the repository, at:

    ~/.claude/projects/C--Users-yomarakesha-Desktop-projects-poctamat/memory/

`MEMORY.md` is the index that is loaded into context at the start of every session;
each other file holds one fact with YAML frontmatter.

## Restoring on another machine

Copy the contents of this directory into the local memory path for the project. The
directory name is derived from the absolute project path with separators replaced by
dashes, so it differs if the project lives elsewhere. On Windows:

    mkdir "%USERPROFILE%\.claude\projects\C--Users-<user>-Desktop-projects-poctamat\memory"
    copy .claude\memory\*.md "%USERPROFILE%\.claude\projects\...\memory\"

Do not copy this README into the memory directory — it is not a memory file.

Session history and work artifacts also live in the repository:

- `.remember/` — dated session logs (now/recent/archive plus `today-*.md`)
- `.superpowers/sdd/` — per-sprint planning and task briefs
