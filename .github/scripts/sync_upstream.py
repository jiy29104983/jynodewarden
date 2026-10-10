#!/usr/bin/env python3
"""Stage an upstream snapshot without changing this repository's deployment identity."""
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

CONFIGS = ("wrangler.toml", "wrangler.kv.toml")
# Any change to these settings needs a human to reconcile the deployment first.
PROTECTED = (
    "account_id", "route", "routes", "workers_dev", "env", "vars",
    "d1_databases", "r2_buckets", "kv_namespaces", "durable_objects",
    "services", "queues", "hyperdrive", "vectorize", "migrations",
)


def git(*args, check=True):
    return subprocess.run(["git", *args], check=check, capture_output=True).stdout


def read_at(ref, path):
    # Deployment/manifests must be regular files, not symlinks from either side.
    entry = git("ls-tree", ref, "--", path).decode().split()
    if not entry or entry[0] not in ("100644", "100755"):
        raise ValueError(f"{path}: expected a regular tracked file; review manually")
    return git("show", f"{ref}:{path}").decode()


def preserve_config(old_text, new_text, path):
    old = tomllib.loads(old_text)
    new = tomllib.loads(new_text)
    for key in PROTECTED:
        if old.get(key) != new.get(key):
            raise ValueError(f"{path}: protected setting '{key}' changed; review manually")
    name = old.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{path}: missing Worker name")
    # Only modify the root name, never a Durable Object binding named 'name'.
    header_end = re.search(r"(?m)^\s*\[", new_text)
    split = header_end.start() if header_end else len(new_text)
    header, count = re.subn(
        r"(?m)^name\s*=.*$", lambda _: "name = " + json.dumps(name), new_text[:split]
    )
    if count != 1:
        raise ValueError(f"{path}: expected exactly one root Worker name")
    result = header + new_text[split:]
    if tomllib.loads(result)["name"] != name:
        raise ValueError(f"{path}: could not preserve Worker name")
    return result


def prepare(upstream):
    if git("status", "--porcelain").strip():
        raise ValueError("Start from a clean working tree, including untracked files")
    upstream_sha = git("rev-parse", "--verify", f"{upstream}^{{commit}}").decode().strip()
    replacements = {}
    # Validate all protected configuration before modifying the working tree.
    for path in CONFIGS:
        replacements[path] = preserve_config(
            read_at("HEAD", path), read_at(upstream_sha, path), path
        )
    own_package = json.loads(read_at("HEAD", "package.json"))
    package = json.loads(read_at(upstream_sha, "package.json"))
    lock = json.loads(read_at(upstream_sha, "package-lock.json"))
    package["name"] = own_package["name"]
    lock["name"] = own_package["name"]
    lock["packages"][""]["name"] = own_package["name"]
    for path, data in (("package.json", package), ("package-lock.json", lock)):
        replacements[path] = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    # .github stays repository-owned, including this updater. Upstream workflows
    # are not installed implicitly and need not be writable by GITHUB_TOKEN.
    git("restore", f"--source={upstream_sha}", "--staged", "--worktree", "--", ".", ":(exclude).github")
    for path, text in replacements.items():
        Path(path).write_text(text)
    git("add", "--", *replacements)
    if git("diff", "--cached", "--name-only", "--", ".github").strip():
        raise ValueError("Repository-owned .github files unexpectedly changed")
    return upstream_sha


if __name__ == "__main__":
    try:
        print(prepare(sys.argv[1] if len(sys.argv) == 2 else "upstream/main"))
    except (ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Sync stopped: {error}", file=sys.stderr)
        sys.exit(1)
