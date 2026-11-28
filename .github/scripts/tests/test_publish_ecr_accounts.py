"""Account selection and fallback for ECR publication."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

path = Path(__file__).resolve().parents[1] / "publish-ecr-accounts.py"
spec = importlib.util.spec_from_file_location("accounts", path)
accounts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(accounts)


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.args = SimpleNamespace(account="AUTO", archive=Path("image"), identity=Path("scan"),
                                    service="identity-service", commit="a" * 40,
                                    evidence=Path(self.temp.name) / "evidence.json")
        self.env = {f"{label}_{name}": f"{label}-{name}" for label in "ABC" for name in
                    ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                     "AWS_REGION", "AWS_ACCOUNT_ID")}

    def test_automatic_falls_back_in_order_and_stops_after_success(self):
        called = []
        def publish(*args):
            label = os.environ["AWS_ACCESS_KEY_ID"][0]
            called.append(label)
            if label == "A":
                raise accounts.publisher.PublishError("expired credentials")
            self.args.evidence.write_text('{}')
        with patch.dict(os.environ, self.env, clear=True), patch.object(accounts.publisher, "publish", side_effect=publish):
            self.assertEqual(accounts.publish_accounts(self.args), "B")
        self.assertEqual(called, ["A", "B"])
        self.assertEqual(json.loads(self.args.evidence.read_text())["account_label"], "B")

    def test_manual_tries_only_requested_account(self):
        self.args.account = "C"
        def publish(*args):
            self.assertEqual(os.environ["AWS_ACCESS_KEY_ID"], "C-AWS_ACCESS_KEY_ID")
            self.args.evidence.write_text('{}')
        with patch.dict(os.environ, self.env, clear=True), patch.object(accounts.publisher, "publish", side_effect=publish) as call:
            accounts.publish_accounts(self.args)
            self.assertEqual(call.call_count, 1)

    def test_all_failed_reports_failure(self):
        with patch.dict(os.environ, self.env, clear=True), patch.object(
                accounts.publisher, "publish", side_effect=accounts.publisher.PublishError("unavailable")) as call:
            with self.assertRaisesRegex(accounts.publisher.PublishError, "all selected accounts"):
                accounts.publish_accounts(self.args)
            self.assertEqual(call.call_count, 3)
        self.assertFalse(self.args.evidence.exists())


if __name__ == "__main__":
    unittest.main()
