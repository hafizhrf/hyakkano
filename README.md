# Hyakkano

Custom multi-agent Discord deployment built on [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Hyakkano runs two character agents—**Karane** and **Hakari**—in a shared Discord channel. A shared AI turn director chooses which agent should answer a general message, so the conversation feels like a natural group chat instead of two bots replying at once.

## What is customized

- Shared-channel turn taking: one bot is selected for a normal user message.
- Direct names select that character; an explicit request to both lets both answer.
- Character-to-character replies stay short, while substantive user questions receive a complete answer.
- Cross-process claims prevent duplicate replies when both gateway processes see the same message.
- Native Discord moderation command: `/clear-chat amount` deletes up to 1,000 recent unpinned messages in the current channel.

The custom gateway code lives in `plugins/platforms/discord/adapter.py`. The deployable shared-channel gate is versioned in `customizations/mood_reply/`.

## How it runs

Karane and Hakari are **two independent gateway processes**, each with its own Discord bot and its own runtime profile directory. They only meet inside Discord — there is no shared in-memory state, which is why coordination is done through cross-process claims.

| Character | Runtime profile (`HERMES_HOME`) | systemd service |
| --- | --- | --- |
| Karane | `~/.hermes` | `hermes-emilia.service` |
| Hakari | `~/.hermes-hakari` | `hermes-hakari.service` |

Each profile holds its own credentials, `.env`, session history, memories, and character soul file. **These directories are deliberately kept out of this repository — never commit them.**

## Layout

| Path | Purpose |
| --- | --- |
| `plugins/platforms/discord/adapter.py` | Discord events, slash commands, channel context |
| `customizations/mood_reply/` | Shared reply-selection plugin to install into each profile |
| `AGENTS.md` | Instructions for any coding agent working in this repository |
| `CLAUDE.md` | Claude Code-oriented project notes |

---

## Setup

### 1. Install the code

```bash
git clone git@github.com:hafizhrf/hyakkano.git
cd hyakkano
python3 -m venv .venv
.venv/bin/pip install -e '.[all]'
```

### 2. Create the Discord bots

You need **two** Discord applications — one for Karane, one for Hakari. Repeat these steps twice.

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** (name it Karane, then again for Hakari).
2. Open the **Bot** tab → **Add Bot** → **Reset Token** and copy the token. This is the value you put in `DISCORD_BOT_TOKEN` (see below). Keep it secret.
3. Under **Privileged Gateway Intents**, enable **Message Content Intent**. The gateway needs it to read channel messages. (Enable **Server Members Intent** too if you plan to allow users by username or by role rather than by numeric ID.)
4. Open **OAuth2 → URL Generator**, select the scopes `bot` and `applications.commands`, then grant the bot at least: *Read Messages/View Channels*, *Send Messages*, *Read Message History*, *Manage Messages* (required for `/clear-chat`).
5. Open the generated invite URL and add the bot to your server. Do this for **both** bots so they share the same channel.

### 3. Configure each profile

Configuration lives in each profile's own `.env` (e.g. `~/.hermes/.env` and `~/.hermes-hakari/.env`) — **not** in tracked files in this repo. The easiest way to write it is the interactive wizard, run once per profile:

```bash
# Karane (default profile → ~/.hermes)
.venv/bin/hermes setup

# Hakari (→ ~/.hermes-hakari)
HERMES_HOME=~/.hermes-hakari .venv/bin/hermes setup
```

The wizard prompts for the model provider, the Discord bot token, and an allowlist. Give each profile the token of its *own* bot.

If you prefer to edit `.env` by hand, these are the Discord-relevant keys. Set them in **each** profile with that character's own bot token:

| Key | Required | Purpose |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | **yes** | Bot token from the Developer Portal (per character) |
| `DISCORD_ALLOWED_USERS` | recommended | Comma-separated Discord user IDs allowed to talk to the bot. Without it, anyone can use it. |
| `DISCORD_ALLOWED_ROLES` | optional | Comma-separated role IDs allowed to talk to the bot |
| `DISCORD_ALLOWED_CHANNELS` | optional | Restrict the bot to specific channel IDs |
| `DISCORD_IGNORED_CHANNELS` | optional | Channels the bot should ignore |
| `GATEWAY_ALLOW_ALL_USERS` | optional | `true` opens the bot to everyone (default `false` = deny unless allowlisted) |
| `DISCORD_ALLOW_MENTION_EVERYONE` | optional | Allow `@everyone`/`@here` pings (default `false`) |
| `DISCORD_ALLOW_MENTION_ROLES` | optional | Allow role pings (default `false`) |

> To copy a Discord user ID: enable **Developer Mode** (Settings → Advanced), then right-click a name → **Copy ID**.

Model-provider settings (API keys, model choice) also go through `hermes setup` / the profile `.env` and `cli-config.yaml`. See the upstream `.env.example` in this repo for the full list of provider variables.

### 4. Install the shared-channel plugin into both profiles

The `mood_reply` gate is versioned here but has to be copied into each runtime profile's `plugins/` directory:

```bash
for HOME_DIR in ~/.hermes ~/.hermes-hakari; do
  mkdir -p "$HOME_DIR/plugins"
  cp -R customizations/mood_reply "$HOME_DIR/plugins/mood_reply"
done
```

Re-run this whenever `customizations/mood_reply/` changes — both profiles must run the same version.

### 5. Run the gateways

For local/foreground testing:

```bash
HERMES_HOME=~/.hermes           .venv/bin/hermes gateway run   # Karane
HERMES_HOME=~/.hermes-hakari    .venv/bin/hermes gateway run   # Hakari
```

In production both run as systemd services:

```bash
sudo systemctl restart hermes-emilia.service hermes-hakari.service
systemctl is-active hermes-emilia.service hermes-hakari.service
journalctl -u hermes-emilia.service -u hermes-hakari.service -n 100 --no-pager
```

---

## Behaviour contract

- A normal question in a shared channel gets one primary answer.
- `Karane, ...` or `Hakari, ...` routes to the named character.
- “you both”, “both of you”, or an equivalent explicit request permits both.
- Bot-to-bot roleplay is intentionally compact; do not turn a simple exchange into a long scene.
- Slash commands are passed to the native Discord command dispatcher and must not be intercepted by reply selection.

## Validation

```bash
scripts/run_tests.sh
python3 -m py_compile plugins/platforms/discord/adapter.py customizations/mood_reply/__init__.py
```

Run targeted tests when changing Discord behaviour:

```bash
.venv/bin/python -m pytest tests/gateway/test_discord_free_response.py -q
```

## Upstream and license

This is a customized deployment/fork of Hermes Agent by Nous Research. Upstream source and documentation: <https://github.com/NousResearch/hermes-agent>. The upstream MIT license is retained in [LICENSE](LICENSE).
</content>
</invoke>
