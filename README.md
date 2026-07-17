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

## Layout

| Path | Purpose |
| --- | --- |
| `plugins/platforms/discord/adapter.py` | Discord events, slash commands, channel context |
| `customizations/mood_reply/` | Shared reply-selection plugin to install into each profile |
| `AGENTS.md` | Instructions for any coding agent working in this repository |
| `CLAUDE.md` | Claude Code-oriented project notes |

Runtime profiles deliberately stay outside this repository:

- Karane: `~/.hermes`
- Hakari: `~/.hermes-hakari`

Those profiles contain credentials, session history, local memories, and character soul files. **Never commit them.**

## Local development

```bash
git clone git@github.com:hafizhrf/hyakkano.git
cd hyakkano
python3 -m venv .venv
.venv/bin/pip install -e '.[all]'
```

Configure Discord and model-provider credentials in the runtime profiles, never in tracked files. See Hermes Agent's upstream documentation for provider and gateway setup.

To install the shared-channel plugin into a profile:

```bash
mkdir -p "$HERMES_HOME/plugins"
cp -R customizations/mood_reply "$HERMES_HOME/plugins/mood_reply"
```

Repeat for both profiles. Restart the gateways after changing source or plugin code:

```bash
sudo systemctl restart hermes-emilia.service hermes-hakari.service
systemctl is-active hermes-emilia.service hermes-hakari.service
journalctl -u hermes-emilia.service -u hermes-hakari.service -n 100 --no-pager
```

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
