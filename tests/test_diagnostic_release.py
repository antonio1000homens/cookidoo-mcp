"""One-time diagnostics release must fail closed and preserve the public URL."""
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

class DiagnosticReleaseContract(unittest.TestCase):
    def test_guarded_one_time_action(self):
        workflow = (ROOT / ".github/workflows/diagnostic-release-once.yml").read_text()
        marker = ".github/cookidoo-mcp-transport-diagnostics-approved-2026-11-05.txt"
        push = workflow.split("  push:", 1)[1].split("permissions:", 1)[0]
        self.assertIn(marker, push)
        self.assertIn(".github/cookidoo-image-upload-release-reviewed-2026-11-05.txt", push)
        self.assertIn(".github/cookidoo-photo-copy-release-reviewed-2026-11-05.txt", push)
        self.assertNotIn("src/recipe_image_copy.py", push)
        self.assertNotIn("src/recipe_images.py", push)
        self.assertNotIn("src/auth.py", push)
        self.assertNotIn("workflow_dispatch:", workflow)
        self.assertIn("environment: production", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("github.event_name == 'push'", workflow)

    def test_tri_phase_order_and_fail_closed(self):
        workflow = (ROOT / ".github/workflows/diagnostic-release-once.yml").read_text()
        actions = ["RELEASE_ACTION: disable-writes", "RELEASE_ACTION: deploy-disabled",
                   "RELEASE_ACTION: enable-writes"]
        positions = [workflow.index(action) for action in actions]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('[[ "$enabled" == \'true\' ]]', workflow)
        self.assertIn('[[ "$key" == "cookidoo-mcp/$GITHUB_SHA/cookidoo-mcp.zip" ]]', workflow)
        self.assertIn("bash scripts/release-recipe-tools.sh", workflow)
        script = (ROOT / "scripts/release-recipe-tools.sh").read_text()
        self.assertIn("new_url", script)
        self.assertIn("old_url", script)

    def test_logging_does_not_include_request_bodies_or_secrets(self):
        auth = (ROOT / "src/auth.py").read_text()
        diagnostic = auth.split("async def traced_send", 1)[1].split("await self.app(scope, receive, traced_send)", 1)[0]
        self.assertIn("Cookidoo MCP transport:", diagnostic)
        self.assertNotIn("logger.warning(authorization", diagnostic)
        self.assertNotIn("request.body", diagnostic)
        self.assertNotIn("cf-access-jwt-assertion", diagnostic)
