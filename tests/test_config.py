import shutil
import tempfile
import unittest
from pathlib import Path

from support import make_config  # noqa: F401  (adds the repository root to sys.path)
from bot import config
from bot.repos import RepoError, parse_repo_ref, same_remote


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_env_file_parsing_and_override(self):
        path = self.tmp / ".env"
        path.write_text('# comment\nTELEGRAM_BOT_TOKEN="abc"\nexport TELEGRAM_ADMIN_IDS=7\n'
                        'TELEGRAM_USER_IDS=8,9\nGITHUB_TOKEN=ghp\n'
                        'COPILOT_MODEL=Claude/Opus 5.5\n')
        cfg = config.load({"GITHUB_TOKEN": "from-env"}, env_file=path)
        self.assertEqual((cfg.telegram_token, cfg.owner_ids, cfg.github_token),
                         ("abc", frozenset({7}), "from-env"))
        self.assertEqual(cfg.user_ids, frozenset({8, 9}))
        self.assertEqual(cfg.authorized_ids, frozenset({7, 8, 9}))
        self.assertTrue(cfg.is_admin(7))
        self.assertFalse(cfg.is_admin(8))
        self.assertEqual(cfg.model, "Claude/Opus 5.5")
        self.assertEqual(cfg.repos_dir, Path("/home/localadmin/repos"))
        self.assertFalse(cfg.show_browser)
        self.assertTrue(cfg.temporary_chat)

    def test_browser_visibility_can_be_enabled(self):
        values = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_OWNER_ID": "1", "GITHUB_TOKEN": "g",
                  "BOT_SHOW_BROWSER": "true"}
        self.assertTrue(config.load(values, env_file=self.tmp / "none").show_browser)

    def test_multiple_telegram_users_are_parsed(self):
        values = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ADMIN_IDS": "1, 2,1", "GITHUB_TOKEN": "g"}
        self.assertEqual(config.load(values, env_file=self.tmp / "none").owner_ids,
                         frozenset({1, 2}))

    def test_legacy_admin_setting_and_singular_aliases_are_supported(self):
        base = {"TELEGRAM_BOT_TOKEN": "t", "GITHUB_TOKEN": "g"}
        legacy = config.load({**base, "TELEGRAM_OWNER_ID": "1"}, env_file=self.tmp / "none")
        aliases = config.load({**base, "TELEGRAM_ADMIN_ID": "2", "TELEGRAM_USER_ID": "3"},
                              env_file=self.tmp / "none")
        self.assertEqual(legacy.admin_ids, frozenset({1}))
        self.assertEqual((aliases.admin_ids, aliases.user_ids), (frozenset({2}), frozenset({3})))

    def test_admin_and_regular_user_ids_must_be_disjoint(self):
        values = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ADMIN_IDS": "1,2",
                  "TELEGRAM_USER_IDS": "2,3", "GITHUB_TOKEN": "g"}
        with self.assertRaisesRegex(config.ConfigError, "both admins and regular users"):
            config.load(values, env_file=self.tmp / "none")

    def test_cloudflare_routing_configuration(self):
        values = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_OWNER_ID": "1", "GITHUB_TOKEN": "g",
                  "CLOUDFLARE_API_TOKEN": "cf", "CLOUDFLARE_ACCOUNT_ID": "a" * 32,
                  "CLOUDFLARE_ZONE_ID": "b" * 32}
        cfg = config.load(values, env_file=self.tmp / "none")
        self.assertTrue(cfg.cloudflare_enabled)
        self.assertEqual((cfg.cloudflare_tunnel, cfg.cloudflare_domain),
                         ("kurwalab-tunnel", "kurwalab.fyi"))
        self.assertIn("cf", cfg.secrets)

    def test_missing_and_invalid_settings(self):
        with self.assertRaisesRegex(config.ConfigError, "TELEGRAM_BOT_TOKEN"):
            config.load({}, env_file=self.tmp / "none")
        base = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_OWNER_ID": "1", "GITHUB_TOKEN": "g"}
        for bad in ({"TELEGRAM_OWNER_ID": "abc"}, {"TELEGRAM_OWNER_ID": "1,"},
                    {"TELEGRAM_OWNER_ID": "0,1"}, {"COPILOT_MODEL": "GPT"},
                    {"BOT_SHOW_BROWSER": "maybe"}, {"BOT_TEMPORARY_CHAT": "maybe"},
                    {"CLOUDFLARE_API_TOKEN": "cf"},
                    ):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.load({**base, **bad}, env_file=self.tmp / "none")

    def test_malformed_env_line(self):
        path = self.tmp / ".env"
        path.write_text("not a setting\n")
        with self.assertRaises(config.ConfigError):
            config.parse_env_file(path)

    def test_redact(self):
        self.assertEqual(config.redact("a tok b tok", ["tok", ""]), "a *** b ***")


class RepoRefTests(unittest.TestCase):
    def test_accepted_forms(self):
        self.assertEqual(parse_repo_ref("app", "me"), ("me", "app"))
        self.assertEqual(parse_repo_ref("org/app.v2", "me"), ("org", "app.v2"))
        self.assertEqual(parse_repo_ref("https://github.com/org/app.git", "me"), ("org", "app"))
        self.assertEqual(parse_repo_ref(" github.com/org/app/ ", "me"), ("org", "app"))

    def test_rejected_forms(self):
        for bad in ("", "a/b/c", "../x", "-x", "x y", "a..b", "bad;name", "org//x", "/"):
            with self.subTest(bad=bad), self.assertRaises(RepoError):
                parse_repo_ref(bad, "me")

    def test_same_remote(self):
        self.assertTrue(same_remote("https://github.com/Org/App.git", "org", "app"))
        self.assertTrue(same_remote("git@github.com:org/app.git", "org", "app"))
        self.assertFalse(same_remote("https://github.com/org/other.git", "org", "app"))


if __name__ == "__main__":
    unittest.main()
