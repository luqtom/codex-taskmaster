import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "taskmaster.py"


def run(args, cwd, stdin="", env=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=cwd, input=stdin,
        capture_output=True, text=True, env=env, timeout=60,
    )


class TaskmasterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.state = Path(self.tmp.name) / "state"
        self.env = dict(os.environ, CODEX_TASKMASTER_STATE=str(self.state))
        self.env.pop("CODEX_TASKMASTER_JUDGE", None)
        self.env.pop("CODEX_TASKMASTER_DISABLE", None)
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "init"],
                       cwd=self.repo, check=True)
        self.assertEqual(run(["init"], self.repo, env=self.env).returncode, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def write_tasks(self, body):
        (self.repo / ".codex" / "tasks.md").write_text("# Tasks\n\n" + body)

    def configure(self, **kw):
        path = self.repo / ".codex" / "taskmaster.json"
        cfg = json.loads(path.read_text())
        cfg.update(kw)
        path.write_text(json.dumps(cfg))

    def stop(self, message="All done!", session="s1"):
        event = {"session_id": session, "cwd": str(self.repo), "hook_event_name": "Stop",
                 "stop_hook_active": False, "last_assistant_message": message}
        out = run(["hook"], self.repo, json.dumps(event), self.env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def touch(self, name):
        (self.repo / name).write_text(name)

    # ------------------------------------------------------------------ tests

    def test_init_creates_files(self):
        hooks = json.loads((self.repo / ".codex" / "hooks.json").read_text())
        cmd = hooks["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertIn("taskmaster.py", cmd)
        self.assertTrue((self.repo / ".codex" / "hooks" / "taskmaster.py").exists())
        self.assertIn("codex-taskmaster", (self.repo / "AGENTS.md").read_text())
        # Idempotent: a second init does not register the hook twice.
        run(["init"], self.repo, env=self.env)
        hooks = json.loads((self.repo / ".codex" / "hooks.json").read_text())
        self.assertEqual(len(hooks["hooks"]["Stop"]), 1)

    def test_allows_stop_when_no_open_tasks(self):
        self.write_tasks("- [x] write parser\n")
        self.assertEqual(self.stop(), {})

    def test_blocks_stop_with_open_tasks(self):
        self.write_tasks("- [x] write parser\n- [ ] add tests\n  - [ ] edge cases\n")
        out = self.stop()
        self.assertEqual(out["decision"], "block")
        self.assertIn("add tests", out["reason"])
        self.assertIn("edge cases", out["reason"])
        self.assertNotIn("write parser", out["reason"])

    def test_blocked_marker_lets_it_stop(self):
        self.write_tasks("- [ ] deploy\n")
        self.assertEqual(self.stop("I need the API key.\nBLOCKED: missing API key"), {})

    def test_stops_after_no_progress(self):
        self.configure(max_no_progress=2)
        self.write_tasks("- [ ] add tests\n")
        self.assertEqual(self.stop()["decision"], "block")  # baseline
        self.assertEqual(self.stop()["decision"], "block")  # no change 1
        self.assertEqual(self.stop(), {})                   # no change 2 -> give up
        # Counters were cleared, so the next user turn starts fresh.
        self.assertEqual(self.stop()["decision"], "block")

    def test_progress_resets_no_progress_counter(self):
        self.configure(max_no_progress=2)
        self.write_tasks("- [ ] a\n- [ ] b\n")
        for i in range(5):
            self.touch(f"f{i}.txt")
            self.assertEqual(self.stop()["decision"], "block")

    def test_max_continuations(self):
        self.configure(max_continuations=3)
        self.write_tasks("- [ ] a\n")
        for i in range(3):
            self.touch(f"f{i}.txt")
            self.assertEqual(self.stop()["decision"], "block")
        self.touch("last.txt")
        self.assertEqual(self.stop(), {})

    def test_sessions_are_independent(self):
        self.configure(max_continuations=1)
        self.write_tasks("- [ ] a\n")
        self.assertEqual(self.stop(session="one")["decision"], "block")
        self.assertEqual(self.stop(session="two")["decision"], "block")

    def test_judge_continue_without_task_list(self):
        judge = self.repo / "judge.sh"
        judge.write_text("#!/bin/sh\ncat >/dev/null\necho CONTINUE\necho 'tests are failing'\n")
        judge.chmod(0o755)
        self.configure(judge_command=str(judge), judge_when="no_tasks")
        out = self.stop()
        self.assertEqual(out["decision"], "block")
        self.assertIn("tests are failing", out["reason"])

    def test_judge_done(self):
        judge = self.repo / "judge.sh"
        judge.write_text("#!/bin/sh\ncat >/dev/null\necho DONE\n")
        judge.chmod(0o755)
        self.configure(judge_command=str(judge), judge_when="no_tasks")
        self.assertEqual(self.stop(), {})

    def test_broken_judge_does_not_block(self):
        self.configure(judge_command="exit 1", judge_when="no_tasks")
        self.assertEqual(self.stop(), {})

    def test_bad_input_is_harmless(self):
        out = run(["hook"], self.repo, "not json", self.env)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(json.loads(out.stdout), {})

    def test_disable_env(self):
        self.write_tasks("- [ ] a\n")
        self.env["CODEX_TASKMASTER_DISABLE"] = "1"
        self.assertEqual(self.stop(), {})


if __name__ == "__main__":
    unittest.main()
