# Claude Code notes — Hyakkano

Hyakkano is a custom two-character Hermes Agent Discord deployment. Karane and Hakari run as separate Hermes gateway processes but share Discord channels.

## Start here

- Discord adapter: `plugins/platforms/discord/adapter.py`
- Shared turn director: `customizations/mood_reply/__init__.py`
- Contributor instructions: `AGENTS.md`
- Regression test: `tests/gateway/test_discord_free_response.py`

## Non-negotiable behaviour

- A generic user message in the shared channel has one selected responder.
- Naming a character selects that character. Only explicit wording such as “you both” permits both.
- Keep bot-to-bot roleplay short. A real user question may be a normal, useful answer.
- Native slash commands (including `/clear-chat`) must bypass reply selection.
- Karane and Hakari are independent processes: in-memory coordination is insufficient.

## Environment

The checkout is `/home/ubuntu/workspace/hermes-agent`. Runtime homes are `~/.hermes` and `~/.hermes-hakari`; services are `hermes-emilia.service` and `hermes-hakari.service`.

Do not read into commits or expose tokens, `.env` files, `cli-config.yaml`, runtime sessions, memories, or personal soul files. Runtime configuration is deliberately not versioned.

## Validate and deploy

```bash
python3 -m py_compile plugins/platforms/discord/adapter.py customizations/mood_reply/__init__.py
.venv/bin/python -m pytest tests/gateway/test_discord_free_response.py -q
sudo systemctl restart hermes-emilia.service hermes-hakari.service
systemctl is-active hermes-emilia.service hermes-hakari.service
```

If changing `customizations/mood_reply`, copy that exact directory into both runtime profiles before restart. Prefer focused changes and regression tests over broad refactors.
