# Repository instructions

Apply these instructions whenever modifying this repository.

- Make only the requested changes. Preserve unrelated files and behavior.
- Always review and clean README files and comments in all repository files after making changes. Keep README content logically correct and aligned with the current repository state. Keep comments concise and free of historical references.
- Explain current intent, constraints, and non-obvious behavior in comments. Remove redundant narration, change logs, and descriptions of previous implementations. Preserve required license notices and tool directives.
- Keep the Python automation in `engine/copilot_repo_roundtrip.py` as the single canonical Copilot round-trip engine; the bot in `bot/` runs it as a subprocess and does not reimplement it.
- Preserve archive validation, Git metadata protection, rollback, cancellation, and authentication-state safeguards in the engine.
- Keep the bot restricted to configured Telegram users in private chats, keep tokens out of logs, Telegram messages, remote URLs, and deployed apps' environments, and keep apps' `make run` processes detached from the bot.
- Run `make test` (`python3 -m unittest discover -s tests -p "test_*.py"`). Report skipped tests or unavailable dependencies; do not claim a live Copilot, Telegram, or GitHub round-trip was tested without running one.
- Review the final diff for unrelated changes. Never include a `.git` directory in a returned repository archive.
