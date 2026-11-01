# Copilot Telegram Bot

A Telegram bot for an Ubuntu VM that turns chat messages from authorized users into repository changes. For each request it:

1. selects an existing GitHub repository and clones it, or creates a new one from the local `eurofish` reference app, at `/home/localadmin/repos/<name>`;
2. takes the message as the prompt;
3. sends the repository to Microsoft 365 Copilot Chat through the Playwright round-trip engine and applies the returned repository;
4. commits and pushes the result to GitHub;
5. redeploys the app by running `make run` from `/home/localadmin/repos/<name>`;
6. for `/new`, allocates the next tunnel port, creates the Cloudflare route, and adds reboot startup to the user crontab.

**Experimental:** the engine drives Copilot's browser UI, so UI changes can interrupt it. Changes are pushed without review; review the commits on GitHub afterwards.

## Requirements

- Ubuntu with Python 3.9+, `python3-venv`, Git, and `make`.
- A Microsoft 365 account with access to Copilot Chat.
- A Telegram bot token from [@BotFather](https://t.me/BotFather) and the numeric Telegram user id of each authorized user (for example from @userinfobot).
- A GitHub token with read/write access to repository contents. Creating repositories also requires permission to create repositories for the owner.
- A reference app checkout at `<REPOS_DIR>/eurofish` (by default `/home/localadmin/repos/eurofish`) for new repositories.
- For `/new` and route commands, a remotely managed Cloudflare Tunnel and an API token with **Account / Cloudflare Tunnel / Edit** and **Zone / DNS / Edit** permissions.
- The `crontab` command for installing each new app's reboot entry.

## Setup

```bash
make setup          # virtual environment, Playwright, Chromium
make system-deps    # Chromium's operating-system libraries (sudo)
sudo mkdir -p /home/localadmin/repos && sudo chown "$USER" /home/localadmin/repos
cp .env.example .env && chmod 600 .env   # then fill in the required values
make login          # sign in to Copilot once (see below)
make run            # start the bot in the foreground
```

Put the Cloudflare API token, account ID, and zone ID in `.env` to use `/new` or `/cloudflare`. The tunnel is always `kurwalab-tunnel` and the domain is always `kurwalab.fyi`. Ordinary redeployment never changes routes; only `/new` and `/cloudflare` do.

### Signing in to Copilot

`make login` opens a visible Chromium window; sign in, open a usable Copilot Chat page, and press Enter in the terminal. The profile (default `~/.copilot-repo-browser`) holds authentication state: keep it private. Bot runs use the same profile. Browser visibility defaults to `BOT_SHOW_BROWSER` and can be overridden for one request with `browser` or `nobrowser` in `/fix`. On Linux without `DISPLAY` or `WAYLAND_DISPLAY`, a requested visible bot run falls back to hidden Chromium instead of failing; `make login` still requires a display.

A VM usually has no display. Either run `make login` over `ssh -X` (or a VNC session), or sign in on a Linux desktop and copy the profile directory to the VM. A copied profile may not be accepted by Copilot; if so, sign in on the VM. Profiles from Windows or macOS do not carry over. Run `make login` while the bot is idle or stopped, and repeat it when a run fails with an authentication error.

### Running as a service

Edit `deploy/copilot-telegram-bot.service` (user and paths), install it under `/etc/systemd/system/`, and run `sudo systemctl enable --now copilot-telegram-bot`. The unit uses `KillMode=process`, so apps started by `make run` keep running when the bot restarts.

## Using the bot

Only user ids listed in `TELEGRAM_OWNER_ID` are served, and only in private chats; every other message is ignored. Separate multiple ids with commas. Authorized users share the active repository, model, and single job slot.

| Message | Effect |
| --- | --- |
| `/new <name> <request>` or `/new <owner/name> <request>` | Create a new GitHub repository (private by default), seed it from `<REPOS_DIR>/eurofish`, run the request with the allocated port instruction, push the result, deploy it, publish `https://<name>.kurwalab.fyi`, and install reboot startup. Refuses existing repositories. |
| `/repo <name>` or `/repo <owner/name>` | Select the active repository. A missing repository is created and seeded from `<REPOS_DIR>/eurofish`; an existing one is cloned or fast-forwarded. A GitHub URL is also accepted. |
| `/repo` | Show the active repository. |
| plain text | Run the text as a request against the active repository. |
| `/fix <repo> <request>` | Select a repository and run a request in one message. |
| `/fix [browser\|nobrowser] [tempchat\|notempchat] <request>` | Run a request against the active repository with one or both per-request options. Options may be supplied in either order. |
| `/cloudflare <subdomain> <port>` | Create or update `<subdomain>.kurwalab.fyi` on `kurwalab-tunnel`, targeting `http://127.0.0.1:<port>`; requires Cloudflare credentials. |
| `/cloudflare list` | List named ingress routes on `kurwalab-tunnel`. |
| `/cloudflare delete <subdomain>` | Delete the named ingress route and its matching tunnel CNAME. |
| `/model <provider/model>` | Choose the Copilot model label (default `GPT/GPT-6`). |
| `/status` | Show repository, model, job state, and whether the app is running. |
| `/cancel` | Ask Copilot to stop and discard the running job. |

Use `public` or `private` before the name when creating a repository; the default is `private`. Existing repositories keep their visibility. Private repositories are cloned using `GITHUB_TOKEN` through `GIT_ASKPASS`, without storing credentials in remote URLs.

A bare name uses `GITHUB_OWNER`, or the token's account when it is not set. The checkout directory is `<REPOS_DIR>/<repository name>`, so two repositories with the same name under different owners cannot be used side by side; the bot refuses a directory that belongs to another repository.

### New repository template

For every missing repository, the bot creates an empty GitHub repository, copies the contents of `<REPOS_DIR>/eurofish` into the new checkout, excludes every `.git` entry, and pushes the copy as the single `Initial commit`. The reference app must exist and contain files that Git can commit. `/new <name> <request>` then reads the ordered tunnel ingress list, chooses one more than the last local HTTP route port (or `8080` when none exists), and prefixes the request with `Instead of this app, let's create another app, with the same architecture, stack and styling, but running on the <port>`. App names must be lowercase DNS labels containing only letters, digits, and hyphens.

One job runs at a time. Progress is shown in a single message that is edited as the job advances. Its first line is `Currently doing: ...`, using the latest visible reasoning paragraph, active step, task, or bot stage. Each displayed bot stage, task, plan item, activity, and step carries a stable first-seen timestamp with second precision, the bot host's local timezone name, and its UTC offset. A compact stage is shown until Copilot exposes structured output, then the current task and plan, activity or step names, reasoning-duration labels such as `Reasoned for 12s`, and interleaved live answers take its place. Other reasoning and code-execution details are omitted. Once structured output is visible, repetitive inactivity text stays hidden. Ordinary requests end with the commit, change summary, and redeploy result; `/new` also returns Copilot's final output, provisioning status, and public app URL.

### What a request does

- **Prepare:** the checkout must be on a branch with no uncommitted changes; the bot fast-forwards it to `origin`.
- **Round-trip:** the engine uploads a ZIP of the committed `HEAD`, asks Copilot for the complete fixed repository, validates the returned ZIP, and replaces the worktree. The bot appends a note asking Copilot to keep a Makefile whose `run` target starts one HTTP server in the foreground and to keep secrets and runtime data outside the repository.
- **Commit and push:** all changes are committed on the current branch with the first line of the request as the subject, and pushed to `origin`. If Copilot changed nothing, nothing is committed or deployed. A failed push leaves the commit locally and is reported; the next request pushes it.
- **Redeploy:** the previous `make run` process group for the app is stopped, then `make run` is started detached in `/home/localadmin/repos/<name>` with output appended to `<STATE_DIR>/logs/<name>.log`. The redeploy counts as successful when the process is still running after `DEPLOY_SETTLE_SECONDS` or has exited with code 0; otherwise the log tail is sent. A repository without a `run` target is pushed but not started. Deployed apps do not receive the bot's tokens.
- **New-app publish:** after a successful `/new` redeploy, the bot creates `<name>.kurwalab.fyi` on `kurwalab-tunnel`, targeting the allocated `127.0.0.1` port, and adds `@reboot cd <REPOS_DIR>/<name> && git pull && make run` to the bot user's crontab without duplicating it. If deployment, routing, or crontab setup fails, the report identifies the completed and failed stages.
- **Routes on demand:** `/cloudflare eurofish 8080` creates or updates `eurofish.kurwalab.fyi` to `http://127.0.0.1:8080` on `kurwalab-tunnel` and maintains its proxied DNS CNAME. `/cloudflare list` shows named ingress rules, and `/cloudflare delete eurofish` removes that ingress rule and its matching tunnel CNAME. Route names must be one lowercase DNS label, and ports must be 1–65535. Other ingress rules are preserved. Ordinary deployment never changes routes.

**Important:** the engine applies a returned ZIP as a full replacement. Everything in the checkout except `.git` is replaced, including untracked and ignored files such as `.env`, virtual environments, `node_modules`, and local databases. Keep runtime data and secrets outside the checkout and let `make run` rebuild dependencies.

## Configuration

Settings come from `.env` (or the file named by `BOT_ENV_FILE`); real environment variables take precedence. See `.env.example`.

| Setting | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | required | Bot token. |
| `TELEGRAM_OWNER_ID` | required | Comma-separated numeric ids of accepted users. |
| `GITHUB_TOKEN` | required | Used for GitHub API calls and Git over HTTPS through `GIT_ASKPASS` (including private repository clones); it is never written into remote URLs. |
| `GITHUB_OWNER` | token's account | Owner for bare repository names. |
| `REPOS_DIR` | `/home/localadmin/repos` | Parent of app checkouts and the `eurofish` reference app used for new repositories. |
| `STATE_DIR` | `~/.copilot-telegram-bot` | Active repository and model, process ids, app logs. |
| `COPILOT_PROFILE` | `~/.copilot-repo-browser` | Browser profile created by `make login`. |
| `COPILOT_MODEL` | `GPT/GPT-6` | Initial model label; change it later with `/model`. |
| `COPILOT_TIMEOUT` | `900` | Seconds without a visible output change or task-card change before a run gives up; output detection starts after submission. |
| `BOT_SHOW_BROWSER` | `false` | Default browser visibility. `browser` or `nobrowser` on `/fix` overrides it for that request. Without `DISPLAY` or `WAYLAND_DISPLAY`, a visible request runs hidden; use VNC or X forwarding to see it. |
| `BOT_TEMPORARY_CHAT` | `true` | Default Temporary chat state. `tempchat` or `notempchat` on `/fix` overrides it for that request; disabling it saves the chat in your account. |
| `DEPLOY_SETTLE_SECONDS` | `10` | How long `make run` must stay up to count as deployed. |
| `CLOUDFLARE_API_TOKEN` | empty | Enables `/new` publishing and `/cloudflare` route management; requires Cloudflare Tunnel Edit and DNS Edit permissions. It is not passed to deployed apps. |
| `CLOUDFLARE_ACCOUNT_ID` | empty | Account containing the tunnel; required with the Cloudflare token. |
| `CLOUDFLARE_ZONE_ID` | empty | Zone containing the public hostname; required with the Cloudflare token. |
| `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL` | Copilot Telegram Bot | Identity on bot commits. |

The engine's selector overrides (`COPILOT_COMPOSER`, `COPILOT_FILE_INPUT`, `COPILOT_SEND`, `COPILOT_DOWNLOAD`, `COPILOT_MODEL_BUTTON`, `COPILOT_TEMPORARY_CHAT`, `COPILOT_REASONING`, `COPILOT_STOP`) are CSS selectors read from the bot's environment when Copilot's UI changes. Run `python3 engine/copilot_repo_roundtrip.py --help` for the engine's options.

Without `.env` values or `/fix` overrides, requests use `nobrowser` and `tempchat`. Telegram options apply only to the request that contains them and take precedence over `.env`.

## Security

- The bot obeys only the Telegram user ids in `TELEGRAM_OWNER_ID`. Anyone who controls an authorized account can make Copilot-written code run on the VM.
- A request can create or change a `Makefile` that the bot then runs. Use a dedicated unprivileged VM user, restrict the GitHub token to the repositories involved, and keep `.env` and the browser profile readable only by that user.
- Upload only repositories approved for your organization's Copilot environment.
- Telegram, GitHub, and Cloudflare API tokens are redacted from errors and messages and are removed from the environment of deployed apps.

## Layout

| Path | Purpose |
| --- | --- |
| `engine/copilot_repo_roundtrip.py` | Playwright round-trip engine (`login` and `fix`), the single automation engine. |
| `bot/service.py` | Authorized-user commands and the request pipeline. |
| `bot/telegram_api.py`, `bot/github_api.py` | Telegram long polling and GitHub REST clients (standard library only). |
| `bot/gitops.py`, `bot/repos.py` | Seed, clone, sync, commit, push; repository references. |
| `bot/roundtrip.py`, `bot/deploy.py` | Engine subprocess runner; detached `make run` management and reboot crontab entries. |
| `bot/cloudflare.py` | Cloudflare tunnel ingress and DNS route management. |
| `bin/git-askpass` | Supplies GitHub credentials to Git from the environment. |
| `deploy/` | systemd unit. |

## Tests

```bash
make test
```

The unit tests use fakes for Telegram, GitHub, Copilot, and Cloudflare, and local bare repositories for Git. Browser tests in `tests/test_engine.py` require Playwright and Chromium and are skipped if they are unavailable. The tests do not exercise a live Copilot sign-in, Telegram bot, GitHub account, or Cloudflare account.
