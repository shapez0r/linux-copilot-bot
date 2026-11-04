# Copilot Telegram Bot

A Telegram bot for an Ubuntu VM that turns chat messages from authorized users into repository changes. For each request it:

1. selects an existing GitHub repository and clones it, or creates a new one from a selected local app template (`eurofish` by default), at `/home/localadmin/repos/<name>`;
2. takes the message as the prompt;
3. sends the repository to Microsoft 365 Copilot Chat through the Playwright round-trip engine and applies the returned repository;
4. commits and pushes the result to GitHub;
5. registers and restarts an enabled systemd system service that runs `make run` from `/home/localadmin/repos/<name>`;
6. for `/new`, allocates the next tunnel port and creates the Cloudflare route.

**Experimental:** the engine drives Copilot's browser UI, so UI changes can interrupt it. Changes are pushed without review; review the commits on GitHub afterwards.

## Requirements

- Ubuntu with Python 3.9+, `python3-venv`, Git, and `make`.
- A Microsoft 365 account with access to Copilot Chat.
- A Telegram bot token from [@BotFather](https://t.me/BotFather) and the numeric Telegram user id of each administrator and regular user (for example from @userinfobot).
- A GitHub token with read/write access to repository contents. Creating repositories requires permission to create repositories for the owner, and `/delete` requires permission to delete repositories.
- A local app checkout to use as a new-repository template. The default is `<REPOS_DIR>/eurofish` (`/home/localadmin/repos/eurofish` with the default configuration).
- For `/new`, route commands, and the tunnel portion of `/delete`, a remotely managed Cloudflare Tunnel and an API token with **Account / Cloudflare Tunnel / Edit** and **Zone / DNS / Edit** permissions.
- systemd and non-interactive `sudo` permission for the bot's Linux account to install, remove, enable, and control its system services.

## Setup

```bash
make setup          # virtual environment, Playwright, Chromium
make system-deps    # Chromium's operating-system libraries (sudo)
sudo mkdir -p /home/localadmin/repos && sudo chown "$USER" /home/localadmin/repos
cp .env.example .env && chmod 600 .env   # then fill in the required values
make login          # sign in to Copilot once (see below)
make run            # first run uses sudo to register and enable the bot service
```

Put the Cloudflare API token, account ID, and zone ID in `.env` to use `/new` or `/cloudflare` and to let `/delete` remove tunnel routes. Without those settings, `/delete` still attempts the GitHub and local cleanup and reports that it skipped the route. The tunnel is always `kurwalab-tunnel` and the domain is always `kurwalab.fyi`. Ordinary redeployment never changes routes; only these commands do.

### Signing in to Copilot

`make login` opens a visible Chromium window; sign in, open a usable Copilot Chat page, and press Enter in the terminal. The profile (default `~/.copilot-repo-browser`) holds authentication state: keep it private. Bot runs use the same profile. Browser visibility defaults to `BOT_SHOW_BROWSER` and can be overridden for one request with `browser` or `nobrowser` in `/fix`. On Linux without `DISPLAY` or `WAYLAND_DISPLAY`, a requested visible bot run falls back to hidden Chromium instead of failing; `make login` still requires a display.

A VM usually has no display. Either run `make login` over `ssh -X` (or a VNC session), or sign in on a Linux desktop and copy the profile directory to the VM. A copied profile may not be accepted by Copilot; if so, sign in on the VM. Profiles from Windows or macOS do not carry over. Run `make login` while the bot is idle or stopped, and repeat it when a run fails with an authentication error.

### Running as a service

The first foreground `make run` uses non-interactive `sudo` to install and enable `/etc/systemd/system/copilot-telegram-bot.service` unless the bot is already running under systemd. It does not start a duplicate process. Configure the bot account for passwordless access to the required `install`, `rm`, and `systemctl` commands so registration and later app management can run without a terminal. Stop the foreground run, then start the registered service:

```bash
sudo systemctl start copilot-telegram-bot
sudo systemctl status copilot-telegram-bot
```

The generated unit runs under the account that launched the bot, sets the repository as `WorkingDirectory`, and starts it with `make run`. `deploy/copilot-telegram-bot.service` is an equivalent editable template whose `User`, `Group`, `HOME`, and paths must match the installation account. Enabled services start at boot through `multi-user.target`. The bot and app services use the checked-out files as they are; they do not run `git pull` during startup.

## Using the bot

Only user ids listed in `TELEGRAM_ADMIN_IDS` or `TELEGRAM_USER_IDS` are served, and only in private chats; every other message is ignored. Separate multiple ids with commas and do not put an id in both settings. `TELEGRAM_OWNER_ID` remains accepted as an alias for `TELEGRAM_ADMIN_IDS`.

Administrators have every bot permission. Regular users can create apps, list all apps, fix apps they created, and start or stop only their own app services. Administrators can start or stop any app, register services for existing app folders, and change the bot itself (see [Changing the bot](#changing-the-bot)). The app list opens with a status summary (for example 🟢 2 running · ⚪ 3 stopped) followed by one card per app: a status icon, the bold name, and the status, with the public URL beneath as plain text that Telegram links itself, so tapping it opens the app without a confirmation prompt; admin cards also show the tunnel port and folder. Long lists are split into complete HTML messages that repeat a short heading. The app list is built from every direct folder under `REPOS_DIR` except the bot repository, including stopped or untracked apps; administrators also see the bot's own `linux-copilot-bot` entry. Its status comes from systemd and distinguishes running, stopped, failed, transitional, and unregistered services. Administrators see each app's name, status, full folder, tunnel port, and tunnel URL; regular users see its name, status without a process id, and public URL. Messages to regular users identify apps only by name and public URL: they omit deployment and tunnel sections, tunnel routes and ports, process ids, server paths, and systemd service details, and failures in those areas are reported generically. The bot repository never appears to regular users (or to administrators in user view): it is left out of their app list, fix and start/stop choices, and template choices, and their requests for it are refused like requests for any app they do not own. Each created app is recorded with an owner: regular-user apps belong to the creating Telegram user id, while administrator-created and newly registered existing apps are owned by `kurwalab.fyi admins`. Active repositories are stored per Telegram user, the selected default model is shared, and all users share the single job slot.

Administrators can switch between **admin view** and **user view** at any time with the **👤 Switch to user view** and **🛡 Switch to admin view** buttons, which are always in their main menu. In user view, an administrator sees and uses the bot exactly as a regular user: the regular-user menu, help, listings, messages, and permissions apply, admin-only commands and buttons are refused, and apps created in that view are public and owned by the administrator's Telegram user id. The choice is stored per administrator and persists across restarts. Regular users cannot switch views.

Send `/start` or `/menu` to open a role-specific persistent keyboard. Regular users see creation, listing, fixing, and start/stop controls for their apps; administrators in admin view also see service registration, deletion, repository, model, status, and tunnel-management options, and every administrator menu includes the view switch. The guided **Create an app** flow:

1. asks for a lowercase app name;
2. checks the models available in Copilot (see below) and offers them for selection;
3. loads template choices from local HTTP routes on the configured Cloudflare tunnel and keeps only apps that also have a local checkout under `REPOS_DIR`;
4. for administrators, offers a choice between a private (default) and a public GitHub repository; regular users skip this step and always get a public repository;
5. for administrators, offers a choice between a **Temporary chat**, which is not saved in Copilot, and a **Regular chat**, which stays in the Copilot chat history; regular users skip this step and use `BOT_TEMPORARY_CHAT`;
6. explains that the template's styling, architecture, and stack are retained by default but can be changed in the app description;
7. accepts the description, together with any files attached as described in [Attaching files](#attaching-files), then shows the existing edited progress message while Copilot builds, commits, deploys, and publishes the app;
8. returns the final public app link when provisioning succeeds.

The guided **Fix an app** flow offers the apps the sender may fix that have a folder under `REPOS_DIR` (the same folders `/list` shows), so ownership records of apps without a local checkout are not offered; the **Delete an app** picker uses the same rule, and a name can still be typed. The flow then checks the available models, accepts the chosen model, lets administrators choose a temporary or regular Copilot chat, accepts the fix request and any attached files, and then runs, commits, pushes, and redeploys the app with those choices. The chosen model and chat type apply only to that request; the shared model set with `/model` is unchanged, and regular users always use `BOT_TEMPORARY_CHAT`.

#### Attaching files

Administrators and regular users can attach any files, such as screenshots, photos, documents, logs, audio, or video, to a request that creates or fixes an app. Send them to the bot before the request text, at any step of the guided flows, or with a caption: the caption is handled as the request text (for example a plain request, `/fix app <request>`, `/new name=app prompt="..."`, or the description in a guided flow), and the files in a Telegram album are all attached to the album's caption. Files without a caption are held for the sender's next create or fix request, which uses and then clears them; the bot confirms the files it is holding. Up to 10 files can be attached to one request, and each file must be at most 20 MB, the largest file a Telegram bot can download. **Cancel / stop**, `/cancel`, and **Main menu** discard held files.

When the request runs, the bot downloads the files into a temporary folder under `STATE_DIR`, attaches them to the Copilot prompt after the repository archive, and removes the folder when the round-trip ends. For `/new`, the files are downloaded before the repository is created, so a failed download stops the request without creating anything. Copilot is told that the files are context and not part of the repository; they are not committed. Copilot must accept each file type: a file it refuses fails the request with an error.

Before either flow offers models, the bot opens Copilot with the browser profile, reads the **Claude** and **GPT** submenus of the model picker, and shows every enabled entry as a `provider/model` button, such as `Claude/Opus 5.5`. The check uses the shared job slot and can be stopped with **Cancel / stop**. If the picker cannot be read, the bot reports why and offers `Claude/Sonnet 5.5`, `Claude/Opus 5.5`, `GPT/GPT-6`, `GPT/GPT-6.1 Sol`, or a free-form provider/model value instead.

Apps created by regular users are public GitHub repositories. Apps created by administrators are private unless the administrator chooses a public repository in the guided flow or passes `public` to `/new`. The administrator menu also provides app service registration and controls, confirmed app deletion, repository selection, model selection, status, and Cloudflare route management. Command forms remain available for automation and direct use, subject to the same permissions.

| Message | Effect |
| --- | --- |
| `/start` or `/menu` | Show the guided app-management keyboard. |
| `/new [public\|private] [tempchat\|notempchat] [model=<provider/model>] [template=<app>] name="<name\|owner/name>" prompt="<request>"` | Create an owned GitHub repository (private by default for administrators, always public for regular users), seed it from the selected local app (`eurofish` by default), run the request with the allocated port instruction, push the result, register and start its systemd service, and publish `https://<name>.kurwalab.fyi`. Available to all configured users; `tempchat` and `notempchat` are administrator-only. The optional model and chat type apply only to this command; quote a model name containing spaces or punctuation, such as `model="Claude/Opus 5.5"`. Straight or typographic quotes are accepted. Refuses existing repositories. |
| `/delete <name>` or `/delete <owner/name>` | **Admin only.** Independently attempt to stop and unregister the app service, remove its Cloudflare tunnel route and matching CNAME, GitHub repository, verified local checkout at `<REPOS_DIR>/<name>`, ownership record, and active selections. Missing resources are successful no-ops; failures and skipped cleanup are reported without blocking other deletion attempts. Cloudflare credentials are needed only for route cleanup, and deleting an existing GitHub repository requires repository-delete permission. |
| `/repo <name>` or `/repo <owner/name>` | **Admin only.** Select the administrator's active repository. A missing repository is created and seeded from `<REPOS_DIR>/eurofish`; an existing one is cloned or fast-forwarded. A GitHub URL is also accepted. An untracked repository selected this way is assigned to `kurwalab.fyi admins`. |
| `/repo` | **Admin only.** Show the administrator's active repository. |
| plain text | Run the text as a request against the sender's active repository. Regular users are restricted to apps they created. |
| files, screenshots, or photos | Attach them to the sender's next create or fix request; a caption runs as that request. See [Attaching files](#attaching-files). |
| `/fix <repo> <request>` | Select a repository and run a request in one message. Administrators can use `linux-copilot-bot` to change the bot itself. Regular users are restricted to apps they created. |
| `/fix [browser\|nobrowser] [tempchat\|notempchat] <request>` | Run a request against the sender's active repository with one or both per-request options. Options may be supplied in either order. Regular users are restricted to apps they created. |
| `/list` | List every direct app folder under `REPOS_DIR` except the bot repository, using each app's current systemd state. Administrators see name, status, folder, tunnel port, and tunnel URL, plus a `linux-copilot-bot` entry with the bot service's status and folder; regular users see name, status without a process id, and public URL. The list is rendered as a status summary plus one card per app (see above). Missing routes are identified, and route details are marked unavailable when Cloudflare is not configured or cannot be reached. Available to all configured users. |
| `/app start <name>` or `/app stop <name>` | Start or stop an app through its systemd service. Administrators may control every app; regular users may control only apps recorded as their own. Starting an unregistered owned app registers and enables its service first. Administrators receive the systemd result; regular users receive the app name and URL. |
| `/register [all\|<name>]` | **Admin only.** Register and enable services for every app folder or one named app. The bot repository is excluded. Registration requires non-interactive `sudo` and a usable `make run` target, does not start a stopped app, assigns previously unowned apps to `kurwalab.fyi admins`, and removes the matching reboot crontab entry when present. |
| `/cloudflare <subdomain> <port>` | **Admin only.** Create or update `<subdomain>.kurwalab.fyi` on `kurwalab-tunnel`, targeting `http://127.0.0.1:<port>`; requires Cloudflare credentials. |
| `/cloudflare list` | **Admin only.** List named ingress routes on `kurwalab-tunnel`. |
| `/cloudflare delete <subdomain>` | **Admin only.** Delete the named ingress route and its matching tunnel CNAME. |
| `/model <provider/model>` | **Admin only.** Choose the shared Copilot model label (default `GPT/GPT-6`). |
| `/status` | **Admin only.** Show the sender's repository, model, job state, and whether the app is running. |
| `/cancel` | Stop the sender's running job and discard the sender's held attachments. Administrators may stop any running job. |

Administrators use `public` or `private` to choose visibility when creating a repository; the default is `private`. Regular users' `/new` apps are always public: `public` is accepted and `private` is refused. For `/new`, specify the app repository with `name="<name|owner/name>"`, for example `/new name="fresh-app" prompt="build a starter app"`. Administrators may add `tempchat` or `notempchat` to use a temporary or a saved regular Copilot chat for that app; without either, `BOT_TEMPORARY_CHAT` applies, and regular users' `/new` commands that include either option are refused. The `name`, `model=<provider/model>`, `template=<app>`, and `prompt="<request>"` options may appear in any order with the visibility and chat-type options, before the request. Straight single or double quotes and their typographic equivalents are accepted around option values. Quote a multiword prompt unless `prompt=` is the final option, in which case it consumes the remaining text. Use the other quote style for quotes inside a quoted request. Empty, malformed, or repeated `prompt=` options are rejected. The positional repository and trailing request forms are also accepted, for example `/new fresh-app build a starter app` or `/new prompt="build a starter app" fresh-app`. App names still must be lowercase DNS labels; quotes do not allow spaces or uppercase letters. The model overrides the active model only while creating that app. The template must name an existing local app directory directly under `<REPOS_DIR>`. Existing repositories keep their visibility. Private repositories are cloned using `GITHUB_TOKEN` through `GIT_ASKPASS`, without storing credentials in remote URLs.

A bare name uses `GITHUB_OWNER`, or the token's account when it is not set. The checkout directory is `<REPOS_DIR>/<repository name>`, so two repositories with the same name under different owners cannot be used side by side; the bot refuses a directory that belongs to another repository.

### New repository template

For every missing repository, the bot creates an empty GitHub repository, copies a local template app into the new checkout, excludes every `.git` entry, and pushes the copy as the single `Initial commit`. `/repo` always uses `<REPOS_DIR>/eurofish`; `/new` uses that default unless `template=<app>` selects another directory under `<REPOS_DIR>`. The template must exist and contain files that Git can commit. The guided creation flow derives its template buttons from one-label hostnames under `kurwalab.fyi` that route to local HTTP ports and have matching local app directories. `/new` then reads the ordered tunnel ingress list, chooses one more than the last local HTTP route port (or `8080` when none exists), selects its per-command model or the active model, and tells Copilot to preserve the selected template's architecture, stack, and styling by default while honoring requested changes. App and template names must be lowercase DNS labels containing only letters, digits, and hyphens.

One job runs at a time. Progress is shown in a single message that is edited as the job advances. Its first line is `Currently doing: ...`, using the latest visible reasoning paragraph, active step, task, or bot stage; once Copilot output is shown, the next line is `Repository: https://github.com/<owner>/<name>`. Each displayed bot stage, task, plan item, activity, and step carries a stable first-seen timestamp in the bot host's local time, formatted as `HH:MM:SS`. A compact stage is shown until Copilot exposes structured output, then the current task and plan, activity or step names, reasoning-duration labels such as `Reasoned for 12s`, and interleaved live answers take its place. Live answers, activity sections, and task cards are read from the whole page, including open shadow roots and same-origin frames. Answers come from Copilot's response messages or, when those are not marked or contain no readable text, from the content that follows the submitted prompt. Visible reasoning text is not discarded: the latest part of each reasoning passage is placed under its step in a collapsed Telegram quote block marked 💭, which expands when tapped. The progress message is sent as Telegram HTML, and when it grows too long the oldest lines are dropped. Code-execution details are omitted, including code blocks, editors, monospace preformatted output, and fenced code. While Copilot is responding, the last line of the message is a ⏳ heartbeat refreshed every 15 seconds with the current timestamp: it states that the prompt is still running when a Stop control is visible or the output recently changed, and otherwise how long neither has been seen against `COPILOT_TIMEOUT`. Ordinary requests end with the commit, change summary, and redeploy result (the app URL for regular users); `/new` also returns Copilot's final output, provisioning status, and public app URL, with the `Deploy:` and `Tunnel:` lines shown only to administrators.

### What a request does

- **Prepare:** the checkout must be on a branch with no uncommitted changes; the bot fast-forwards it to `origin`. A mode-only change of `bin/git-askpass` in the bot's own checkout is reverted first; any other change still stops the request.
- **Round-trip:** the engine uploads a ZIP of the committed `HEAD` together with a `.git` directory holding the checked-out branch's history (without remotes, hooks, or reflogs), so Copilot can read earlier changes with `git log`. It asks Copilot for the complete fixed repository without a `.git` directory, rejects a returned ZIP that contains one, validates the rest, and replaces the worktree. Attached files are uploaded with the archive as context. The bot appends a note asking Copilot to keep a Makefile whose `run` target starts one HTTP server in the foreground and to keep secrets and runtime data outside the repository.
- **Commit and push:** all changes are committed on the current branch with the first line of the request as the subject and a body holding the request followed by Copilot's complete final output (tokens redacted), and pushed to `origin`. Later requests upload this history, so Copilot can see what each earlier change did. If Copilot changed nothing, nothing is committed or deployed. A failed push leaves the commit locally and is reported; the next request pushes it.
- **Redeploy:** the bot uses non-interactive `sudo` to write and enable `/etc/systemd/system/copilot-app-<name>.service`, then asks systemd to restart it. The unit sets the app repository as `WorkingDirectory`, executes `make run`, restarts on failure, removes bot secrets from its environment, and appends output to `<STATE_DIR>/logs/<name>.log`. The redeploy counts as successful only when systemd reports the service active through `DEPLOY_SETTLE_SECONDS`; otherwise the systemd state and log tail are sent. A repository without a usable `run` target is pushed but not started.
- **New-app publish:** after a successful `/new` service restart, the bot creates `<name>.kurwalab.fyi` on `kurwalab-tunnel`, targeting the allocated `127.0.0.1` port. If deployment or routing fails, the report identifies the completed and failed stages.
- **App deletion:** `/delete app` independently attempts to stop, disable, and remove its systemd unit, delete its GitHub repository, remove a matching verified checkout at `<REPOS_DIR>/app`, and delete its Cloudflare route and tunnel CNAME. The matching reboot crontab line is also removed when present. Missing resources need no cleanup. Every step is reported, and a failure or missing Cloudflare configuration does not block the other attempts.
- **Routes on demand:** `/cloudflare eurofish 8080` creates or updates `eurofish.kurwalab.fyi` to `http://127.0.0.1:8080` on `kurwalab-tunnel` and maintains its proxied DNS CNAME. `/cloudflare list` shows named ingress rules, and `/cloudflare delete eurofish` removes that ingress rule and its matching tunnel CNAME. Route names must be one lowercase DNS label, and ports must be 1–65535. Other ingress rules are preserved. Ordinary deployment never changes routes.

**Important:** the engine applies a returned ZIP as a full replacement. Everything in the checkout except `.git` is replaced, including untracked and ignored files such as `.env`, virtual environments, `node_modules`, and local databases. Keep runtime data and secrets outside the checkout and let `make run` rebuild dependencies.

### Changing the bot

The bot's own checkout, `<REPOS_DIR>/linux-copilot-bot`, can be changed only by administrators in admin view, through **Fix an app**, `/fix linux-copilot-bot <request>`, `/repo linux-copilot-bot`, or plain text after selecting it. A bare `linux-copilot-bot` name resolves to the GitHub repository of the checkout's `origin`. The bot never creates that repository, records an owner for it, registers it as an app service, deletes it, or offers it as a template. A bot request:

- fast-forwards the live checkout, then runs the round-trip in a separate temporary clone, so the live checkout's ignored `.env` and `.venv` are not replaced;
- asks Copilot to keep the `run` target that starts the bot service and to keep secrets outside the repository;
- commits and pushes the result, then fast-forwards the live checkout;
- reports the commit, then queues a non-blocking restart of `copilot-telegram-bot.service` through non-interactive `sudo` so the bot comes back with the new code; systemd may terminate the `sudo` call as part of that restart, which is not treated as a failure. When the bot was not started by systemd, it is not restarted and the report asks for a manual restart.

## Configuration

Settings come from `.env` (or the file named by `BOT_ENV_FILE`); real environment variables take precedence. See `.env.example`.

| Setting | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | required | Bot token. |
| `TELEGRAM_ADMIN_IDS` | required | Comma-separated numeric ids with full bot access. `TELEGRAM_ADMIN_ID` and `TELEGRAM_OWNER_ID` are accepted aliases. |
| `TELEGRAM_USER_IDS` | empty | Comma-separated numeric ids that may create and list apps and fix, start, or stop their own apps. `TELEGRAM_USER_ID` is accepted as an alias. |
| `GITHUB_TOKEN` | required | Used for GitHub API calls and Git over HTTPS through `GIT_ASKPASS` (including private repository clones); it is never written into remote URLs. |
| `GITHUB_OWNER` | token's account | Owner for bare repository names. |
| `REPOS_DIR` | `/home/localadmin/repos` | Parent of app checkouts and local app templates; `eurofish` is the default template. |
| `STATE_DIR` | `~/.copilot-telegram-bot` | App ownership, per-user active repositories, administrators' view choices, shared model, app logs, and the executable copy of the Git credential helper. |
| `COPILOT_PROFILE` | `~/.copilot-repo-browser` | Browser profile created by `make login`. |
| `COPILOT_MODEL` | `GPT/GPT-6` | Initial model label; change it later with `/model`. |
| `COPILOT_TIMEOUT` | `30` | Seconds without a visible Stop control, output change, or task-card change before a run gives up. The run keeps waiting while a Stop control is visible in the current task card (revealed by hovering it) or near the prompt input, even when no output change is detected. Output detection starts after submission and covers the whole page, including open shadow roots and same-origin frames. |
| `BOT_SHOW_BROWSER` | `false` | Default browser visibility. `browser` or `nobrowser` on `/fix` overrides it for that request. Without `DISPLAY` or `WAYLAND_DISPLAY`, a visible request runs hidden; use VNC or X forwarding to see it. |
| `BOT_TEMPORARY_CHAT` | `true` | Default Temporary chat state. `tempchat` or `notempchat` on `/fix`, an administrator's `tempchat` or `notempchat` on `/new`, or an administrator's chat-type choice in the guided flows overrides it for that request; disabling it saves the chat in your account. |
| `DEPLOY_SETTLE_SECONDS` | `10` | How long an app's systemd service must remain active to count as a successful start or redeploy. |
| `CLOUDFLARE_API_TOKEN` | empty | Enables `/new` publishing and Cloudflare route management, including `/delete` route cleanup; requires Cloudflare Tunnel Edit and DNS Edit permissions. It is not passed to deployed apps. |
| `CLOUDFLARE_ACCOUNT_ID` | empty | Account containing the tunnel; required with the Cloudflare token. |
| `CLOUDFLARE_ZONE_ID` | empty | Zone containing the public hostname; required with the Cloudflare token. |
| `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL` | Copilot Telegram Bot | Identity on bot commits. |

The engine's selector overrides (`COPILOT_COMPOSER`, `COPILOT_FILE_INPUT`, `COPILOT_SEND`, `COPILOT_DOWNLOAD`, `COPILOT_MODEL_BUTTON`, `COPILOT_TEMPORARY_CHAT`, `COPILOT_REASONING`, `COPILOT_STOP`) are CSS selectors read from the bot's environment when Copilot's UI changes. To set the chat type, the engine clicks the first visible match of `COPILOT_TEMPORARY_CHAT`, then a visible button, switch, or menu entry labelled Temporary chat; when none appears within a few seconds it also opens popup menus such as new-chat or more-options menus to look for that entry, and it fails only after 30 seconds without one. A **Temporary chat** label on such a control is not read as the active state. Run `python3 engine/copilot_repo_roundtrip.py --help` for the engine's options; its `models` command prints the Claude and GPT models offered by the picker as JSON, and the repeatable `fix --attach <path>` option attaches extra files to the prompt.

Without `.env` values or per-request overrides, requests use `nobrowser` and `tempchat`. Telegram options and guided chat-type choices apply only to the request that contains them and take precedence over `.env`.

## Security

- The bot obeys only Telegram user ids in `TELEGRAM_ADMIN_IDS` and `TELEGRAM_USER_IDS`. Administrators can manage every app and infrastructure setting; regular users can execute Copilot-written code only through apps recorded as their own.
- A request can create or change a `Makefile` that the bot then runs. Use a dedicated unprivileged VM user, restrict its passwordless sudo rules to the required service-management commands, restrict the GitHub token to the repositories involved, and keep `.env` and the browser profile readable only by that user.
- Upload only repositories and attachments approved for your organization's Copilot environment. Each upload includes the branch's full Git history, and commit messages include Copilot's outputs; on public repositories these are visible to anyone. Attachments are kept only in a temporary folder under `STATE_DIR` while their request runs.
- Telegram, GitHub, and Cloudflare API tokens are redacted from errors and messages. App units explicitly unset these token variables and `VIRTUAL_ENV`.

## Layout

| Path | Purpose |
| --- | --- |
| `engine/copilot_repo_roundtrip.py` | Playwright round-trip engine (`login`, `models`, and `fix`), the single automation engine. |
| `bot/service.py`, `bot/state.py` | Role-aware menus, admin/user view switching, permissions, app ownership, per-user selections, guided conversations, commands, held attachments, and the request pipeline. |
| `bot/app_list.py` | Telegram HTML app list: status summary and per-app cards, split into messages within Telegram's size limit. |
| `bot/telegram_api.py`, `bot/github_api.py` | Telegram long polling, reply keyboards, HTML-formatted messages, file downloads, and GitHub REST clients (standard library only). |
| `bot/gitops.py`, `bot/repos.py` | Seed, clone, sync, commit, push; repository references. |
| `bot/roundtrip.py`, `bot/deploy.py` | Engine subprocess runner; sudo-backed systemd system-unit registration, lifecycle control, status, and app logs. |
| `bot/cloudflare.py` | Cloudflare tunnel ingress and DNS route management. |
| `bin/git-askpass` | Supplies GitHub credentials to Git from the environment; Git runs an executable copy in `STATE_DIR`, so checkouts keep their committed file modes. |
| `deploy/` | Editable systemd system-unit template for the bot. |

## Tests

```bash
make test
```

The unit tests use fakes for Telegram, GitHub, Copilot, and Cloudflare, and local bare repositories for Git. Browser tests in `tests/test_engine.py` require Playwright and Chromium and are skipped if they are unavailable; set `ROUNDTRIP_TEST_CHROMIUM` to an existing Chromium executable to run them with that browser. The tests do not exercise a live Copilot sign-in, Telegram bot, GitHub account, or Cloudflare account.
