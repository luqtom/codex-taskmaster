#!/usr/bin/env python3
"""codex-taskmaster: stop Codex CLI from ending its turn while work is still open.

Runs as a Codex `Stop` hook. Before Codex finishes a turn it reads the project's
task list (and optionally asks an LLM judge). If tasks are still open, it returns
`{"decision": "block", "reason": ...}`, which makes Codex continue with `reason`
as the next prompt.

Single file, standard library only. Subcommands:
    taskmaster.py hook      run as the Stop hook (reads hook JSON on stdin)
    taskmaster.py init      set up .codex/ in the current repository
    taskmaster.py status    show open tasks and loop counters
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

VERSION = "0.1.0"

DEFAULT_CONFIG = {
    # Markdown checklist that is the source of truth for "is the work done".
    "tasks_file": ".codex/tasks.md",
    # Hard cap on automatic continuations per session, so it can never loop forever.
    "max_continuations": 25,
    # Give up after this many continuations in a row with no change to the repo or task list.
    "max_no_progress": 3,
    # Loop counters older than this (seconds) are treated as a new chain.
    "state_ttl": 1800,
    # If Codex's last message contains a line starting with this, it is allowed to stop.
    "blocked_marker": "BLOCKED:",
    # Optional LLM judge: a shell command that receives a prompt on stdin and answers
    # with a first line of CONTINUE or DONE, followed by the reason. Example:
    #   "claude -p --model haiku"
    "judge_command": None,
    # When to ask the judge: "never", "no_tasks" (only if the task list is empty or
    # missing), "all_done" (to double-check when every task is ticked) or "always".
    "judge_when": "no_tasks",
    "judge_timeout": 120,
    # How much of the transcript tail (characters) to show the judge.
    "judge_transcript_chars": 20000,
}

CONFIG_PATH = ".codex/taskmaster.json"

OPEN_RE = re.compile(r"^\s*[-*+]\s+\[ \]\s+(.+?)\s*$")
DONE_RE = re.compile(r"^\s*[-*+]\s+\[[xX]\]\s+(.+?)\s*$")

CONTINUE_TEMPLATE = """\
[codex-taskmaster] You tried to stop, but the work is not finished.

{details}

Keep going: pick the next open item and work on it now. Do not summarise or ask \
for confirmation. Tick each item ([x]) in {tasks_file} as soon as it is really \
done (code written and checked). If you truly cannot continue without the user, \
reply with a line starting with `{marker}` that says exactly what you need."""


# --------------------------------------------------------------------------- utils


def log(msg: str) -> None:
    # stderr is shown in Codex's hook log; stdout is reserved for the JSON decision.
    print(f"[codex-taskmaster] {msg}", file=sys.stderr)


def find_root(start: Path) -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start, capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return start


def load_config(root: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = root / CONFIG_PATH
    if path.is_file():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            log(f"ignoring unreadable {CONFIG_PATH}: {e}")
    return cfg


def parse_tasks(path: Path) -> tuple[list[str], list[str]] | None:
    """Return (open, done) task texts, or None when the file does not exist."""
    if not path.is_file():
        return None
    open_tasks, done_tasks = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if m := OPEN_RE.match(line):
            open_tasks.append(m.group(1))
        elif m := DONE_RE.match(line):
            done_tasks.append(m.group(1))
    return open_tasks, done_tasks


def fingerprint(root: Path, tasks_path: Path) -> str:
    """Hash of everything that counts as progress: task list plus working tree."""
    h = hashlib.sha256()
    if tasks_path.is_file():
        h.update(tasks_path.read_bytes())
    for cmd in (["git", "rev-parse", "HEAD"], ["git", "diff", "HEAD"],
                ["git", "status", "--porcelain"]):
        try:
            out = subprocess.run(cmd, cwd=root, capture_output=True, timeout=30)
            h.update(out.stdout)
        except (OSError, subprocess.SubprocessError):
            pass
    return h.hexdigest()


def state_dir() -> Path:
    base = os.environ.get("CODEX_TASKMASTER_STATE")
    if base:
        return Path(base)
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(xdg) / "codex-taskmaster"


def state_file(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")
    return state_dir() / f"{safe}.json"


def load_state(session_id: str) -> dict:
    try:
        return json.loads(state_file(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"continuations": 0, "no_progress": 0, "fingerprint": None}


def save_state(session_id: str, state: dict) -> None:
    path = state_file(session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        state["updated"] = time.time()
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError as e:
        log(f"could not save state: {e}")


def clear_state(session_id: str) -> None:
    try:
        state_file(session_id).unlink()
    except OSError:
        pass


def transcript_tail(path: str | None, limit: int) -> str:
    if not path:
        return ""
    try:
        data = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return data[-limit:]


def is_blocked(message: str | None, marker: str) -> bool:
    if not message or not marker:
        return False
    return any(line.strip().startswith(marker) for line in message.splitlines())


# --------------------------------------------------------------------------- judge


JUDGE_PROMPT = """\
You supervise an autonomous coding agent (OpenAI Codex). It just tried to end its \
turn. Decide whether the work the user asked for is actually finished.

Answer with exactly one first line: CONTINUE or DONE.
After that, give one short paragraph. For CONTINUE, say concretely what is still \
missing; it will be sent to the agent as its next instruction.

Answer DONE if the agent is waiting for a real decision or information from the user.

## Task list ({tasks_file})
{tasks}

## Agent's last message
{last_message}

## Transcript tail (JSONL, may be truncated)
{transcript}
"""


def ask_judge(cfg: dict, tasks_text: str, last_message: str, transcript: str,
              cwd: Path) -> tuple[bool, str] | None:
    """Return (should_continue, reason), or None if the judge failed."""
    prompt = JUDGE_PROMPT.format(
        tasks_file=cfg["tasks_file"], tasks=tasks_text or "(no task list)",
        last_message=last_message or "(empty)", transcript=transcript or "(unavailable)",
    )
    env = dict(os.environ, CODEX_TASKMASTER_JUDGE="1")
    try:
        out = subprocess.run(
            cfg["judge_command"], shell=True, input=prompt, capture_output=True,
            text=True, timeout=cfg["judge_timeout"], cwd=cwd, env=env,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log(f"judge failed: {e}")
        return None
    text = out.stdout.strip()
    if out.returncode != 0 or not text:
        log(f"judge exited {out.returncode}: {out.stderr.strip()[:500]}")
        return None
    first, _, rest = text.partition("\n")
    verdict = first.strip().strip("*`#: ").upper()
    if verdict.startswith("CONTINUE"):
        return True, rest.strip() or "The supervisor judged the work unfinished."
    if verdict.startswith("DONE"):
        return False, rest.strip()
    log(f"judge gave no verdict: {first[:200]!r}")
    return None


# --------------------------------------------------------------------------- hook


def decide(event: dict) -> dict | None:
    """Core logic. Returns a block decision, or None to let Codex stop."""
    # A judge that itself runs Codex would trigger this hook again; never nest.
    if os.environ.get("CODEX_TASKMASTER_JUDGE") or os.environ.get("CODEX_TASKMASTER_DISABLE"):
        return None

    cwd = Path(event.get("cwd") or os.getcwd())
    root = find_root(cwd)
    cfg = load_config(root)
    session_id = str(event.get("session_id") or "unknown")
    last_message = event.get("last_assistant_message") or ""
    tasks_path = root / cfg["tasks_file"]

    if is_blocked(last_message, cfg["blocked_marker"]):
        log("agent reported BLOCKED; letting it stop")
        clear_state(session_id)
        return None

    # Counters live until we let Codex stop (which clears them). We deliberately do not
    # trust `stop_hook_active` to reset them: Codex does not enforce it (openai/codex#37937),
    # and a reset on every continuation would defeat the loop limits. A chain abandoned
    # midway (e.g. the user pressed Esc) expires after `state_ttl` seconds.
    state = load_state(session_id)
    if time.time() - state.get("updated", 0) > cfg["state_ttl"]:
        state = {"continuations": 0, "no_progress": 0, "fingerprint": None}

    if state["continuations"] >= cfg["max_continuations"]:
        log(f"reached max_continuations={cfg['max_continuations']}; letting it stop")
        clear_state(session_id)
        return None

    fp = fingerprint(root, tasks_path)
    if state["fingerprint"] == fp:
        state["no_progress"] += 1
    else:
        state["no_progress"] = 0
    state["fingerprint"] = fp
    if state["no_progress"] >= cfg["max_no_progress"]:
        log(f"no progress in {state['no_progress']} continuations; letting it stop")
        clear_state(session_id)
        return None

    parsed = parse_tasks(tasks_path)
    open_tasks, done_tasks = parsed if parsed else ([], [])
    tasks_text = tasks_path.read_text(encoding="utf-8") if parsed is not None else ""

    details = None
    if open_tasks:
        listed = "\n".join(f"- [ ] {t}" for t in open_tasks)
        details = (f"{cfg['tasks_file']} still has {len(open_tasks)} open "
                   f"item(s):\n{listed}")

    want_judge = cfg.get("judge_command") and (
        cfg["judge_when"] == "always"
        or (cfg["judge_when"] == "no_tasks" and not open_tasks and not done_tasks)
        or (cfg["judge_when"] == "all_done" and not open_tasks and done_tasks)
    )
    if want_judge:
        verdict = ask_judge(
            cfg, tasks_text, last_message,
            transcript_tail(event.get("transcript_path"), cfg["judge_transcript_chars"]),
            root,
        )
        if verdict is not None:
            go_on, reason = verdict
            if go_on:
                details = (details + "\n\n" if details else "") + f"Supervisor: {reason}"
            elif not open_tasks:
                details = None

    if not details:
        clear_state(session_id)
        return None

    state["continuations"] += 1
    save_state(session_id, state)
    log(f"blocking stop (continuation {state['continuations']}/{cfg['max_continuations']})")
    return {
        "decision": "block",
        "reason": CONTINUE_TEMPLATE.format(
            details=details, tasks_file=cfg["tasks_file"], marker=cfg["blocked_marker"],
        ),
    }


def cmd_hook() -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except ValueError as e:
        log(f"bad hook input: {e}")
        print("{}")
        return 0
    try:
        result = decide(event)
    except Exception as e:  # never break the user's Codex session
        log(f"internal error, letting Codex stop: {e!r}")
        result = None
    print(json.dumps(result or {}))
    return 0


# --------------------------------------------------------------------------- init / status


AGENTS_SNIPPET = """
## Task list (codex-taskmaster)

This repository uses `.codex/tasks.md` as the source of truth for open work.
- When you get a multi-step request, write each step as `- [ ] ...` in `.codex/tasks.md` first.
- Tick an item (`- [x]`) only when it is really done and checked.
- You cannot end your turn while items are open; a hook will send you back to work.
- If you genuinely need the user, write a line starting with `BLOCKED:` and the reason.
"""

TASKS_TEMPLATE = """# Tasks

<!-- codex-taskmaster: Codex may not stop while any "- [ ]" item below is open. -->
"""


def cmd_init(args: list[str]) -> int:
    root = find_root(Path.cwd())
    codex = root / ".codex"
    hooks_dir = codex / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    target = hooks_dir / "taskmaster.py"
    if Path(__file__).resolve() != target.resolve():
        shutil.copy2(__file__, target)
        target.chmod(0o755)
    print(f"installed {target.relative_to(root)}")

    cfg_path = root / CONFIG_PATH
    if not cfg_path.exists():
        cfg_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        print(f"created {CONFIG_PATH}")

    tasks_path = root / DEFAULT_CONFIG["tasks_file"]
    if not tasks_path.exists():
        tasks_path.write_text(TASKS_TEMPLATE, encoding="utf-8")
        print(f"created {DEFAULT_CONFIG['tasks_file']}")

    hooks_path = codex / "hooks.json"
    command = 'python3 "$(git rev-parse --show-toplevel)/.codex/hooks/taskmaster.py" hook'
    handler = {"type": "command", "command": command, "timeout": 300}
    hooks = {}
    if hooks_path.exists():
        try:
            hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        except ValueError:
            print(f"error: {hooks_path} is not valid JSON; fix it and re-run", file=sys.stderr)
            return 1
    stop_groups = hooks.setdefault("hooks", {}).setdefault("Stop", [])
    already = any("taskmaster.py" in h.get("command", "")
                  for g in stop_groups for h in g.get("hooks", []))
    if not already:
        stop_groups.append({"hooks": [handler]})
        hooks_path.write_text(json.dumps(hooks, indent=2) + "\n", encoding="utf-8")
        print("registered Stop hook in .codex/hooks.json")

    agents = root / "AGENTS.md"
    existing = agents.read_text(encoding="utf-8") if agents.exists() else ""
    if "codex-taskmaster" not in existing:
        agents.write_text(existing + AGENTS_SNIPPET, encoding="utf-8")
        print("added task-list rules to AGENTS.md")

    print("\nDone. Start codex in this repo and approve the hook with /hooks.")
    return 0


def cmd_status() -> int:
    root = find_root(Path.cwd())
    cfg = load_config(root)
    parsed = parse_tasks(root / cfg["tasks_file"])
    if parsed is None:
        print(f"no task list at {cfg['tasks_file']} (run: taskmaster.py init)")
    else:
        open_tasks, done_tasks = parsed
        print(f"{cfg['tasks_file']}: {len(open_tasks)} open, {len(done_tasks)} done")
        for t in open_tasks:
            print(f"  [ ] {t}")
    print(f"judge: {cfg.get('judge_command') or 'off'} (when: {cfg['judge_when']})")
    sessions = sorted(state_dir().glob("*.json")) if state_dir().is_dir() else []
    for s in sessions:
        try:
            st = json.loads(s.read_text(encoding="utf-8"))
            print(f"session {s.stem}: {st.get('continuations', 0)} continuation(s)")
        except (OSError, ValueError):
            pass
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "hook"
    if cmd == "hook":
        return cmd_hook()
    if cmd == "init":
        return cmd_init(argv[2:])
    if cmd == "status":
        return cmd_status()
    if cmd in ("-V", "--version"):
        print(VERSION)
        return 0
    print(__doc__)
    return 0 if cmd in ("-h", "--help", "help") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
