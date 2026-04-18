#!/usr/bin/env python3
"""
INV-09 CAPITAL — Slack ↔ Anthropic Managed Agent Bridge
=========================================================
Bidirectional bridge: messages in #inv-capital are forwarded to the
INV-09 CAPITAL agent via the Anthropic Sessions API. Agent responses
are posted back to the same Slack thread.

Architecture:
  Slack #inv-capital  ──►  Bridge (Socket Mode)  ──►  Anthropic Sessions API
       ◄─────────────────────────────────────────────────┘

Requirements:
  pip install slack_bolt anthropic python-dotenv

Usage:
  1. Copy .env.bridge.example to .env.bridge and fill in values
  2. python slack-bridge.py
"""

import os
import re
import time
import logging
import threading
from pathlib import Path

import anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# Load .env.bridge if present
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / ".env.bridge"
    if env_path.exists():
        load_dotenv(env_path)
        print(f"Loaded config from {env_path}")
except ImportError:
    pass

# Configuration

def require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val

# Anthropic
ANTHROPIC_API_KEY = require_env("ANTHROPIC_API_KEY")
AGENT_ID = os.environ.get("INV09_AGENT_ID", "agent_011CaBMMNDABYvMWYpwJUQuk")
ENVIRONMENT_ID = os.environ.get("INV09_ENVIRONMENT_ID", "env_01GSksEBrA6qNi25bFzQcorf")
VAULT_ID = os.environ.get("INV09_VAULT_ID", "vlt_011CaBakhrVieQCmak5oX7MC")
CREDENTIALS_FILE_ID = os.environ.get("INV09_CREDENTIALS_FILE_ID", "file_011CaBdsoCWXTY1dtNWAF6Ur")

# Slack
SLACK_BOT_TOKEN = require_env("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = require_env("SLACK_APP_TOKEN")
BOT_USER_ID = os.environ.get("SLACK_BOT_USER_ID", "U0AT2RJSDBR")

# Bridge settings
MAX_POLL_ATTEMPTS = 60
POLL_INTERVAL_SECS = 3
SESSION_TTL_SECS = 3600
MAX_RESPONSE_LENGTH = 3900

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("inv09-bridge")

# Anthropic Client
ant = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Session Management
_sessions = {}
_session_lock = threading.Lock()


def get_or_create_session(thread_key: str) -> str:
    with _session_lock:
        entry = _sessions.get(thread_key)
        now = time.time()
        if entry and (now - entry["created_at"]) < SESSION_TTL_SECS:
            log.info(f"Reusing session {entry['session_id']} for thread {thread_key}")
            return entry["session_id"]
        log.info(f"Creating new INV-09 session for thread {thread_key}...")
        session = ant.beta.sessions.create(
            agent=AGENT_ID,
            environment_id=ENVIRONMENT_ID,
            vault_ids=[VAULT_ID],
            resources=[
                {
                    "type": "file",
                    "file_id": CREDENTIALS_FILE_ID,
                    "mount_path": "/workspace/.env",
                }
            ],
        )
        _sessions[thread_key] = {"session_id": session.id, "created_at": now}
        log.info(f"Session created: {session.id}")
        return session.id


def send_and_wait(session_id: str, message: str) -> str:
    log.info(f"Sending message to session {session_id}: {message[:80]}...")
    ant.beta.sessions.events.send(
        session_id=session_id,
        events=[
            {"type": "user.message", "content": [{"type": "text", "text": message}]}
        ],
    )
    for attempt in range(MAX_POLL_ATTEMPTS):
        time.sleep(POLL_INTERVAL_SECS)
        sess = ant.beta.sessions.retrieve(session_id=session_id)
        status = sess.status
        log.info(f"  Poll {attempt+1}/{MAX_POLL_ATTEMPTS}: status={status}")
        if status in ("ready", "completed", "ended"):
            break
        if status in ("failed", "error"):
            return f"[Error: session ended with status '{status}']"
    else:
        return "[Error: agent timed out after 3 minutes]"
    events = list(ant.beta.sessions.events.list(session_id=session_id, limit=100))
    assistant_text = ""
    for ev in events:
        ev_type = getattr(ev, "type", None)
        if ev_type in ("agent.message", "assistant.message"):
            content = getattr(ev, "content", None)
            if isinstance(content, str):
                assistant_text = content
            elif isinstance(content, list):
                for block in content:
                    if getattr(block, "type", None) == "text":
                        assistant_text = block.text
    if not assistant_text:
        return "[Agent processed the request but returned no text response]"
    return assistant_text


# Slack App
app = App(token=SLACK_BOT_TOKEN)


def split_message(text: str, max_len: int = MAX_RESPONSE_LENGTH) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def format_response(text: str) -> str:
    text = re.sub(r"^#{1,3}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    return text


@app.event("message")
def handle_message(event, say, client):
    pass


def _process_agent_request(event, say, client):
    user = event.get("user", "")
    bot_id = event.get("bot_id", "")
    subtype = event.get("subtype", "")
    if bot_id or user == BOT_USER_ID or subtype in ("bot_message", "message_changed"):
        return
    text = event.get("text", "").strip()
    if not text:
        return
    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts")
    ts = event.get("ts")
    log.info(f"Agent request from {user} in {channel}: {text[:80]}...")
    try:
        client.reactions_add(channel=channel, name="hourglass_flowing_sand", timestamp=ts)
    except Exception:
        pass

    def process():
        try:
            session_id = get_or_create_session(thread_ts)
            response = send_and_wait(session_id, text)
            response = format_response(response)
            chunks = split_message(response)
            for i, chunk in enumerate(chunks):
                say(text=chunk, thread_ts=thread_ts, unfurl_links=False, unfurl_media=False)
                if i < len(chunks) - 1:
                    time.sleep(0.5)
            try:
                client.reactions_remove(channel=channel, name="hourglass_flowing_sand", timestamp=ts)
                client.reactions_add(channel=channel, name="white_check_mark", timestamp=ts)
            except Exception:
                pass
        except Exception as e:
            log.error(f"Error processing message: {e}", exc_info=True)
            say(text=f"[Bridge error: {str(e)[:200]}]", thread_ts=thread_ts)
            try:
                client.reactions_remove(channel=channel, name="hourglass_flowing_sand", timestamp=ts)
                client.reactions_add(channel=channel, name="x", timestamp=ts)
            except Exception:
                pass

    threading.Thread(target=process, daemon=True).start()


@app.event("app_mention")
def handle_mention(event, say, client):
    text = re.sub(r"<@[A-Z0-9]+>\s*", "", event.get("text", "")).strip()
    if text:
        event["text"] = text
        _process_agent_request(event, say, client)


if __name__ == "__main__":
    log.info("=" * 60)
    log.info("INV-09 CAPITAL — Slack Bridge")
    log.info(f"Agent:       {AGENT_ID}")
    log.info(f"Environment: {ENVIRONMENT_ID}")
    log.info(f"Vault:       {VAULT_ID}")
    log.info(f"Bot User:    {BOT_USER_ID}")
    log.info("=" * 60)
    log.info("Starting Socket Mode connection...")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
