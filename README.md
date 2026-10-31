# Copilot Telegram Bot

A Telegram bot for an Ubuntu VM that turns chat messages from its owner into repository changes. For each request it:

1. selects the repository (an existing GitHub repository, or a new private one) and clones it to `/home/localadmin/repos/<name>`;
2. takes the message as the prompt;
3. sends the repository to Microsoft 365 Copilot Chat through the Playwright round-trip engine and applies the returned repository;
4. commits and pushes the result to GitHub;
5. redeploys the app by running `make run` from `/home/localadmin/repos/<name>`.

**Experimental:** the engine drives Copilot's browser UI, so UI changes can interrupt it. Changes are pushed without review; review the commits on GitHub afterwards.

## Requirements

- Ubuntu with Python 3.9+, `python3-venv`, Git, and `make`.
- A Microsoft 365 account with access to Copilot Chat.
- A Telegram bot token from [@BotFather](https://t.me/BotFather) and your numeric Telegram user id (for example from @userinfobot).
- A GitHub token with read/write access to repository contents. Creating repositories also requires permission to create repositories for the owner.

## Setup

```bash
make setup          # virtual environment, Playwright, Chromium
make system-deps    # Chromium's operating-system libraries (sudo)
sudo mkdir -p /home/localadmin/repos && sudo chown "$USER" /home/localadmin/repos
cp .env.example .env && chmod 600 .env   # then fill in the required values
make login          # sign in to Copilot once (see below)
make run            # start the bot in the foreground
```

### Signing in to Copilot

`make login` opens a visible Chromium window; sign in, open a usable Copilot Chat page, and press Enter in the terminal. The profile (default `~/.copilot-repo-browser`) holds authentication state: keep it private. Bot runs use the same profile with a hidden browser.

A VM usually has no display. Either run `make login` over `ssh -X` (or a VNC session), or sign in on a Linux desktop and copy the profile directory to the VM. A copied profile may not be accepted by Copilot; if so, sign in on the VM. Profiles from Windows or macOS do not carry over. Run `make login` while the bot is idle or stopped, and repeat it when a run fails with an authentication error.

### Running as a service

Edit `deploy/copilot-telegram-bot.service` (user and paths), install it under `/etc/systemd/system/`, and run `sudo systemctl enable --now copilot-telegram-bot`. The unit uses `KillMode=process`, so apps started by `make run` keep running when the bot restarts.

## Using the bot

Only the owner (`TELEGRAM_OWNER_ID`) in a private chat is served; every other message is ignored.

| Message | Effect |
| --- | --- |
| `/repo <name>` or `/repo <owner/name>` | Select the active repository. A missing repository is created as a private GitHub repository with an initial commit, then cloned; an existing one is cloned or fast-forwarded. A GitHub URL is also accepted. |
| `/repo` | Show the active repository. |
| plain text | Run the text as a request against the active repository. |
| `/fix <repo> <request>` | Select a repository and run a request in one message. |
| `/model <provider/model>` | Choose the Copilot model label (default `GPT/GPT-6`). |
| `/status` | Show repository, model, job state, and whether the app is running. |
| `/cancel` | Ask Copilot to stop and discard the running job. |

A bare name uses `GITHUB_OWNER`, or the token's account when it is not set. The checkout directory is `<REPOS_DIR>/<repository name>`, so two repositories with the same name under different owners cannot be used side by side; the bot refuses a directory that belongs to another repository.

One job runs at a time. Progress is shown in a single message that is edited as the job advances; the final message reports the commit, a change summary, the redeploy result, and Copilot's reply.

### What a request does

- **Prepare:** the checkout must be on a branch with no uncommitted changes; the bot fast-forwards it to `origin`.
- **Round-trip:** the engine uploads a ZIP of the committed `HEAD`, asks Copilot for the complete fixed repository, validates the returned ZIP, and replaces the worktree. The bot appends a note asking Copilot to keep a Makefile whose `run` target starts the app in the foreground and to keep secrets and runtime data outside the repository.
- **Commit and push:** all changes are committed on the current branch with the first line of the request as the subject, and pushed to `origin`. If Copilot changed nothing, nothing is committed or deployed. A failed push leaves the commit locally and is reported; the next request pushes it.
- **Redeploy:** the previous `make run` process group for the app is stopped, then `make run` is started detached in `/home/localadmin/repos/<name>` with output appended to `<STATE_DIR>/logs/<name>.log`. The redeploy counts as successful when the process is still running after `DEPLOY_SETTLE_SECONDS` or has exited with code 0; otherwise the log tail is sent. A repository without a `run` target is pushed but not started. Deployed apps do not receive the bot's tokens.

**Important:** the engine applies a returned ZIP as a full replacement. Everything in the checkout except `.git` is replaced, including untracked and ignored files such as `.env`, virtual environments, `node_modules`, and local databases. Keep runtime data and secrets outside the checkout and let `make run` rebuild dependencies.

## Configuration

Settings come from `.env` (or the file named by `BOT_ENV_FILE`); real environment variables take precedence. See `.env.example`.

| Setting | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | required | Bot token. |
| `TELEGRAM_OWNER_ID` | required | Numeric id of the only accepted user. |
| `GITHUB_TOKEN` | required | Used for GitHub API calls and Git over HTTPS through `GIT_ASKPASS`; it is never written into remote URLs. |
| `GITHUB_OWNER` | token's account | Owner for bare repository names. |
| `REPOS_DIR` | `/home/localadmin/repos` | Parent of the app checkouts. |
| `STATE_DIR` | `~/.copilot-telegram-bot` | Active repository and model, process ids, app logs. |
| `COPILOT_PROFILE` | `~/.copilot-repo-browser` | Browser profile created by `make login`. |
| `COPILOT_MODEL` | `GPT/GPT-6` | Initial model label; change it later with `/model`. |
| `COPILOT_TIMEOUT` | `900` | Seconds without new Copilot output before a run gives up. |
| `BOT_TEMPORARY_CHAT` | `true` | Use Copilot's Temporary chat; `false` saves chats in your account. |
| `DEPLOY_SETTLE_SECONDS` | `10` | How long `make run` must stay up to count as deployed. |
| `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL` | Copilot Telegram Bot | Identity on bot commits. |

The engine's selector overrides (`COPILOT_COMPOSER`, `COPILOT_FILE_INPUT`, `COPILOT_SEND`, `COPILOT_DOWNLOAD`, `COPILOT_MODEL_BUTTON`, `COPILOT_TEMPORARY_CHAT`, `COPILOT_REASONING`, `COPILOT_STOP`) are CSS selectors read from the bot's environment when Copilot's UI changes. Run `python3 engine/copilot_repo_roundtrip.py --help` for the engine's options.

## Security

- The bot obeys only `TELEGRAM_OWNER_ID`. Anyone who controls that Telegram account can make Copilot-written code run on the VM.
- A request can create or change a `Makefile` that the bot then runs. Use a dedicated unprivileged VM user, restrict the GitHub token to the repositories involved, and keep `.env` and the browser profile readable only by that user.
- Upload only repositories approved for your organization's Copilot environment.
- Tokens are redacted from Git errors, logs, and Telegram messages, and are removed from the environment of deployed apps.

## Layout

| Path | Purpose |
| --- | --- |
| `engine/copilot_repo_roundtrip.py` | Playwright round-trip engine (`login` and `fix`), the single automation engine. |
| `bot/service.py` | Owner commands and the request pipeline. |
| `bot/telegram_api.py`, `bot/github_api.py` | Telegram long polling and GitHub REST clients (standard library only). |
| `bot/gitops.py`, `bot/repos.py` | Clone, sync, commit, push; repository references. |
| `bot/roundtrip.py`, `bot/deploy.py` | Engine subprocess runner; detached `make run` management. |
| `bin/git-askpass` | Supplies GitHub credentials to Git from the environment. |
| `deploy/` | systemd unit. |

## Tests

```bash
make test
```

The unit tests use fakes for Telegram, GitHub, and Copilot, and local bare repositories for Git. Browser tests in `tests/test_engine.py` require Playwright and Chromium and are skipped if they are unavailable. The tests do not exercise a live Copilot sign-in, a real Telegram bot, or GitHub.
