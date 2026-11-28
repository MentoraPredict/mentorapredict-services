"""Checks the version calculated by release-tag.sh on temporary Git repositories."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "release-tag.sh"


def find_bash():
    if os.name != "nt":
        return shutil.which("bash")
    # In PowerShell the first "bash" in PATH is the WSL launcher, so use the one shipped with Git.
    git = shutil.which("git")
    if git:
        for folder in Path(git).resolve().parents[:3]:
            candidate = folder / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
    return None


BASH = find_bash()


@unittest.skipUnless(BASH, "bash is required")
class ReleaseTagTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.git("init", "--quiet")

    def git(self, *args):
        config = ["-c", "user.name=test", "-c", "user.email=test@example.com",
                  "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
                  "-c", "core.hooksPath=none"]
        subprocess.run(["git", *config, *args], cwd=self.temp.name, check=True,
                       capture_output=True)

    def commit(self, message):
        self.git("commit", "--quiet", "--allow-empty", "-m", message)

    def release(self, branch="qa", version=""):
        env = {**os.environ, "BRANCH": branch, "FORCE_VERSION": version, "DRY_RUN": "true"}
        result = subprocess.run([BASH, SCRIPT.as_posix()], cwd=self.temp.name, env=env,
                                capture_output=True, text=True, check=True)
        return result.stdout

    def test_reads_every_commit_and_not_only_the_newest(self):
        self.commit("feat: add login")
        self.commit("docs: explain login")
        self.commit("ci: run tests")
        self.assertIn("New tag: v0.1.0-rc.1.", self.release())

    def test_highest_change_wins_in_older_commits(self):
        self.commit("chore: start")
        self.git("tag", "v1.2.3")
        for older, expected in (("fix: a", "v1.2.4"), ("feat: b", "v1.3.0"),
                                ("refactor: c\n\nBREAKING CHANGE: removes a", "v2.0.0")):
            self.commit(older)
            self.commit("docs: newest commit")
            with self.subTest(expected=expected):
                self.assertIn(f"New tag: {expected}.", self.release("main"))

    def test_non_releasable_commits_create_nothing(self):
        self.commit("docs: one")
        self.commit("chore: two")
        self.assertIn("No releasable commits", self.release())

    def test_same_commit_does_not_get_a_second_candidate(self):
        self.commit("feat: add login")
        self.git("tag", "v0.1.0-rc.1")
        self.assertIn("already published as v0.1.0-rc.1", self.release())

    def test_new_commit_gets_the_next_candidate(self):
        self.commit("feat: add login")
        self.git("tag", "v0.1.0-rc.1")
        self.commit("fix: login")
        self.assertIn("New tag: v0.1.0-rc.2.", self.release())

    def test_stable_release_keeps_the_tested_version(self):
        self.commit("feat: add login")
        self.git("tag", "v0.1.0-rc.1")
        self.assertIn("New tag: v0.1.0.", self.release("main"))


if __name__ == "__main__":
    unittest.main()
