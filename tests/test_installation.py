import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile


class InstallationTests(unittest.TestCase):
    def test_clean_archive_install_discovers_view_and_preserves_data_on_reload(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "plugin.zip"
            with ZipFile(archive, "w") as bundle:
                for filename in (
                    *repo.glob("*.py"),
                    repo / "README.md",
                    repo / "metadata.yaml",
                    repo / "requirements.txt",
                    repo / "_conf_schema.json",
                    *repo.glob("views/cognition/*"),
                    *repo.glob(".astrbot-plugin/i18n/*.json"),
                ):
                    bundle.write(filename, Path(repo.name + "-main") / filename.relative_to(repo))

            result = subprocess.run(
                [sys.executable, str(repo / "tests/installation_probe.py"), str(archive)],
                env={
                    **os.environ,
                    "ASTRBOT_ROOT": str(root),
                    "ASTRBOT_DISABLE_METRICS": "1",
                    "PYTHONIOENCODING": "utf-8",
                },
                capture_output=True,
                encoding="utf-8",
                timeout=60,
                cwd=root,
            )
            self.assertEqual(result.returncode, 0, result.stdout[-4500:] + result.stderr[-4500:])
