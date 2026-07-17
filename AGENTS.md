# Hyakkano agent guide

## Scope

This repository is a custom Hermes Agent deployment for two Discord characters: Karane and Hakari. Preserve upstream Hermes behaviour unless the requested change is specifically about the shared Discord experience.

## Important code paths

- `plugins/platforms/discord/adapter.py`: Discord gateway, message context, native slash commands.
- `customizations/mood_reply/`: reply selection and compact roleplay policy. It is copied to each runtime profile's `plugins/` directory.
- `tests/gateway/test_discord_free_response.py`: regression coverage for shared free-response behaviour.

## Runtime separation

Source code is in this checkout. Runtime state is outside the repository:

- Karane profile: `~/.hermes`
- Hakari profile: `~/.hermes-hakari`
- services: `hermes-emilia.service`, `hermes-hakari.service`

Do not commit, print, or move credentials, Discord tokens, provider keys, sessions, memories, or local `SOUL.md` files. Treat `.env`, `cli-config.yaml`, and profile directories as private.

## Shared-channel rules

1. One normal user message should produce one primary character response.
2. A direct name routes to that character.
3. Only an explicit request to both may allow both characters to reply.
4. Partner-to-partner banter should be compact; user questions and real tasks may be longer.
5. Slash commands must reach the Discord dispatcher without the mood gate selecting or suppressing them.
6. Any coordination mechanism must work across two separate gateway processes.

## Workflow

1. Inspect existing behaviour and tests before changing gateway logic.
2. Make source edits in this repository, not only in `~/.hermes*`.
3. When `mood_reply` changes, deploy the same plugin version to both profiles.
4. Validate with the narrow test first, then `scripts/run_tests.sh` when dependencies are installed.
5. Restart both services only after the source/plugin is ready, then check their status and logs.

Useful commands:

```bash
python3 -m py_compile plugins/platforms/discord/adapter.py customizations/mood_reply/__init__.py
.venv/bin/python -m pytest tests/gateway/test_discord_free_response.py -q
sudo systemctl restart hermes-emilia.service hermes-hakari.service
journalctl -u hermes-emilia.service -u hermes-hakari.service -n 100 --no-pager
```

## Change quality

- Keep Discord handlers non-blocking and defensive around network/API failures.
- Preserve pin-protection and authorization checks in `/clear-chat`.
- Avoid deterministic “both bots answer” logic; use the shared selector and atomic claims.
- Add or update focused tests for externally visible Discord behaviour.
- Do not commit generated caches, virtual environments, or runtime transcript data.
