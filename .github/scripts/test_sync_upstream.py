"""Regression tests for preserving a live deployment while updating source."""
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("sync_upstream", Path(__file__).with_name("sync_upstream.py"))
SYNC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SYNC)
CONFIG = '''name = "jynodewarden"
main = "src/index.ts"
[assets]
run_worker_first = false
[[d1_databases]]
binding = "DB"
database_name = "existing-db"
[[durable_objects.bindings]]
name = "HUB"
class_name = "Hub"
'''


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = os.getcwd()
        os.chdir(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Sync test")
        self.git("config", "user.email", "sync-test@example.invalid")
        self.write("wrangler.toml", CONFIG)
        self.write("wrangler.kv.toml", CONFIG.replace('name = "jynodewarden"', 'name = "kv-worker"', 1))
        self.write("package.json", '{"name":"jynodewarden","version":"1.0.0"}\n')
        self.write("package-lock.json", '{"name":"old-lock-name","packages":{"":{"name":"old-lock-name"}}}\n')
        self.write("src/old.ts", "old\n")
        self.write("obsolete.txt", "remove me\n")
        self.write(".github/workflows/sync-upstream.yml", "local updater\n")
        self.commit("own")
        self.git("switch", "-q", "--orphan", "upstream")
        self.write("wrangler.toml", CONFIG.replace('name = "jynodewarden"', 'name = "nodewarden"', 1).replace("false", "true"))
        self.write("wrangler.kv.toml", CONFIG.replace('name = "jynodewarden"', 'name = "nodewarden"', 1).replace("false", "true"))
        self.write("package.json", '{"name":"nodewarden","version":"2.0.0"}\n')
        self.write("package-lock.json", '{"name":"nodewarden","packages":{"":{"name":"nodewarden","version":"2.0.0"}}}\n')
        self.write("src/new.ts", "new\n")
        self.write("asset.bin", b"\x00\xff\x01")
        self.write(".github/workflows/upstream.yml", "upstream automation\n")
        self.commit("upstream")
        self.git("switch", "-q", "main")

    def tearDown(self):
        os.chdir(self.previous)
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], check=True, capture_output=True).stdout

    def write(self, path, data):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data if isinstance(data, bytes) else data.encode())

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-qm", message)

    def change_upstream(self, path, transform):
        self.git("switch", "-q", "upstream")
        self.write(path, transform(Path(path).read_text()))
        self.commit("change")
        self.git("switch", "-q", "main")

    def test_unrelated_history_source_update_preserves_deployment(self):
        SYNC.prepare("upstream")
        self.assertFalse(Path("obsolete.txt").exists())
        self.assertFalse(Path("src/old.ts").exists())
        self.assertEqual(Path("src/new.ts").read_text(), "new\n")
        self.assertEqual(Path("asset.bin").read_bytes(), b"\x00\xff\x01")
        self.assertTrue(Path(".github/workflows/sync-upstream.yml").exists())
        self.assertFalse(Path(".github/workflows/upstream.yml").exists())
        self.assertIn('name = "jynodewarden"', Path("wrangler.toml").read_text())
        self.assertIn('name = "kv-worker"', Path("wrangler.kv.toml").read_text())
        self.assertIn('name = "HUB"', Path("wrangler.toml").read_text())
        self.assertIn("run_worker_first = true", Path("wrangler.toml").read_text())
        package = json.loads(Path("package.json").read_text())
        self.assertEqual((package["name"], package["version"]), ("jynodewarden", "2.0.0"))
        lock = json.loads(Path("package-lock.json").read_text())
        self.assertEqual(lock["packages"][""]["name"], "jynodewarden")
        self.assertEqual(self.git("diff", "--cached", "--name-only", "--", ".github"), b"")

    def test_second_sync_has_no_changes(self):
        SYNC.prepare("upstream")
        self.commit("sync")
        SYNC.prepare("upstream")
        self.assertEqual(self.git("status", "--porcelain"), b"")

    def test_binding_change_stops_before_any_write(self):
        self.change_upstream("wrangler.kv.toml", lambda s: s.replace("existing-db", "different-db"))
        with self.assertRaisesRegex(ValueError, "protected setting 'd1_databases'"):
            SYNC.prepare("upstream")
        self.assertEqual(self.git("status", "--porcelain"), b"")
        self.assertTrue(Path("obsolete.txt").exists())

    def test_database_id_is_never_silently_dropped(self):
        self.write("wrangler.toml", CONFIG.replace('database_name = "existing-db"', 'database_name = "existing-db"\ndatabase_id = "existing-id"'))
        self.commit("configured database")
        with self.assertRaisesRegex(ValueError, "d1_databases"):
            SYNC.prepare("upstream")
        self.assertEqual(self.git("status", "--porcelain"), b"")

    def test_dirty_worktree_is_rejected(self):
        self.write("src/old.ts", "unsaved work\n")
        with self.assertRaisesRegex(ValueError, "clean working tree"):
            SYNC.prepare("upstream")
        self.assertEqual(Path("src/old.ts").read_text(), "unsaved work\n")

    def test_symlink_config_is_rejected(self):
        self.git("switch", "-q", "upstream")
        Path("wrangler.toml").unlink()
        Path("wrangler.toml").symlink_to("/tmp/not-a-config")
        self.commit("symlink")
        self.git("switch", "-q", "main")
        with self.assertRaisesRegex(ValueError, "regular tracked file"):
            SYNC.prepare("upstream")
        self.assertEqual(self.git("status", "--porcelain"), b"")

    def test_migration_change_requires_review(self):
        self.change_upstream("wrangler.toml", lambda s: s + '\n[[migrations]]\ntag = "v2"\nnew_sqlite_classes = ["Other"]\n')
        with self.assertRaisesRegex(ValueError, "migrations"):
            SYNC.prepare("upstream")
        self.assertEqual(self.git("status", "--porcelain"), b"")


if __name__ == "__main__":
    unittest.main()
