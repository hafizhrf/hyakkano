"""mood_reply — AI-decided reply gate for a multi-bot Discord channel.

Two Hermes bots (Karane + Hakari) share one channel, both free-response.
Without a gate both answer every message, which feels robotic and they never
react to each other. This plugin registers a ``pre_gateway_dispatch`` hook that
decides, per message, whether THIS bot should reply — using a small YES/NO
call to the local 9router chat-completions endpoint (an "LLM judge"), so the
decision is natural instead of a coin flip.

Decision flow (discord group/channel messages only; DMs & other platforms pass
through untouched):
  1. Partner-bot messages:
       * loop guard: at most BOT_CHAIN_LIMIT (default 3) replies to the
         partner bot per "beat" (between human messages) — banter can flow
         and task handoffs ("NOW HAKARI! DECIDE!") still land, but chains
         always terminate (✅ react once the cap is hit).
       * under the cap: partner names THIS bot -> reply; otherwise ask the
         judge with recent conversation context — the judge may say YES so
         the bots chime in on each other naturally (e.g. the human praised
         the partner and this bot wants to tease), but is biased toward NO
         to avoid spam.
       * every "listened but not replying" skip adds a ✅ reaction to the
         partner's message, so the humans can see it was heard.
  2. Human messages (these reset the loop guard):
       * names THIS bot (BOT_SELF_NAME in text) -> ALWAYS reply.
       * names ONLY the other bot -> stay quiet (no reaction; the partner
         will answer, and this bot may still chime in on the partner's reply).
       * otherwise -> ask the judge "should {SELF} reply? YES/NO".
  3. If the judge errors / times out / is disabled -> fall back to a random
     roll (REPLY_CHANCE for humans, REACT_CHANCE for the partner bot).

Outbound speech filter: because the light judge/roleplay model sometimes
ignores negative SOUL rules (and channel-history backfill teaches it its own
old bad habits, e.g. "darling"), the plugin can also hard-rewrite outgoing
text. On first dispatch it wraps the Discord adapter's ``send`` and
``edit_message`` (streaming edits included) and applies the SPEECH_FILTER
rules to every outgoing message.

Env (per HERMES_HOME/.env):
  BOT_SELF_NAME, BOT_OTHER_NAME           — character names
  MOOD_JUDGE_ENABLED (default true)       — set false to use pure RNG
  MOOD_JUDGE_URL, MOOD_JUDGE_MODEL, MOOD_JUDGE_KEY, MOOD_JUDGE_TIMEOUT
  REPLY_CHANCE (0.6), REACT_CHANCE (0.25) — RNG fallback probabilities
  MOOD_SEEN_EMOJI (default ✅)            — reaction for "heard, not replying"
  SPEECH_FILTER                           — outbound rewrites, e.g.
                                            "darling=>Pizh;sweetheart=>Pizh"
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import tempfile
import time
import urllib.request
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

# Short rolling transcript of inbound messages (speaker, text) given to the
# judge as context, plus a counter capping how many times this bot replies to
# the partner bot between human messages (anti-loop). Module-level is fine:
# one gateway process per bot.
_recent: deque = deque(maxlen=6)
_bot_replies_since_human = 0
_filter_installed = False
_compact_roleplay_reply = False
_GENERAL_CLAIM_DIR = Path(tempfile.gettempdir()) / "hermes-mood-reply-claims"
_GENERAL_CLAIM_TTL_SECONDS = 6 * 60 * 60


def _chain_limit() -> int:
    try:
        return int(_env("BOT_CHAIN_LIMIT", "3"))
    except ValueError:
        return 3


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _chance(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _truthy(name: str, default: str = "true") -> bool:
    return _env(name, default).lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------- speech filter

def _filter_rules():
    """Parse SPEECH_FILTER ("bad=>good;worse=>better") into regex rules."""
    rules = []
    for part in _env("SPEECH_FILTER").split(";"):
        if "=>" not in part:
            continue
        bad, good = part.split("=>", 1)
        bad, good = bad.strip(), good.strip()
        if bad and good:
            rules.append((re.compile(rf"\b{re.escape(bad)}\b", re.IGNORECASE), good))
    return rules


def _is_no_reply(text) -> bool:
    """True when the model chose silence via the SOUL's NO_REPLY token."""
    return isinstance(text, str) and text.strip().strip("`*_ ").upper() == "NO_REPLY"


_SUBSTANTIVE_REQUEST_RE = re.compile(
    r"\b(what|when|where|who|why|how|can you|could you|would you|please|"
    r"look up|search|find|research|check|news|latest|set up|script|monitor|"
    r"cron|remind|help|decide|choose|explain)\b",
    re.IGNORECASE,
)
_TASK_HANDOFF_RE = re.compile(
    r"\b(look up|search|find|research|check|set up|script|monitor|cron|"
    r"remind|help|decide|choose|summari[sz]e|compare)\b",
    re.IGNORECASE,
)


def _is_substantive_request(text: str) -> bool:
    """True for questions/tasks, which need room for a real answer."""
    return "?" in text or bool(_SUBSTANTIVE_REQUEST_RE.search(text or ""))


def _is_task_handoff(text: str) -> bool:
    """Partner questions stay compact unless they actually hand off work."""
    return bool(_TASK_HANDOFF_RE.search(text or ""))


def _shorten_roleplay(text: str) -> str:
    """Keep a pure RP turn to one action beat plus one spoken beat."""
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    shortened = "\n\n".join(blocks[:2])
    if len(shortened) <= 420:
        return shortened
    clipped = shortened[:420].rsplit(" ", 1)[0].rstrip(" ,;:")
    return (clipped or shortened[:420]).rstrip() + "…"


def _fake_ok_result():
    try:
        from gateway.platforms.base import SendResult
        return SendResult(success=True)
    except Exception:
        return None


def _install_speech_filter(gateway) -> None:
    """Wrap the Discord adapter's send/edit_message on first dispatch.

    Two jobs: apply SPEECH_FILTER rewrites, and swallow messages whose entire
    content is the SOUL's NO_REPLY token (the token means "stay silent", but
    nothing upstream suppresses it, so it leaks as a literal chat message).
    Wrapping send/edit covers every outgoing message including streaming
    edits — unlike the transform_llm_output hook, which fires after streaming
    already delivered the text.
    """
    global _filter_installed
    if _filter_installed or gateway is None:
        return
    _filter_installed = True  # attempt once, even on failure
    rules = _filter_rules()

    def _clean(text):
        global _compact_roleplay_reply
        if not isinstance(text, str) or not text:
            return text
        for rx, repl in rules:
            text = rx.sub(repl, text)
        if _compact_roleplay_reply and not _BOT_NOISE_RE.search(text):
            text = _shorten_roleplay(text)
        return text

    try:
        for platform, adapter in (getattr(gateway, "adapters", {}) or {}).items():
            if getattr(platform, "value", "") != "discord":
                continue

            _orig_send = adapter.send

            async def _send(*args, _o=_orig_send, **kwargs):
                if "content" in kwargs:
                    if _is_no_reply(kwargs["content"]):
                        logger.info("mood_reply: suppressed NO_REPLY message")
                        return _fake_ok_result()
                    kwargs["content"] = _clean(kwargs["content"])
                elif len(args) >= 2:
                    if _is_no_reply(args[1]):
                        logger.info("mood_reply: suppressed NO_REPLY message")
                        return _fake_ok_result()
                    args = (*args[:1], _clean(args[1]), *args[2:])
                return await _o(*args, **kwargs)

            adapter.send = _send

            if hasattr(adapter, "edit_message"):
                _orig_edit = adapter.edit_message

                async def _edit(*args, _o=_orig_edit, **kwargs):
                    if "content" in kwargs:
                        kwargs["content"] = _clean(kwargs["content"])
                    elif len(args) >= 3:
                        args = (*args[:2], _clean(args[2]), *args[3:])
                    return await _o(*args, **kwargs)

                adapter.edit_message = _edit

            logger.info(
                "mood_reply: outbound wrapper active (NO_REPLY suppression + %d filter rule(s))",
                len(rules),
            )
    except Exception as exc:
        logger.warning("mood_reply: outbound wrapper install failed: %s", exc)


# ------------------------------------------------------------------- reactions

async def _do_react(raw, emoji: str) -> None:
    try:
        await raw.add_reaction(emoji)
    except Exception as exc:
        logger.debug("mood_reply: add_reaction failed: %s", exc)


def _react_seen(event) -> None:
    """React ✅ on a partner-bot message we heard but chose not to answer."""
    raw = getattr(event, "raw_message", None)
    if raw is None or not hasattr(raw, "add_reaction"):
        return
    emoji = _env("MOOD_SEEN_EMOJI", "✅")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_do_react(raw, emoji))


# ----------------------------------------------------------------------- judge

def _rng_decision(author_is_bot: bool):
    """Random fallback used when the judge is unavailable."""
    if author_is_bot:
        chance = _chance("REACT_CHANCE", "0.25")
    else:
        chance = _chance("REPLY_CHANCE", "0.6")
    if random.random() < chance:
        return {"action": "allow"}
    return {"action": "skip", "reason": "mood_reply: rng fallback (quiet)"}


def _ask_judge(self_name: str, other_name: str, author_is_bot: bool, convo: str):
    """Ask 9router whether SELF should reply. Returns True/False, or None on error."""
    url = _env("MOOD_JUDGE_URL", "http://127.0.0.1:20128/api/v1/chat/completions")
    model = _env("MOOD_JUDGE_MODEL", "emilia")
    key = _env("MOOD_JUDGE_KEY")
    try:
        timeout = float(_env("MOOD_JUDGE_TIMEOUT", "5"))
    except ValueError:
        timeout = 5.0

    self_c = (self_name or "this character").title()
    other_c = (other_name or "the other character").title()

    system = (
        f"You are a silent decision function for a Discord group roleplay chat. "
        f"Participants: the user (a human), and two AI characters — {self_c} and {other_c}. "
        f"Decide whether {self_c} should send a reply to the LATEST message right now, "
        f"so the conversation feels natural and not spammy. "
        f"Say YES if {self_c} is the natural one to respond (addressed to {self_c}, a question "
        f"{self_c} would answer, or a good moment to chime in). "
        f"Say NO if the message is really meant for {other_c}, if replying would be spammy, or to "
        f"leave room for the user. Lines like '{self_c} (me): [replied]' mean {self_c} already "
        f"spoke recently — use them to avoid {self_c} dominating the chat, but a direct question "
        f"from the human aimed at {self_c} or at the whole group should still be answered (YES) "
        f"even if {self_c} spoke recently. "
        f"If the latest message is from {other_c} (the other AI): {self_c} MAY chime in when it "
        f"feels natural — e.g. {other_c} just answered the user and {self_c} would tease or react "
        f"(especially if the user was praising/teasing {other_c}, or {other_c} mentioned {self_c}). "
        f"Still lean toward NO for {other_c}'s messages more often than YES, so the two AIs never "
        f"fall into an endless back-and-forth. Answer with ONLY one word: YES or NO."
    )
    user = (
        f"Recent messages (oldest first):\n{convo}\n"
        f"Should {self_c} reply to the LATEST message? Answer YES or NO."
    )

    payload = {
        "model": model,
        "stream": False,
        "temperature": 0,
        "max_tokens": 4,
        "reasoning_effort": "none",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:  # network / timeout / http error
        logger.warning("mood_reply judge call failed: %s", exc)
        return None

    # The combo endpoint may append a trailing SSE "data: [DONE]" marker.
    part = raw.split("data: [DONE]")[0].strip()
    try:
        d = json.loads(part)
        content = (d["choices"][0]["message"].get("content") or "").strip().lower()
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("mood_reply judge parse failed: %s (raw=%.120s)", exc, raw)
        return None

    if not content:
        return None
    if content.startswith("y") or "yes" in content[:6]:
        return True
    if content.startswith("n") or "no" in content[:6]:
        return False
    return None


# ------------------------------------------------------------------------ hook

# Partner-bot messages that are system notices / tool logs, not conversation:
# memory notifications ("💾 Self-improvement review: ..."), delegate banners,
# terminal/tool blocks, approval prompts, busy acks. RP-ing a reply to these
# looks absurd ("Ooh~ A system update! Good for them~"), so they are dropped
# silently — no ✅, no judge, not even recorded as context.
_BOT_NOISE_RE = re.compile(
    r"^\s*(?:[💾🔀💻🧠⚙📋⏳⌛✅❌⚠⚡🔄]|```)"
    r"|Self-improvement review"
    r"|Command Approval Required"
    r"|delegate_task:"
    r"|Interrupting current task"
    r"|Queued for the next turn",
    re.UNICODE,
)

# Human phrasings that address BOTH bots at once ("you two", "you and karane",
# ...). These must beat the "names only the other bot -> skip" fast path,
# otherwise "i want to ask you and karane" silences the unnamed bot.
_GROUP_ADDRESS_RE = re.compile(
    r"\b(you two|you both|both of you|you guys|you all|all of you|you girls|"
    r"girls|everyone|you and|and you)\b",
    re.IGNORECASE,
)
_EXPLICIT_BOTH_RE = re.compile(
    r"\b(you two|you both|both of you|all of you|you and|and you)\b",
    re.IGNORECASE,
)


def _mark_replied(self_name: str) -> None:
    _recent.append(f"{(self_name or 'me').title()} (me): [replied]")


def _claim_general_human_turn(event, self_name: str) -> bool:
    """Atomically choose one bot to answer an unaddressed human message.

    Karane and Hakari run in separate processes, so their in-memory mood state
    cannot coordinate a general question. A tiny O_EXCL claim file, keyed by
    the Discord message ID, gives the first bot whose judge says YES the turn.
    The other bot then waits for the winner's completed Discord message and
    may naturally chime in through the normal partner-message path.
    """
    raw = getattr(event, "raw_message", None)
    message_id = getattr(raw, "id", None)
    src = getattr(event, "source", None)
    chat_id = getattr(src, "chat_id", None)
    if not message_id or not chat_id:
        # This hook only coordinates real Discord messages. Do not make a
        # malformed/synthetic event silently lose its otherwise valid reply.
        return True

    safe_key = re.sub(r"[^0-9A-Za-z_.-]", "_", f"{chat_id}-{message_id}")
    claim_path = _GENERAL_CLAIM_DIR / f"{safe_key}.claim"
    try:
        _GENERAL_CLAIM_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            if time.time() - claim_path.stat().st_mtime > _GENERAL_CLAIM_TTL_SECONDS:
                claim_path.unlink()
        except FileNotFoundError:
            pass

        fd = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as claim_file:
            claim_file.write(f"{self_name or 'bot'} {time.time():.3f}\n")
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        # Preserve graceful degradation if /tmp is unavailable; this must
        # never prevent both bots from talking.
        logger.warning("mood_reply: general-turn claim failed: %s", exc)
        return True


def _ask_general_speaker(self_name: str, other_name: str, convo: str, *, must_answer: bool = False):
    """Ask 9router to pick exactly one speaker for a general human message."""
    url = _env("MOOD_JUDGE_URL", "http://127.0.0.1:20128/api/v1/chat/completions")
    model = _env("MOOD_JUDGE_MODEL", "emilia")
    key = _env("MOOD_JUDGE_KEY")
    try:
        timeout = float(_env("MOOD_JUDGE_TIMEOUT", "5"))
    except ValueError:
        timeout = 5.0
    self_c, other_c = (self_name or "karane").lower(), (other_name or "hakari").lower()
    none_rule = (
        "The latest message is a question/request, so you MUST choose one character; NEVER choose NONE. "
        if must_answer else
        "Choose NONE only when neither character should reply. "
    )
    system = (
        "You direct turn-taking in a Discord roleplay with one human and two AI "
        f"characters: {self_c.title()} and {other_c.title()}. For the LATEST "
        "unaddressed human message, choose one character to take the lead, or NONE. "
        "character may react only after the chosen character's final answer. Reply ONLY: "
        f"{self_c.upper()}, {other_c.upper()}, or NONE."
        "Use conversational fit and personality; avoid duplicate research. "
        f"{none_rule}The other character may react only after the chosen character's "
        f"final answer. Reply ONLY: {self_c.upper()}, {other_c.upper()}, or NONE."
        "character may react only after the chosen character's final answer. Reply ONLY: "
        f"{self_c.upper()}, {other_c.upper()}, or NONE."
    )
    payload = {
        "model": model, "stream": False, "temperature": 0, "max_tokens": 4,
        "reasoning_effort": "none",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Recent messages (oldest first):\n{convo}"},
        ],
    }
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        content = json.loads(raw.split("data: [DONE]")[0].strip())["choices"][0]["message"].get("content", "")
    except Exception as exc:
        logger.warning("mood_reply general speaker judge failed: %s", exc)
        return None
    choice = content.strip().lower()
    if self_c in choice:
        return self_c
    if other_c in choice:
        return other_c
    if "none" in choice or choice.startswith("no"):
        return "none"
    return None


def _select_general_human_speaker(event, self_name: str, other_name: str, convo: str):
    """Coordinate one 9router decision across both gateway processes."""
    raw = getattr(event, "raw_message", None)
    message_id = getattr(raw, "id", None)
    chat_id = getattr(getattr(event, "source", None), "chat_id", None)
    if not message_id or not chat_id:
        return None
    safe_key = re.sub(r"[^0-9A-Za-z_.-]", "_", f"{chat_id}-{message_id}")
    decision_path = _GENERAL_CLAIM_DIR / f"{safe_key}.speaker"

    if _claim_general_human_turn(event, self_name):
        must_answer = _is_substantive_request(getattr(event, "text", "") or "")
        choice = _ask_general_speaker(self_name, other_name, convo, must_answer=must_answer)
        if choice == "none" and must_answer:
            logger.warning("mood_reply: selector returned NONE for a question; assigning arbiter")
            choice = self_name
        if choice is None:
            verdict = _ask_judge(self_name, other_name, False, convo)
            if verdict is None:
                verdict = _rng_decision(False).get("action") == "allow"
            choice = self_name if verdict else "none"
        try:
            decision_path.write_text(choice, encoding="utf-8")
        except OSError as exc:
            logger.warning("mood_reply: could not publish speaker decision: %s", exc)
        return choice

    deadline = time.monotonic() + 6.0
    valid = {self_name, other_name, "none"}
    while time.monotonic() < deadline:
        try:
            choice = decision_path.read_text(encoding="utf-8").strip().lower()
            if choice in valid:
                return choice
        except OSError:
            pass
        time.sleep(0.05)
    logger.warning("mood_reply: timed out waiting for shared speaker decision")
    return None


def _on_pre_gateway_dispatch(event=None, gateway=None, **_):
    global _bot_replies_since_human, _compact_roleplay_reply

    _install_speech_filter(gateway)

    if event is None:
        return {"action": "allow"}
    src = getattr(event, "source", None)
    if src is None:
        return {"action": "allow"}

    platform = getattr(getattr(src, "platform", None), "value", "")
    chat_type = getattr(src, "chat_type", "") or ""
    # Only gate multi-party discord chatter; never gate DMs or other platforms.
    if platform != "discord" or chat_type == "dm":
        return {"action": "allow"}

    # Do not reset this for background/memory hooks; the outbound wrapper
    # still needs the style selected by the user-facing turn.
    _compact_roleplay_reply = False

    text = (getattr(event, "text", "") or "").strip()
    text_l = text.lower()
    self_name = _env("BOT_SELF_NAME").lower()
    other_name = _env("BOT_OTHER_NAME").lower()
    author_is_bot = bool(getattr(src, "is_bot", False))

    # Native /new and /reset are routed through the same event pipeline as
    # chat. They must bypass the mood selector or a "quiet" decision can
    # silently prevent the command dispatcher from ever seeing them.
    if not author_is_bot and text.startswith("/"):
        return {"action": "allow"}

    # Drop partner system notices / tool logs before anything else — they are
    # not conversation and must not reach the judge or the character.
    if author_is_bot and (not text or _BOT_NOISE_RE.search(text)):
        return {"action": "skip", "reason": "mood_reply: partner system notice/tool log"}

    speaker = (
        f"{(other_name or 'partner').title()} (the other AI)"
        if author_is_bot
        else f"{getattr(src, 'user_name', None) or 'user'} (human)"
    )
    _recent.append(f'{speaker}: "{text[:300]}"')

    if author_is_bot:
        # Loop guard: cap replies to the partner bot per "beat" (between
        # human messages) so banter can flow but never loops forever.
        if _bot_replies_since_human >= _chain_limit():
            _react_seen(event)
            return {"action": "skip", "reason": "mood_reply: bot chain limit reached; waiting for human"}

        # Fast path: the partner addresses me by name (e.g. "NOW HAKARI!
        # DECIDE!") -> answer. Task handoffs must not get stuck behind the
        # judge or an already-spent chime-in.
        if self_name and self_name in text_l:
            _bot_replies_since_human += 1
            _compact_roleplay_reply = not _is_task_handoff(text)
            _mark_replied(self_name)
            return {"action": "allow"}

        verdict = None
        if _truthy("MOOD_JUDGE_ENABLED", "true"):
            verdict = _ask_judge(self_name, other_name, True, "\n".join(_recent))
        if verdict is None:  # judge disabled/unavailable -> RNG
            verdict = _rng_decision(True).get("action") == "allow"

        if verdict:
            _bot_replies_since_human += 1
            _compact_roleplay_reply = not _is_task_handoff(text)
            _mark_replied(self_name)
            return {"action": "allow"}
        _react_seen(event)
        return {"action": "skip", "reason": "mood_reply: heard partner, judge said no"}

    # Human message: resets the bot-to-bot chain counter.
    _bot_replies_since_human = 0

    # Fast path: directly addressed by my own name -> always answer.
    if self_name and self_name in text_l:
        _compact_roleplay_reply = not _is_substantive_request(text)
        _mark_replied(self_name)
        return {"action": "allow"}

    # Fast path: the human addresses the whole group ("you two", "you guys",
    # "you and karane", ...) -> both bots answer.
    if _EXPLICIT_BOTH_RE.search(text):
        _compact_roleplay_reply = not _is_substantive_request(text)
        _mark_replied(self_name)
        return {"action": "allow"}

    # Fast path: a human names ONLY the other bot -> stay quiet and let the
    # partner answer (we may still chime in on the partner's reply above).
    if other_name and other_name in text_l and self_name not in text_l:
        return {"action": "skip", "reason": "mood_reply: addressed to other bot"}

    # One shared AI selector chooses the lead speaker. The partner waits for
    # the decision instead of launching the same task in parallel.
    choice = _select_general_human_speaker(event, self_name, other_name, "\n".join(_recent))
    if choice == self_name:
        _compact_roleplay_reply = not _is_substantive_request(text)
        _mark_replied(self_name)
        return {"action": "allow"}
    if choice in {other_name, "none"}:
        return {"action": "skip", "reason": "mood_reply: shared AI selected another speaker"}
    return {"action": "skip", "reason": "mood_reply: shared AI selector unavailable"}


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    logger.info(
        "mood_reply registered (self=%s other=%s judge=%s filter=%s)",
        _env("BOT_SELF_NAME"), _env("BOT_OTHER_NAME"),
        _truthy("MOOD_JUDGE_ENABLED"), bool(_filter_rules()),
    )
