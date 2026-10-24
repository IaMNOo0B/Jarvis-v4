"""Build identity stamp: which shipped build is actually running.

The page status line and /api/state carry BUILD_STAMP so the owner
can tell a fresh build from a stale folder at a glance. ZIP
distributions carry no .git directory, so a commit hash cannot be
read at runtime — the stamp is a short human build tag, bumped on
every shipped build. Older builds carry no stamp at all, which is
itself the signal: stamp present means this build, stamp absent
means an older folder is running.
"""

BUILD_STAMP = "2026-10-24-ui-stamp"
