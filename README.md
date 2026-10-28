# codex-taskmaster

**Stop OpenAI Codex CLI from quitting while work is still open.**

Codex often ends its turn with "Done!" while half of what you asked for is still
undone, and you have to type "continue" again and again. Prompts, `AGENTS.md`
rules and even `/goal` don't fully fix it, because in the end the model decides
for itself that it is finished.

`codex-taskmaster` takes that decision away from the model. It is a Codex
[`Stop` hook](https://learn.chatgpt.com/docs/hooks.md): every time Codex tries to
end a turn, the hook checks a task list in your repository. If anything is still
unchecked, it blocks the stop, and Codex continues with a message listing what
is left.

- **Always on.** No special prompt header or mode to remember.
- **Checklist as source of truth.** `.codex/tasks.md`, plain Markdown, which you
  and Codex can both edit.
- **Optional independent judge.** Another model (for example Claude) reviews the
  transcript when there is no checklist, or double-checks "all done".
- **Safe by default.** It enforces loop limits, detects when there is no
  progress, gives Codex a `BLOCKED:` escape hatch, and lets Codex stop if the
  hook itself fails. Codex does not cap blocking Stop hooks on its own
  ([openai/codex#37937](https://github.com/openai/codex/issues/37937)), so this
  matters.
- **One file, no dependencies.** Python 3.9+ standard library.

## Quick start

```bash
cd your-project            # a git repository
curl -fsSLO https://raw.githubusercontent.com/luqtom/codex-taskmaster/main/taskmaster.py
python3 taskmaster.py init
rm taskmaster.py           # init copied it to .codex/hooks/
```

`init` creates:

| File | Purpose |
| --- | --- |
| `.codex/hooks/taskmaster.py` | the hook itself |
| `.codex/hooks.json` | registers it as a `Stop` hook (existing hooks are kept) |
| `.codex/taskmaster.json` | settings |
| `.codex/tasks.md` | the task list |
| `AGENTS.md` | a short section that tells Codex how to use the task list |

Then start `codex` in the repo and **approve the hook with `/hooks`**. Codex
does not run unreviewed hooks.

### Windows and the Codex desktop app

The Codex desktop app (macOS and Windows) uses the same agent engine as the CLI
and runs the same `Stop` hooks. On native Windows, run `init` with the Python
you want to use (`py taskmaster.py init`). It writes that interpreter's
absolute path into `.codex/hooks.json`, because Windows has no `python3` or
`$(...)` by default. Git must be on `PATH`.

Known quirks in the desktop app:
- After a blocked stop, the app folds the earlier answer into a collapsed
  "Worked for ..." section ([openai/codex#34418](https://github.com/openai/codex/issues/34418)).
  This only affects the display; the work continues.
- Hooks added while a task is already open may not apply until you start a new
  task ([openai/codex#47607](https://github.com/openai/codex/issues/47607)).
  If you need to approve the hook, do it once in the CLI with `codex` then `/hooks`.

## How it works

```
you: "add login, tests and docs"
codex: writes .codex/tasks.md
         - [x] add login
         - [ ] tests
         - [ ] docs
codex: "Login is done!"  → tries to stop
hook:  2 open items → {"decision": "block", "reason": "... - [ ] tests - [ ] docs ..."}
codex: continues with tests...
```

On every stop attempt, the hook goes through these checks in order:

1. If Codex's last message has a line starting with `BLOCKED:`, it may stop,
   because it needs you.
2. If the session already hit `max_continuations`, it may stop.
3. If nothing changed (task list, `git diff`, `git status`) over
   `max_no_progress` continuations in a row, it may stop. This avoids
   burning tokens on a stuck agent.
4. If `.codex/tasks.md` has `- [ ]` items, the stop is **blocked**.
5. If a judge is configured, the judge decides (see below).
6. Otherwise Codex may stop.

If the hook crashes, it always lets Codex stop. It never traps your session.

## Configuration

`.codex/taskmaster.json`:

```json
{
  "tasks_file": ".codex/tasks.md",
  "max_continuations": 25,
  "max_no_progress": 3,
  "state_ttl": 1800,
  "blocked_marker": "BLOCKED:",
  "judge_command": null,
  "judge_when": "no_tasks",
  "judge_timeout": 120,
  "judge_transcript_chars": 20000
}
```

Set `CODEX_TASKMASTER_DISABLE=1` to switch it off for one run.

### LLM judge

A checklist only works if Codex keeps it up to date. For a second opinion, point
`judge_command` at any CLI that reads a prompt on stdin and prints a reply. The
reply must start with a line `CONTINUE` or `DONE`, followed by the reason, which
is passed to Codex.

```json
{ "judge_command": "claude -p --model haiku", "judge_when": "all_done" }
```

`judge_when`:

- `"no_tasks"` (default): ask the judge only if the task list is empty or
  missing.
- `"all_done"`: ask the judge when every item is ticked, to catch Codex ticking
  boxes it didn't finish.
- `"always"`: ask on every stop.
- `"never"`: never ask.

The judge runs with `CODEX_TASKMASTER_JUDGE=1`, and the hook does nothing while
that variable is set. So `codex exec` can also act as a judge without
recursing.

## Status

```bash
python3 .codex/hooks/taskmaster.py status
```

## Compared to alternatives

- **`/goal`** (built into Codex): the model judges its own completion, and users
  report it still stops early
  ([#50771](https://github.com/openai/codex/issues/50771)). `codex-taskmaster`
  works alongside it.
- **[codex-loop](https://github.com/compozy/codex-loop)**: also built on Stop
  hooks. It is opt-in per prompt and runs for a set time, number of rounds or
  goal. Use it for long unattended runs. `codex-taskmaster` is for everyday
  "finish what you started".
- **Ralph loop**: a shell loop that feeds the same prompt over and over. It is
  simple, but it knows nothing about what is left to do.

## Development

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
