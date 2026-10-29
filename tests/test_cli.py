import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentflow.cli import demo
from agentflow.report import render


class CLITests(unittest.TestCase):
    def test_module_demo_and_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "agentflow", "demo"], check=True, capture_output=True, text=True
        )
        self.assertEqual(json.loads(result.stdout)["project"], "AgentFlow")
        subprocess.run(
            [sys.executable, "-m", "agentflow", "--help"], check=True, capture_output=True
        )

    def test_self_contained_report_escapes_untrusted_data(self):
        data = demo()
        data["untrusted"] = "</script><script>alert(1)</script>"
        with tempfile.TemporaryDirectory() as tmp:
            path = render(data, Path(tmp) / "report.html")
            html = path.read_text()
            self.assertIn("Recorded synthetic run", html)
            self.assertNotIn("</script><script>alert(1)</script>", html)
            self.assertNotIn('src="https://', html)
            self.assertIn("\\u003c", html)


if __name__ == "__main__":
    unittest.main()
