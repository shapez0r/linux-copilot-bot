import unittest
from unittest import mock

from support import make_config  # noqa: F401
from bot.github_api import GitHub


class GitHubTests(unittest.TestCase):
    def test_delete_repo_accepts_githubs_empty_response(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b""
        with mock.patch("bot.github_api.urllib.request.urlopen", return_value=response) as urlopen:
            GitHub("token").delete_repo("owner", "app")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "DELETE")
        self.assertEqual(request.full_url, "https://api.github.com/repos/owner/app")


if __name__ == "__main__":
    unittest.main()
