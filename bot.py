import os
import json
import time
import hashlib
import base64
import secrets
import threading
from datetime import datetime, timezone

import tweepy
import requests
from flask import Flask, jsonify, redirect, request as flask_request

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

X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN")

# OAuth 2.0 PKCE tokens for posting replies
OAUTH2_CLIENT_ID = os.environ.get("OAUTH2_CLIENT_ID")
OAUTH2_CLIENT_SECRET = os.environ.get("OAUTH2_CLIENT_SECRET")
oauth2_access_token = os.environ.get("OAUTH2_ACCESS_TOKEN")
oauth2_refresh_token = os.environ.get("OAUTH2_REFRESH_TOKEN")

# ── Tweepy client for READING (bearer token) ──
read_client = tweepy.Client(
    bearer_token=X_BEARER_TOKEN,
    consumer_key=X_CONSUMER_KEY,
    consumer_secret=X_CONSUMER_SECRET,
    access_token=X_ACCESS_TOKEN,
    access_token_secret=X_ACCESS_SECRET,
)

# ── No tweepy for writing — use direct API with OAuth 2.0 token ──


def refresh_oauth2_token():
    """Refresh the OAuth 2.0 access token using the refresh token."""
    global oauth2_access_token, oauth2_refresh_token

    try:
        resp = requests.post(
            "https://api.x.com/2/oauth2/token",
            auth=(OAUTH2_CLIENT_ID, OAUTH2_CLIENT_SECRET),
            data={
                "grant_type": "refresh_token",
                "refresh_token": oauth2_refresh_token,
            },
        )

        if resp.status_code == 200:
            data = resp.json()
            oauth2_access_token = data["access_token"]
            oauth2_refresh_token = data.get("refresh_token", oauth2_refresh_token)
            print(f"OAuth2 token refreshed successfully")
            return True
        else:
            print(f"Token refresh failed: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        print(f"Token refresh error: {e}")
        return False

# ── Draft storage ──
drafts = {}
draft_counter = 0
seen_tweets = set()

# ── System prompt for reply generation ──
SYSTEM_PROMPT = """You are ghostwriting X (Twitter) quote tweets for Lorenz, who runs @TreeLineTrade — a systematic crypto trading brand called Treeline Trading.

Voice and style:
- Direct, confident, no fluff
- Casual but knowledgeable — like a sharp friend who actually trades
- Short sentences. Punchy. No corporate speak.
- Uses "lol", "tbh", "ngl" naturally but not excessively
- Never uses hashtags
- Never sounds like a bot or AI — no "Great point!", no "I completely agree!", no "This is so true!"
- Occasionally contrarian — willing to push back respectfully
- References systematic/quantitative trading naturally when relevant
- Avoids shilling or self-promotion unless it fits organically

Quote tweet guidelines:
- Keep under 200 characters when possible, max 280
- Add genuine value — an insight, a question, a different angle, your take
- Match the energy of the original post
- If the post is serious, be thoughtful. If it's casual, be casual.
- Never just agree — add something new. Your followers should learn something or think differently.
- The original tweet will be embedded below your text, so don't repeat what it says
- If you can't add genuine value, say SKIP and nothing else

You will receive a tweet and must write a single quote tweet. Nothing else — no explanation, no options, just the text. Or SKIP if you can't add value."""


# ── X API calls ──
def get_list_tweets():
    """Fetch recent tweets from the Reply Hunt list."""
    try:
        response = read_client.get_list_tweets(
            id=LIST_ID,
            max_results=20,
            tweet_fields=["author_id", "created_at", "text", "conversation_id", "reply_settings"],
            expansions=["author_id"],
            user_fields=["username", "name"],
        )

        if not response.data:
            print("No tweets found in list")
            return []

        users = {}
        if response.includes and "users" in response.includes:
            users = {u.id: u for u in response.includes["users"]}

        results = []
        for tweet in response.data:
            # Skip tweets with restricted replies
            reply_settings = tweet.reply_settings if hasattr(tweet, "reply_settings") else "everyone"
            if reply_settings != "everyone":
                author = users.get(tweet.author_id)
                username = author.username if author else "unknown"
                print(f"Skipping @{username} tweet — replies restricted to: {reply_settings}")
                continue

            author = users.get(tweet.author_id)
            results.append({
                "id": str(tweet.id),
                "text": tweet.text,
                "author_username": author.username if author else "unknown",
                "author_name": author.name if author else "Unknown",
                "created_at": str(tweet.created_at) if tweet.created_at else "",
                "conversation_id": str(tweet.conversation_id) if tweet.conversation_id else str(tweet.id),
            })

        return results

    except Exception as e:
        print(f"X API error fetching list: {e}")
        return []


def _x_post(endpoint, payload):
    """Make authenticated POST request to X API v2 using OAuth 2.0 user token."""
    resp = requests.post(
        f"https://api.x.com/2/{endpoint}",
        headers={
            "Authorization": f"Bearer {oauth2_access_token}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    return resp


def _x_delete(endpoint):
    """Make authenticated DELETE request to X API v2 using OAuth 2.0 user token."""
    resp = requests.delete(
        f"https://api.x.com/2/{endpoint}",
        headers={
            "Authorization": f"Bearer {oauth2_access_token}",
        },
    )
    return resp


def post_quote_tweet(tweet_id, quote_text, author_username=None):
    """Post a quote tweet on X using OAuth 2.0."""
    tweet_url = f"https://x.com/{author_username}/status/{tweet_id}"
    payload = {
        "text": quote_text,
        "quote_tweet_id": str(tweet_id),
    }

    for attempt in range(2):
        resp = _x_post("tweets", payload)

        if resp.status_code in (200, 201):
            print(f"Quote tweet posted for tweet {tweet_id}: {resp.json()}")
            return True
        elif resp.status_code == 401:
            print(f"Unauthorized — refreshing OAuth2 token (attempt {attempt + 1})")
            if refresh_oauth2_token():
                continue
            else:
                print("Token refresh failed")
                return False
        else:
            print(f"Failed to post quote tweet: {resp.status_code} {resp.text}")
            return False
    return False


def test_post():
    """Test if we can post a standalone tweet."""
    try:
        resp = _x_post("tweets", {"text": "test — ignore"})
        if resp.status_code in (200, 201):
            tweet_id = resp.json()["data"]["id"]
            print(f"Test tweet posted: {tweet_id}")
            _x_delete(f"tweets/{tweet_id}")
            print("Test tweet deleted")
            return True
        else:
            print(f"Test post failed: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        print(f"Test post failed: {e}")
        return False


def test_reply():
    """Test if we can reply to our own tweet."""
    try:
        # Post a tweet
        resp = _x_post("tweets", {"text": "test thread — ignore"})
        if resp.status_code not in (200, 201):
            print(f"Test tweet failed: {resp.status_code} {resp.text}")
            return {"can_reply_self": False, "error": resp.text}

        tweet_id = resp.json()["data"]["id"]
        print(f"Test tweet posted: {tweet_id}")

        # Try to reply to it
        reply_resp = _x_post("tweets", {
            "text": "test reply — ignore",
            "reply": {"in_reply_to_tweet_id": tweet_id},
        })

        if reply_resp.status_code in (200, 201):
            reply_id = reply_resp.json()["data"]["id"]
            print(f"Test reply posted: {reply_id}")
            _x_delete(f"tweets/{reply_id}")
            _x_delete(f"tweets/{tweet_id}")
            print("Test tweets deleted")
            return {"can_reply_self": True}
        else:
            print(f"Test reply failed: {reply_resp.status_code} {reply_resp.text}")
            _x_delete(f"tweets/{tweet_id}")
            return {"can_reply_self": False, "error": reply_resp.text}
    except Exception as e:
        print(f"Test reply failed: {e}")
        return {"can_reply_self": False, "error": str(e)}


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
                    "content": f"@{author_username} posted:\n\n\"{tweet_text}\"\n\nWrite a quote tweet.",
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
def send_draft_to_discord(draft_id, tweet, reply_text, ping=False):
    """Send a draft quote tweet to Discord for approval."""
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
                "name": "Your quote tweet",
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
    if ping:
        payload["content"] = "@everyone New drafts ready for review!"

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
                        author_username = draft.get("tweet", {}).get("author_username")
                        success = post_quote_tweet(draft["tweet_id"], draft["reply_text"], author_username)
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
    MAX_DRAFTS_PER_SCAN = 5

    while True:
        try:
            print(f"[{datetime.now(timezone.utc).isoformat()}] Scanning Reply Hunt list...")
            tweets = get_list_tweets()
            print(f"Found {len(tweets)} tweets")

            # Collect eligible tweets first
            eligible = []
            for tweet in tweets:
                if tweet["id"] in seen_tweets:
                    continue
                seen_tweets.add(tweet["id"])
                if tweet["text"].startswith("RT @"):
                    continue
                eligible.append(tweet)
                if len(eligible) >= MAX_DRAFTS_PER_SCAN:
                    break

            new_count = 0
            for i, tweet in enumerate(eligible):
                reply = generate_reply(tweet["text"], tweet["author_username"])
                if reply is None:
                    print(f"Skipped tweet by @{tweet['author_username']}")
                    continue

                draft_counter += 1
                draft_id = draft_counter
                drafts[draft_id] = {
                    "tweet_id": tweet["id"],
                    "reply_text": reply,
                    "tweet": tweet,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }

                # Only ping on the last draft of the batch
                is_last = (i == len(eligible) - 1)
                send_draft_to_discord(draft_id, tweet, reply, ping=is_last)
                new_count += 1
                print(f"Draft #{draft_id:03d} created for @{tweet['author_username']}")

                time.sleep(2)

            print(f"Created {new_count} new drafts. Total pending: {len(drafts)}")

        except Exception as e:
            print(f"Scan error: {e}")

        # Wait for next scan
        time.sleep(SCAN_INTERVAL)


def scan_loop_once():
    """Run one scan cycle."""
    global draft_counter
    MAX_DRAFTS_PER_SCAN = 5
    try:
        tweets = get_list_tweets()
        eligible = []
        for tweet in tweets:
            if tweet["id"] in seen_tweets:
                continue
            seen_tweets.add(tweet["id"])
            if tweet["text"].startswith("RT @"):
                continue
            eligible.append(tweet)
            if len(eligible) >= MAX_DRAFTS_PER_SCAN:
                break

        for i, tweet in enumerate(eligible):
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
            is_last = (i == len(eligible) - 1)
            send_draft_to_discord(draft_id, tweet, reply, ping=is_last)
            time.sleep(2)
            time.sleep(2)
    except Exception as e:
        print(f"Manual scan error: {e}")


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


@app.route("/test_post")
def test_post_route():
    """Test if we can post to X at all."""
    result = test_post()
    return jsonify({"can_post": result})


@app.route("/test_reply")
def test_reply_route():
    """Test if we can reply to our own tweet."""
    result = test_reply()
    return jsonify(result)


# ── OAuth 2.0 PKCE auth flow ──
auth_state = {}


@app.route("/auth")
def oauth2_authorize():
    """Start OAuth 2.0 PKCE authorization flow."""
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    state = secrets.token_urlsafe(32)
    auth_state["code_verifier"] = code_verifier
    auth_state["state"] = state

    auth_url = (
        "https://x.com/i/oauth2/authorize"
        f"?response_type=code"
        f"&client_id={OAUTH2_CLIENT_ID}"
        f"&redirect_uri=https://treelinereplybot-production.up.railway.app/callback"
        f"&scope=tweet.read%20tweet.write%20users.read%20like.write%20offline.access"
        f"&state={state}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
    )

    return redirect(auth_url)


@app.route("/callback")
def oauth2_callback():
    """Handle OAuth 2.0 callback and exchange code for tokens."""
    global oauth2_access_token, oauth2_refresh_token

    code = flask_request.args.get("code")
    state = flask_request.args.get("state")

    if state != auth_state.get("state"):
        return "Invalid state parameter", 400

    code_verifier = auth_state.get("code_verifier")

    resp = requests.post(
        "https://api.x.com/2/oauth2/token",
        auth=(OAUTH2_CLIENT_ID, OAUTH2_CLIENT_SECRET),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://treelinereplybot-production.up.railway.app/callback",
            "code_verifier": code_verifier,
        },
    )

    if resp.status_code == 200:
        data = resp.json()
        oauth2_access_token = data["access_token"]
        oauth2_refresh_token = data.get("refresh_token", oauth2_refresh_token)
        print(f"New OAuth2 tokens obtained!")
        print(f"Access token: {oauth2_access_token[:20]}...")
        print(f"Refresh token: {oauth2_refresh_token[:20]}...")
        return "<h1>Authorized!</h1><p>Bot is now connected to @TreeLineTrade. You can close this tab.</p>"
    else:
        print(f"Token exchange failed: {resp.status_code} {resp.text}")
        return f"Authorization failed: {resp.text}", 400


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
