import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.learning_selection import (
    discover_local_skills,
    linked_root_candidates,
    linked_skill_target,
)


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

    def test_linked_skill_is_admitted_only_from_explicit_source_root(self) -> None:
        approved = self.root / "checkout/skills"
        _skill(approved / "linked-helper", name="linked-helper")
        sibling = self.root / "checkout-other/skills"
        _skill(sibling / "sibling-helper", name="sibling-helper")
        aliases = self.root / ".agents/skills"
        aliases.mkdir(parents=True)
        (aliases / "linked-helper").symlink_to(approved / "linked-helper", target_is_directory=True)
        (aliases / "sibling-helper").symlink_to(sibling / "sibling-helper", target_is_directory=True)

        self.assertEqual(discover_local_skills(self.root, scope="project"), {})
        self.assertEqual(discover_local_skills(
            self.root, scope="project", linked_skill_roots=(approved.resolve(),)
        ), {"linked-helper": [".agents/skills/linked-helper"]})
        self.assertEqual(linked_skill_target(aliases / "linked-helper", (approved.resolve(),)),
                         (approved / "linked-helper").resolve())
        self.assertIsNone(linked_skill_target(aliases / "sibling-helper", (approved.resolve(),)))

    def test_linked_target_must_be_strictly_inside_root_and_alias_id_must_match(self) -> None:
        approved = self.root / "sources"
        _skill(approved / "canonical", name="canonical")
        _skill(approved / "other", name="other")
        aliases = self.root / ".codex/skills"
        aliases.mkdir(parents=True)
        (aliases / "renamed").symlink_to(approved / "canonical", target_is_directory=True)
        (aliases / "canonical").symlink_to(approved, target_is_directory=True)

        self.assertIsNone(linked_skill_target(aliases / "renamed", (approved.resolve(),)))
        self.assertIsNone(linked_skill_target(aliases / "canonical", (approved.resolve(),)))

    def test_linked_target_rejects_broken_escaped_and_retargeted_links(self) -> None:
        approved = self.root / "approved"
        outside = self.root / "outside"
        _skill(approved / "helper", name="helper")
        _skill(outside / "helper", name="helper")
        aliases = self.root / ".agents/skills"
        aliases.mkdir(parents=True)
        broken = aliases / "helper"
        broken.symlink_to(approved / "missing", target_is_directory=True)
        self.assertIsNone(linked_skill_target(broken, (approved.resolve(),)))
        broken.unlink()
        broken.symlink_to(outside / "helper", target_is_directory=True)
        self.assertIsNone(linked_skill_target(broken, (approved.resolve(),)))
        broken.unlink()
        broken.symlink_to(approved / "helper", target_is_directory=True)
        self.assertEqual(linked_skill_target(broken, (approved.resolve(),)), (approved / "helper").resolve())
        broken.unlink()
        broken.symlink_to(outside / "helper", target_is_directory=True)
        self.assertIsNone(linked_skill_target(broken, (approved.resolve(),)))

    def test_linked_target_rejects_canonical_managed_ancestry_and_nested_repo(self) -> None:
        managed_root = self.root / "plugins" / "approved"
        _skill(managed_root / "managed", name="managed")
        nested_root = self.root / "approved-nested"
        _skill(nested_root / "repo-skill", name="repo-skill")
        (nested_root / "repo-skill/.git").mkdir()
        aliases = self.root / ".agents/skills"
        aliases.mkdir(parents=True)
        (aliases / "managed").symlink_to(managed_root / "managed", target_is_directory=True)
        (aliases / "repo-skill").symlink_to(nested_root / "repo-skill", target_is_directory=True)

        self.assertIsNone(linked_skill_target(aliases / "managed", (managed_root.resolve(),)))
        self.assertIsNone(linked_skill_target(aliases / "repo-skill", (nested_root.resolve(),)))

    def test_linked_target_rechecks_plugin_and_hermes_metadata(self) -> None:
        approved = self.root / "sources"
        plugin = approved / "plugin-source" / "helper"
        _skill(plugin, name="helper")
        (plugin.parent / "plugin.json").write_text("{}", encoding="utf-8")
        bundled_root = self.root / ".hermes/skills"
        bundled = bundled_root / "research/bundled"
        _skill(bundled, name="bundled")
        (bundled_root / ".bundled_manifest").write_text("bundled:deadbeef\n", encoding="utf-8")
        hub_root = bundled_root
        hub = hub_root / "research/hub-managed"
        _skill(hub, name="hub-managed")
        lock = hub_root / ".hub/lock.json"
        lock.parent.mkdir(parents=True)
        lock.write_text('{"installed":{"pkg":{"install_path":"research/hub-managed"}}}',
                        encoding="utf-8")
        aliases = self.root / ".agents/skills"
        aliases.mkdir(parents=True)
        for name, target in (("helper", plugin), ("bundled", bundled), ("hub-managed", hub)):
            (aliases / name).symlink_to(target, target_is_directory=True)

        self.assertIsNone(linked_skill_target(aliases / "helper", (approved.resolve(),)))
        self.assertIsNone(linked_skill_target(aliases / "bundled", (self.root.resolve(),)))
        self.assertIsNone(linked_skill_target(aliases / "hub-managed", (self.root.resolve(),)))

    def test_hermes_alias_categories_are_supported_without_following_symlinked_parent(self) -> None:
        approved = self.root / "source"
        _skill(approved / "category-helper", name="category-helper")
        aliases = self.root / ".hermes/skills/research"
        aliases.mkdir(parents=True)
        (aliases / "category-helper").symlink_to(approved / "category-helper", target_is_directory=True)
        parent_target = self.root / "outside-category"
        _skill(parent_target / "escaped", name="escaped")
        (self.root / ".hermes/skills/linked-category").symlink_to(parent_target, target_is_directory=True)

        self.assertEqual(discover_local_skills(
            self.root, scope="home", linked_skill_roots=(approved.resolve(),)
        ), {"category-helper": [".hermes/skills/research/category-helper"]})

    def test_linked_allowlist_root_inside_user_checkout_is_allowed(self) -> None:
        checkout = self.root / "checkout"
        (checkout / ".git").mkdir(parents=True)
        approved = checkout / "skill-sources"
        _skill(approved / "checkout-helper", name="checkout-helper")
        aliases = self.root / ".agents/skills"
        aliases.mkdir(parents=True)
        (aliases / "checkout-helper").symlink_to(approved / "checkout-helper", target_is_directory=True)

        self.assertEqual(discover_local_skills(
            self.root, scope="project", linked_skill_roots=(approved.resolve(),)
        ), {"checkout-helper": [".agents/skills/checkout-helper"]})

    def test_linked_target_rejects_replaced_allowlist_root_and_does_not_read_unapproved_skill(self) -> None:
        real_root = self.root / "real-source"
        _skill(real_root / "helper", name="helper")
        approved_link = self.root / "approved-source"
        approved_link.symlink_to(real_root, target_is_directory=True)
        approved = self.root / "approved"
        approved.mkdir()
        aliases = self.root / ".agents/skills"
        aliases.mkdir(parents=True)
        (aliases / "helper").symlink_to(real_root / "helper", target_is_directory=True)

        self.assertIsNone(linked_skill_target(aliases / "helper", (approved_link,)))
        with patch("scripts.learning_selection._skill_id", side_effect=AssertionError("read target")) as read_skill:
            self.assertIsNone(linked_skill_target(aliases / "helper", (approved.resolve(),)))
        read_skill.assert_not_called()

    def test_custom_hermes_profile_metadata_is_checked_on_canonical_ancestors(self) -> None:
        hermes_root = self.root / "custom-hermes/profiles/work/skills"
        _skill(hermes_root / "custom-managed", name="custom-managed")
        lock = hermes_root / ".hub/lock.json"
        lock.parent.mkdir(parents=True)
        lock.write_text(
            '{"installed":{"package":{"install_path":"custom-managed"}}}',
            encoding="utf-8",
        )
        aliases = self.root / ".agents/skills"
        aliases.mkdir(parents=True)
        (aliases / "custom-managed").symlink_to(
            hermes_root / "custom-managed", target_is_directory=True
        )

        self.assertIsNone(linked_skill_target(
            aliases / "custom-managed", (self.root.resolve(),)
        ))

    def test_linked_root_candidates_group_safe_aliases_without_reading_skill_content(self) -> None:
        sources = self.root / "sources"
        _skill(sources / "alpha", name="alpha")
        _skill(sources / "beta", name="beta")
        aliases = self.root / ".agents/skills"
        aliases.mkdir(parents=True)
        (aliases / "alpha").symlink_to(sources / "alpha", target_is_directory=True)
        (aliases / "beta").symlink_to(sources / "beta", target_is_directory=True)

        with patch("scripts.learning_selection._skill_id", side_effect=AssertionError("read target")) as read_skill:
            candidates = linked_root_candidates(self.root, scope="project")
        read_skill.assert_not_called()
        self.assertEqual(candidates, {
            sources.resolve(): (
                (".agents/skills/alpha", (sources / "alpha").resolve()),
                (".agents/skills/beta", (sources / "beta").resolve()),
            ),
        })

    def test_linked_root_candidates_honor_exclusions_pins_and_existing_roots(self) -> None:
        sources = self.root / "sources"
        for name in ("alpha", "beta", "gamma"):
            _skill(sources / name, name=name)
        aliases = self.root / ".agents/skills"
        aliases.mkdir(parents=True)
        for name in ("alpha", "beta", "gamma"):
            (aliases / name).symlink_to(sources / name, target_is_directory=True)

        candidates = linked_root_candidates(
            self.root,
            scope="project",
            exclude=("alpha",),
            pins={"beta": ".agents/skills/beta"},
        )
        self.assertEqual(candidates, {
            sources.resolve(): (
                (".agents/skills/beta", (sources / "beta").resolve()),
                (".agents/skills/gamma", (sources / "gamma").resolve()),
            ),
        })
        self.assertEqual(linked_root_candidates(
            self.root,
            scope="project",
            linked_skill_roots=(sources.resolve(),),
        ), {})

    def test_linked_root_candidates_reject_source_root_checkout(self) -> None:
        checkout_sources = self.root / "checkout-sources"
        _skill(checkout_sources / "nested", name="nested")
        (checkout_sources / ".git").mkdir()
        root_aliases = self.root / ".agents/skills"
        root_aliases.mkdir(parents=True)
        (root_aliases / "nested").symlink_to(checkout_sources / "nested", target_is_directory=True)

        self.assertEqual(linked_root_candidates(self.root, scope="project"), {})

    def test_preview_accepts_external_checkout_but_skips_checkout_nested_inside_it(self) -> None:
        project = self.root / "project"
        aliases = project / ".agents/skills"
        aliases.mkdir(parents=True)
        checkout = (self.root / "external").resolve()
        (checkout / ".git").mkdir(parents=True)
        _skill(checkout / "plain", name="plain")
        nested = checkout / "nested"
        (nested / ".git").mkdir(parents=True)
        _skill(nested / "skills/helper", name="helper")
        (aliases / "plain").symlink_to(checkout / "plain", target_is_directory=True)
        (aliases / "helper").symlink_to(nested / "skills/helper", target_is_directory=True)
        self.assertEqual(linked_root_candidates(project, scope="project"), {
            checkout: ((".agents/skills/plain", checkout / "plain"),),
        })

    def test_explicit_git_root_remains_valid_but_preview_rejects_nested_repo_boundaries(self) -> None:
        explicit_root = self.root / "explicit-repo"
        (explicit_root / ".git").mkdir(parents=True)
        _skill(explicit_root / "explicit-helper", name="explicit-helper")
        aliases = self.root / ".agents/skills"
        aliases.mkdir(parents=True)
        explicit_link = aliases / "explicit-helper"
        explicit_link.symlink_to(explicit_root / "explicit-helper", target_is_directory=True)
        self.assertEqual(
            linked_skill_target(explicit_link, (explicit_root.resolve(),)),
            (explicit_root / "explicit-helper").resolve(),
        )
        self.assertEqual(linked_root_candidates(self.root, scope="project"), {})

        explicit_link.unlink()
        nested_root = self.root / "sources"
        _skill(nested_root / "nested-helper", name="nested-helper")
        (nested_root / "nested-helper/.git").mkdir()
        nested_link = aliases / "nested-helper"
        nested_link.symlink_to(nested_root / "nested-helper", target_is_directory=True)
        self.assertEqual(linked_root_candidates(self.root, scope="project"), {})
        self.assertEqual(linked_root_candidates(
            self.root,
            scope="project",
            linked_skill_roots=(nested_root.resolve(),),
        ), {})
