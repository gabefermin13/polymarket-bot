---
description: Verify all bots are safely paused before any V2 / migration work
---

# /safeoff-check — confirm safe-off state

Read-only verification that nothing is trading. Run this before any V2 migration step, deployment, or destructive change.

## Step 1 — Read handoff first

Read `C:\tmp\session_state.md`. If it claims any bot is live, surface that conflict — do not just accept the file.

## Step 2 — Verify pause flags on the VPS

The VPS is `root@68.183.55.155`. Use SSH (paramiko or `ssh`).

Check existence of **all expected pause flags from current runtime / source-map / session state**. Build that list at run time — do not hardcode:
- Read `C:\tmp\session_state.md` for the currently-tracked bot inventory.
- Cross-check against the watchdog source (`/root/watchdog.py`) for every flag it consults.
- Cross-check against any source-map / bot-registry file referenced by `session_state.md`.

For illustration only (verify against current state, do not rely on this list): `/root/cbots_paused`, `/root/d2_paused`, `/root/w_paused`, `/root/arb_paused`.

All expected pause flags must exist for full safe-off. If a flag is expected but missing, that is **NOT SAFE-OFF**.

## Step 3 — Verify no bot processes

On the VPS, look for live processes:
- `pgrep -af "python3 main.py"` (C-bots)
- `pgrep -af "python3 d_main.py"` (D-bots)
- `pgrep -af "python3 w_main.py"` (W-bot)
- `pgrep -af "python3 arb_main.py"` (ARB)

Expected: empty for each.

## Step 4 — Verify watchdog respects flags

Read `/root/watchdog.py` (or have a subagent do it) and confirm, for **every bot expected to be paused per current runtime / source-map / session state**:
- That bot's revival path is gated on its expected pause flag
- No commented-out bot is currently being spawned
- No bot present in the watchdog is missing from the expected-pause inventory without a documented reason

## Step 5 — Report

Output a short table: bot, flag present?, process running?, last positions.jsonl entry timestamp. Plus a single line verdict: **SAFE-OFF** or **NOT SAFE-OFF**.

If NOT SAFE-OFF, stop and ask the user what to do — do not pause anything yourself unless explicitly authorized.

## Forbidden during /safeoff-check
- Do **not** unpause any bot
- Do **not** remove any `*_paused` flag
- Do **not** create `/root/arb_v2_ready`
- Do **not** place orders or run signed-order smoke
- Do **not** restart bots or the dashboard
- Do **not** print secrets
- Do **not** edit `session_state.md` (use `/handoff-end`)
