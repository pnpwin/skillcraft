import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.learning_selection import discover_local_skills


def _skill(directory: Path, *, name: str | None = None, body: str = "private body") -> None:
    directory.mkdir(parents=True)
    title = f"name: {name}\n" if name else ""
    (directory / "SKILL.md").write_text(f"---\n{title}description: Test fixture.\n---\n{body}\n",
                                        encoding="utf-8")


class LearningSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_project_discovery_preserves_exact_ids_and_rechecks_new_directories(self) -> None:
        _skill(self.root / ".agents/skills/my_skill", name="my_skill")
        _skill(self.root / ".codex/skills/my_skill", name="my_skill")

        first = discover_local_skills(self.root, scope="project")
        self.assertEqual(first, {
            "my_skill": [".agents/skills/my_skill", ".codex/skills/my_skill"]
        })

        _skill(self.root / "opencode/skills/third")
        self.assertEqual(
            discover_local_skills(self.root, scope="project")["third"],
            ["opencode/skills/third"],
        )

    def test_declared_name_must_match_case_sensitive_folder_name(self) -> None:
        _skill(self.root / ".agents/skills/mismatched-folder", name="Mismatch")
        _skill(self.root / ".agents/skills/Match", name="Match")

        self.assertEqual(discover_local_skills(self.root, scope="project"), {
            "Match": [".agents/skills/Match"]
        })

    def test_home_scope_supports_hermes_categories_and_config_opencode(self) -> None:
        _skill(self.root / ".hermes/skills/research/local-helper", name="local-helper")
        _skill(self.root / ".config/opencode/skills/opencode-helper")
        _skill(self.root / ".agents/skills/agent-helper")

        self.assertEqual(discover_local_skills(self.root, scope="home"), {
            "agent-helper": [".agents/skills/agent-helper"],
            "local-helper": [".hermes/skills/research/local-helper"],
            "opencode-helper": [".config/opencode/skills/opencode-helper"],
        })

    def test_excludes_bundled_and_hub_installed_hermes_skills(self) -> None:
        hermes = self.root / ".hermes/skills"
        _skill(hermes / "research/bundled-skill")
        (hermes / ".bundled_manifest").write_text("bundled-skill:deadbeef\n", encoding="utf-8")
        _skill(hermes / "software-development/marketplace-installed")
        lock = hermes / ".hub/lock.json"
        lock.parent.mkdir()
        lock.write_text(
            '{"version":1,"installed":{"market-skill":{"install_path":'
            '"software-development/marketplace-installed"}}}',
            encoding="utf-8",
        )
        _skill(hermes / "research/user-authored", name="user-authored")

        self.assertEqual(discover_local_skills(self.root, scope="project"), {
            "user-authored": [".hermes/skills/research/user-authored"]
        })

    def test_unreadable_hermes_hub_provenance_fails_closed(self) -> None:
        hermes = self.root / ".hermes/skills"
        _skill(hermes / "research/not-identified")
        lock = hermes / ".hub/lock.json"
        lock.parent.mkdir()
        lock.write_text("{broken", encoding="utf-8")

        self.assertEqual(discover_local_skills(self.root, scope="project"), {})

    def test_excludes_symlink_package_git_and_plugin_roots(self) -> None:
        _skill(self.root / ".codex/skills/.system/hidden")
        _skill(self.root / ".agents/skills/package")
        (self.root / ".agents/skills/package/plugin.json").write_text("{}", encoding="utf-8")
        _skill(self.root / ".agents/skills/git-package")
        (self.root / ".agents/skills/git-package/.git").mkdir()
        _skill(self.root / ".agents/skills/plugins/installed")
        _skill(self.root / ".agents/skills/real")
        (self.root / ".agents/skills/real/package.json").write_text("{}", encoding="utf-8")
        (self.root / ".agents/skills/alias").symlink_to(
            self.root / ".agents/skills/real", target_is_directory=True
        )

        self.assertEqual(discover_local_skills(self.root, scope="project"), {
            "real": [".agents/skills/real"]
        })

    def test_excludes_git_checkout_at_discovery_root_in_both_scopes(self) -> None:
        _skill(self.root / ".agents/skills/managed")
        (self.root / ".agents/skills/.git").mkdir()
        _skill(self.root / ".codex/skills/local")
        (self.root / ".git").mkdir()

        for scope in ("project", "home"):
            with self.subTest(scope=scope):
                self.assertEqual(discover_local_skills(self.root, scope=scope), {
                    "local": [".codex/skills/local"],
                })

    def test_excludes_git_worktree_above_discovery_root_in_both_scopes(self) -> None:
        _skill(self.root / ".agents/skills/managed")
        (self.root / ".agents/.git").write_text("gitdir: /checkout/worktrees/skills\n", encoding="utf-8")
        _skill(self.root / ".codex/skills/local")

        for scope in ("project", "home"):
            with self.subTest(scope=scope):
                self.assertEqual(discover_local_skills(self.root, scope=scope), {
                    "local": [".codex/skills/local"],
                })

    def test_missing_roots_and_unknown_scope_are_empty(self) -> None:
        self.assertEqual(discover_local_skills(self.root, scope="project"), {})
        self.assertEqual(discover_local_skills(self.root, scope="home"), {})
        self.assertEqual(discover_local_skills(self.root, scope="unknown"), {})
