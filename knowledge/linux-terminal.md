# Terminal & Linux Mastery — JARVIS Knowledge Pack

How to drive a shell like a professional. JARVIS's home is
Windows (run_shell speaks PowerShell there); this pack is
the Linux/sh craft — for WSL, servers, and every coding
task whose docs assume a Unix shell. PowerShell twins are
noted where they differ most.

## First principles

- The shell composes small tools through PIPES: the output
  of one command is the input of the next. Prefer a pipeline
  of simple, checkable steps over one giant command.
- Everything is a file or a stream: stdout (results),
  stderr (complaints), exit code (0 = success, anything
  else = failure — ALWAYS read it).
- Quote paths with spaces. Prefer relative paths inside a
  project. Check where you are (`pwd`) and what is here
  (`ls -la`) before changing anything.

## Navigation & files

- `pwd`, `ls -la`, `cd dir`, `cd ..`, `cd -` (back)
- `mkdir -p a/b/c` (parents too), `cp -r`, `mv`, `touch`
- Read: `cat` (small), `head -n 20`, `tail -n 50`,
  `tail -f log` (follow), `wc -l`
- Find files: `find . -name "*.py"`, `find . -mtime -1`
  (changed today), `du -sh *` (sizes), `df -h` (disk)
- Windows/PowerShell twins: `Get-ChildItem` (ls),
  `Get-Location` (pwd), `Copy-Item`, `Move-Item`,
  `Get-Content` (cat), `Select-String` (grep).

## Text — the real superpower

- `grep -rn "pattern" .` search everywhere; `-i` ignore
  case, `-E` extended regex, `--include="*.py"` filter
- `grep -v` invert, `grep -c` count, `sort | uniq -c |
  sort -rn` (frequency table — endlessly useful)
- `sed -n '5,10p' file` print lines; `sed 's/old/new/g'`
  replace (dry-run first WITHOUT `-i`: see the change,
  then apply)
- `awk -F, '{print $1, $3}'` columns; `cut -d: -f1`
- `diff a b`, `diff -u` unified; `comm` set comparisons
- Redirection: `>` overwrite, `>>` append, `2>` errors,
  `2>&1` merge, `<` input, `| tee file` (see + save)

## Processes & the system

- `ps aux | grep name`, `pgrep -a name`
- `jobs`, `fg`, `bg`, `Ctrl-C` stop, `Ctrl-Z` suspend
- `kill PID` (polite) — `kill -9` is the last resort
- `top`/`htop` live view; `free -h` memory; `uptime`
- `systemctl status/start/stop UNIT` services (Linux);
  Windows twin: `Get-Service`, `Get-Process`
- Long work: `nohup cmd &` survives logout; prefer the
  app's own background systems when one exists

## Permissions & ownership

- `ls -l` shows rwx for user/group/other; `chmod +x f`
  executable, `chmod 644 f`, `chown user:group f`
- `sudo` = borrowed root. NEVER sudo on a guess; a
  permission error is information, not an obstacle to
  bulldoze.

## Archives, network, packages

- `tar czf out.tgz dir`, `tar xzf`, `zip -r`, `unzip`
- `curl -sSL url`, `curl -I url` (headers only),
  `wget -q`; check reachability `ping -c 2 host`,
  `ss -tlnp` / `netstat` listening ports
- Python lives: `python3 -m pip`, venv first:
  `python3 -m venv .venv && . .venv/bin/activate`
  (Windows: `.venv\Scripts\Activate.ps1`)

## Scripting patterns that recur

- Chain on success: `cmd1 && cmd2`; run anyway: `;`
- Capture: `out=$(cmd)`; loop: `for f in *.log; do ...; done`
- Exit fast on error in scripts: `set -euo pipefail`
- Test before destroying: run the `find`/`ls` version of
  a command BEFORE the `rm` version, with the same pattern

## Safety rails (never cross)

- `rm -rf` demands a read-back of the exact path first;
  never on `/`, `~`, or an unexpanded variable
  (`rm -rf $EMPTY/` is the classic self-own)
- Never pipe the internet into a shell
  (`curl ... | sh`) without reading the script first
- Destructive commands are verified on a SMALL sample
  before the full run — measure twice, delete once
- A command whose output you did not read did not happen:
  report exit code + evidence, not vibes
