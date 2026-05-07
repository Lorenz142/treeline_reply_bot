import os
import json
import time
import hmac
import hashlib
import base64
import urllib.parse
import threading
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify

# ── Config ──
X_CONSUMER_KEY = os.environ.get("X_CONSUMER_KEY")
X_CONSUMER_SECRET = os.environ.get("X_CONSUMER_SECRET")
X_ACCESS_TOKEN = os.environ.get("X_ACCESS_TOKEN")
X_ACCESS_SECRET = os.environ.get("X_ACCESS_SECRET")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
LIST_ID = os.environ.get("X_LIST_ID", "1778779740348072233")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "3600"))  # seconds

# ── Draft storage ──
drafts = {}
draft_counter = 0
seen_tweets = set()

# ── System prompt for reply generation ──
SYSTEM_PROMPT = """You are ghostwriting X (Twitter) replies for Lorenz, who runs @TreeLineTrade — a systematic crypto trading brand called Treeline Trading.

Voice and style:
- Direct, confident, no fluff
- Casual but knowledgeable — like a sharp friend who actually trades
- Short sentences. Punchy. No corporate speak.
- Uses "lol", "tbh", "ngl" naturally but not excessively
- Never uses hashtags in replies
- Never sounds like a bot or AI — no "Great point!", no "I completely agree!", no "This is so true!"
- Occasionally contrarian — willing to push back respectfully
- References systematic/quantitative trading naturally when relevant
- Avoids shilling or self-promotion unless it fits organically

Reply guidelines:
- Keep replies under 200 characters when possible, max 280
- Add genuine value — a insight, a question, a different angle
- Match the energy of the original post
- If the post is serious, be thoughtful. If it's casual, be casual.
- Never reply with just agreement — add something new
- If you can't add genuine value, say SKIP and nothing else

You will receive a tweet and must write a single reply. Nothing else — no explanation, no options, just the reply text. Or SKIP if you can't add value."""

# ── OAuth 1.0a signing ──
def oauth_sign(method, url, params=None):
    if params is None:
        params = {}

    oauth_params = {
        "oauth_consumer_key": X_CONSUMER_KEY,
        "oauth_nonce": base64.b64encode(os.urandom(32)).decode("utf-8").strip("=+/"),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": X_ACCESS_TOKEN,
        "oauth_version": "1.0",
    }

    all_params = {**oauth_params, **params}
    sorted_params = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted(all_params.items())
    )

    base_string = f"{method.upper()}&{urllib.parse.quote(url, safe='')}&{urllib.parse.quote(sorted_params, safe='')}"
    signing_key = f"{urllib.parse.quote(X_CONSUMER_SECRET, safe='')}&{urllib.parse.quote(X_ACCESS_SECRET, safe='')}"

    signature = base64.b64encode(
        hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
    ).decode()

    oauth_params["oauth_signature"] = signature
    auth_header = "OAuth " + ", ".join(
        f'{k}="{urllib.parse.quote(v, safe="")}"' for k, v in sorted(oauth_params.items())
    )

    return {"Authorization": auth_header}


# ── X API calls ──
def get_list_tweets():
    """Fetch recent tweets from the Reply Hunt list."""
    url = f"https://api.x.com/2/lists/{LIST_ID}/tweets"
    params = {
        "max_results": "20",
        "tweet.fields": "author_id,created_at,text,conversation_id",
        "expansions": "author_id",
        "user.fields": "username,name",
    }

    headers = oauth_sign("GET", url, params)
    resp = requests.get(url, headers=headers, params=params)

    if resp.status_code != 200:
        print(f"X API error {resp.status_code}: {resp.text}")
        return []

    data = resp.json()
    tweets = data.get("data", [])
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}

    results = []
    for tweet in tweets:
        author = users.get(tweet["author_id"], {})
        results.append({
            "id": tweet["id"],
            "text": tweet["text"],
            "author_username": author.get("username", "unknown"),
            "author_name": author.get("name", "Unknown"),
            "created_at": tweet.get("created_at", ""),
            "conversation_id": tweet.get("conversation_id", tweet["id"]),
        })

    return results


def post_reply(tweet_id, reply_text):
    """Post a reply to a tweet on X using v1.1 endpoint."""
    url = "https://api.x.com/1.1/statuses/update.json"
    params = {
        "status": reply_text,
        "in_reply_to_status_id": tweet_id,
        "auto_populate_reply_metadata": "true",
    }

    headers = oauth_sign("POST", url, params)
    resp = requests.post(url, headers=headers, data=params)

    if resp.status_code in (200, 201):
        print(f"Reply posted to tweet {tweet_id}")
        return True
    else:
        print(f"Failed to post reply: {resp.status_code} {resp.text}")
        return False


# ── Claude API ──
def generate_reply(tweet_text, author_username):
    """Use Claude to generate a reply draft."""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 300,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": f"@{author_username} posted:\n\n\"{tweet_text}\"\n\nWrite a reply.",
                }
            ],
        },
    )

    if resp.status_code != 200:
        print(f"Claude API error: {resp.status_code} {resp.text}")
        return None

    data = resp.json()
    reply = data["content"][0]["text"].strip()

    if reply.upper() == "SKIP":
        return None

    return reply


# ── Discord ──
def send_draft_to_discord(draft_id, tweet, reply_text):
    """Send a draft reply to Discord for approval."""
    tweet_url = f"https://x.com/{tweet['author_username']}/status/{tweet['id']}"

    embed = {
        "title": f"Draft #{draft_id:03d}",
        "color": 0x1DA1F2,
        "fields": [
            {
                "name": f"@{tweet['author_username']}",
                "value": tweet["text"][:500],
                "inline": False,
            },
            {
                "name": "Your reply",
                "value": f"**{reply_text}**",
                "inline": False,
            },
            {
                "name": "Approve",
                "value": f"Type `{draft_id}` to post  •  `no {draft_id}` to skip",
                "inline": False,
            },
        ],
        "footer": {"text": f"Tweet link: {tweet_url}"},
    }

    payload = {"embeds": [embed]}
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload)
    if resp.status_code not in (200, 204):
        print(f"Discord webhook error: {resp.status_code} {resp.text}")


def listen_for_approvals():
    """Poll Discord channel for approval messages."""
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    last_message_id = None

    while True:
        try:
            url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages?limit=10"
            if last_message_id:
                url += f"&after={last_message_id}"

            resp = requests.get(url, headers=headers)
            if resp.status_code != 200:
                print(f"Discord poll error: {resp.status_code}")
                time.sleep(5)
                continue

            messages = resp.json()
            if not messages:
                time.sleep(3)
                continue

            # Update last seen message
            latest_id = max(int(m["id"]) for m in messages)
            if last_message_id is None or latest_id > int(last_message_id):
                last_message_id = str(latest_id)

            for msg in messages:
                # Skip bot messages and webhook messages
                if msg.get("author", {}).get("bot", False):
                    continue
                if msg.get("webhook_id"):
                    continue

                content = msg["content"].strip().lower()

                # Check for approval: just a number
                if content.isdigit():
                    draft_id = int(content)
                    if draft_id in drafts:
                        draft = drafts[draft_id]
                        success = post_reply(draft["tweet_id"], draft["reply_text"])
                        if success:
                            confirm_msg = f"✅ Draft #{draft_id:03d} posted!"
                            del drafts[draft_id]
                        else:
                            confirm_msg = f"❌ Draft #{draft_id:03d} failed to post."
                        # Send confirmation
                        requests.post(
                            f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
                            headers={**headers, "Content-Type": "application/json"},
                            json={"content": confirm_msg},
                        )

                # Check for rejection: "no X" or "skip X"
                elif content.startswith(("no ", "skip ")):
                    parts = content.split()
                    if len(parts) == 2 and parts[1].isdigit():
                        draft_id = int(parts[1])
                        if draft_id in drafts:
                            del drafts[draft_id]
                            requests.post(
                                f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
                                headers={**headers, "Content-Type": "application/json"},
                                json={"content": f"⏭️ Draft #{draft_id:03d} skipped."},
                            )

        except Exception as e:
            print(f"Approval listener error: {e}")

        time.sleep(3)


# ── Main scan loop ──
def scan_loop():
    """Main loop: fetch tweets, generate replies, send drafts."""
    global draft_counter

    while True:
        try:
            print(f"[{datetime.now(timezone.utc).isoformat()}] Scanning Reply Hunt list...")
            tweets = get_list_tweets()
            print(f"Found {len(tweets)} tweets")

            new_count = 0
            for tweet in tweets:
                # Skip already seen tweets
                if tweet["id"] in seen_tweets:
                    continue
                seen_tweets.add(tweet["id"])

                # Skip retweets
                if tweet["text"].startswith("RT @"):
                    continue

                # Generate reply
                reply = generate_reply(tweet["text"], tweet["author_username"])
                if reply is None:
                    print(f"Skipped tweet by @{tweet['author_username']}")
                    continue

                # Store draft
                draft_counter += 1
                draft_id = draft_counter
                drafts[draft_id] = {
                    "tweet_id": tweet["id"],
                    "reply_text": reply,
                    "tweet": tweet,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }

                # Send to Discord
                send_draft_to_discord(draft_id, tweet, reply)
                new_count += 1
                print(f"Draft #{draft_id:03d} created for @{tweet['author_username']}")

                # Small delay between Claude calls
                time.sleep(2)

            print(f"Created {new_count} new drafts. Total pending: {len(drafts)}")

        except Exception as e:
            print(f"Scan error: {e}")

        # Wait for next scan
        time.sleep(SCAN_INTERVAL)


# ── Flask app for health check ──
app = Flask(__name__)


@app.route("/")
def health():
    return jsonify({
        "status": "running",
        "pending_drafts": len(drafts),
        "seen_tweets": len(seen_tweets),
        "last_draft_id": draft_counter,
    })


@app.route("/scan")
def manual_scan():
    """Trigger a manual scan."""
    threading.Thread(target=lambda: scan_loop_once(), daemon=True).start()
    return jsonify({"status": "scan triggered"})


def scan_loop_once():
    """Run one scan cycle."""
    global draft_counter
    try:
        tweets = get_list_tweets()
        for tweet in tweets:
            if tweet["id"] in seen_tweets:
                continue
            seen_tweets.add(tweet["id"])
            if tweet["text"].startswith("RT @"):
                continue
            reply = generate_reply(tweet["text"], tweet["author_username"])
            if reply is None:
                continue
            draft_counter += 1
            draft_id = draft_counter
            drafts[draft_id] = {
                "tweet_id": tweet["id"],
                "reply_text": reply,
                "tweet": tweet,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            send_draft_to_discord(draft_id, tweet, reply)
            time.sleep(2)
    except Exception as e:
        print(f"Manual scan error: {e}")


if __name__ == "__main__":
    # Start scan loop in background thread
    scan_thread = threading.Thread(target=scan_loop, daemon=True)
    scan_thread.start()

    # Start approval listener in background thread
    approval_thread = threading.Thread(target=listen_for_approvals, daemon=True)
    approval_thread.start()

    # Run Flask
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
