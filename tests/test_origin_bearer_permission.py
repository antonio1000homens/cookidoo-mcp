"""Protect the one-time origin-bearer IAM repair scope."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class IAMRepairContract(unittest.TestCase):
    def test_singular_permission_scoped_to_origin_bearer(self):
        template = (ROOT / "infrastructure/cookidoo.yaml").read_text()
        stmt = template.split("PolicyName: ReadOriginBearerSingleParameter", 1)[1].split(
            "PolicyName: ReserveOwnCustomRecipeTitles", 1
        )[0]
        self.assertIn("Action: ssm:GetParameter\n", stmt)
        self.assertIn("parameter${OriginBearerParameterName}", stmt)
        self.assertNotIn("EmailParameterName", stmt)
        self.assertNotIn("PasswordParameterName", stmt)

    def test_one_time_workflow_only_writes_on_marker_push(self):
        wf = (ROOT / ".github/workflows/repair-origin-bearer-iam-once.yml").read_text()
        self.assertIn("environment: production", wf)
        self.assertIn("id-token: write", wf)
        marker = ".github/cookidoo-origin-iam-repair-approved-2026-11-05.txt"
        section = wf.split("  push:", 1)[1].split("permissions:", 1)[0]
        self.assertIn(marker, section)
        self.assertNotIn("infrastructure/cookidoo.yaml", section)
        self.assertIn("github.event_name == 'push'", wf)
        self.assertNotIn("workflow_dispatch:", wf)

    def test_changeset_is_restricted_to_one_iam_role(self):
        script = (ROOT / "scripts/repair-origin-bearer-iam.sh").read_text()
        self.assertIn('"CookidooRole"', script)
        self.assertIn('"AWS::IAM::Role"', script)
        self.assertIn('.Action == "Modify"', script)
        self.assertIn('($items | length) == 1', script)
        self.assertIn('CodeKey', script)
        self.assertIn('EnableRecipeCreation', script)
        self.assertIn('old_url', script)
        self.assertIn('new_url', script)
        self.assertNotIn('get-parameter --with-decryption', script)
        self.assertNotIn('put-parameter', script)


if __name__ == "__main__":
    unittest.main()
