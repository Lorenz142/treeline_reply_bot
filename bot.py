import os
import json
import time
import hashlib
import base64
import secrets
import threading
from datetime import datetime, timezone, timedelta

import tweepy
import requests
from flask import Flask, jsonify, redirect, request as flask_request

# ── Config ──
X_CONSUMER_KEY = os.environ.get("X_CONSUMER_KEY")
X_CONSUMER_SECRET = os.environ.get("X_CONSUMER_SECRET")
X_ACCESS_TOKEN = os.environ.get("X_ACCESS_TOKEN")
X_ACCESS_SECRET = os.environ.get("X_ACCESS_SECRET")
X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
LIST_ID = os.environ.get("X_LIST_ID", "1778779740348072233")
SCAN_INTERVAL = 3600  # 1 hour
MAX_DRAFTS_PER_SCAN = 5

# OAuth 2.0 PKCE tokens for posting
OAUTH2_CLIENT_ID = os.environ.get("OAUTH2_CLIENT_ID")
OAUTH2_CLIENT_SECRET = os.environ.get("OAUTH2_CLIENT_SECRET")
oauth2_access_token = os.environ.get("OAUTH2_ACCESS_TOKEN")
oauth2_refresh_token = os.environ.get("OAUTH2_REFRESH_TOKEN")

# Schedule: 7am Edmonton (MDT/UTC-6) to 12:15am Edmonton
# 7am MDT = 13:00 UTC, 12:15am MDT = 06:15 UTC
SCAN_START_UTC = (13, 0)   # 13:00 UTC = 7am MDT
SCAN_END_UTC = (6, 15)     # 06:15 UTC = 12:15am MDT

# ── Tweepy client for READING (bearer token) ──
read_client = tweepy.Client(
    bearer_token=X_BEARER_TOKEN,
    consumer_key=X_CONSUMER_KEY,
    consumer_secret=X_CONSUMER_SECRET,
    access_token=X_ACCESS_TOKEN,
    access_token_secret=X_ACCESS_SECRET,
)

# ── Draft storage ──
drafts = {}
draft_counter = 0
seen_tweets = set()

# ── System prompt ──
SYSTEM_PROMPT = """You are ghostwriting X (Twitter) replies for Lorenz, who runs @TreeLineTrade — a crypto trading account.

THE RULES:

1. MAX 15 WORDS. Seriously. Count them. If it's over 15, cut it down. The best replies are 5-10 words.

2. NEVER explain anything. No "because", no "when X happens Y follows", no second sentences that elaborate. One thought. That's it.

3. NEVER mention systematic trading, algos, strategies, or backtesting unless the original tweet is specifically about those topics.

4. Vary your style. Rotate between these:
   - Funny/relatable reaction ("my portfolio felt this")
   - Spicy disagreement ("nah this ain't it")
   - Asking a genuine question that makes people want to answer
   - Finishing their thought with a punchline
   - One-word or two-word reactions when appropriate ("pain.", "every time lol")

5. Write like you're texting a friend, not writing a blog post. Lowercase is fine. Fragments are fine. 

6. SKIP aggressively. If you can't write something that would make someone smirk, think, or want to reply — say SKIP. Better to skip than be mid.

EXAMPLES OF GOOD REPLIES:
- "this is the top signal lol"
- "the chart warned us tbh"
- "how many times we gotta learn this lesson"
- "name one person who actually sold the top"
- "pain"
- "bullish on people finally getting this"
- "wait til they find out about the fees"

EXAMPLES OF BAD REPLIES (never do this):
- "This is actually pretty bullish for the space longer term. When traditional growth money flows in, tokens usually follow."
- "Weekend markets hit different when you're running systematic strategies. No news flow but the algos keep grinding."
- "Most guys figure this out after blowing up their first account chasing 100x returns. The market teaches you quick."

Those are bad because they're too long, too complete, too smart-sounding, and they explain instead of react.

You will receive a tweet. Write ONE short reply. Nothing else. Or SKIP."""

# ── OAuth 2.0 token refresh ──
def refresh_oauth2_token():
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
            print("OAuth2 token refreshed")
            return True
        else:
            print(f"Token refresh failed: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        print(f"Token refresh error: {e}")
        return False


# ── Schedule check ──
def is_within_schedule():
    """Check if current time is between 7am and 12:15am Edmonton time."""
    now = datetime.now(timezone.utc)
    current = now.hour * 60 + now.minute  # minutes since midnight UTC
    start = SCAN_START_UTC[0] * 60 + SCAN_START_UTC[1]  # 13:00 = 780
    end = SCAN_END_UTC[0] * 60 + SCAN_END_UTC[1]        # 06:15 = 375
    # Window wraps past midnight UTC
    if start > end:
        return current >= start or current < end
    else:
        return start <= current < end


# ── X API helpers ──
def _x_post(endpoint, payload):
    return requests.post(
        f"https://api.x.com/2/{endpoint}",
        headers={
            "Authorization": f"Bearer {oauth2_access_token}",
            "Content-Type": "application/json",
        },
        json=payload,
    )


def _x_delete(endpoint):
    return requests.delete(
        f"https://api.x.com/2/{endpoint}",
        headers={"Authorization": f"Bearer {oauth2_access_token}"},
    )


# ── X API calls ──
def get_list_tweets():
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
            author = users.get(tweet.author_id)
            results.append({
                "id": str(tweet.id),
                "text": tweet.text,
                "author_username": author.username if author else "unknown",
                "author_name": author.name if author else "Unknown",
                "created_at": str(tweet.created_at) if tweet.created_at else "",
            })
        return results
    except Exception as e:
        print(f"X API error fetching list: {e}")
        return []


def post_quote_tweet(tweet_id, quote_text, author_username=None):
    payload = {
        "text": quote_text,
        "quote_tweet_id": str(tweet_id),
    }
    for attempt in range(2):
        resp = _x_post("tweets", payload)
        if resp.status_code in (200, 201):
            print(f"Quote tweet posted for {tweet_id}: {resp.json()}")
            return True
        elif resp.status_code == 401:
            print(f"Unauthorized — refreshing token (attempt {attempt + 1})")
            if refresh_oauth2_token():
                continue
            else:
                return False
        else:
            print(f"Failed to post quote tweet: {resp.status_code} {resp.text}")
            return False
    return False


def test_post():
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


# ── Claude API ──
def generate_reply(tweet_text, author_username):
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
    tweet_url = f"https://x.com/{tweet['author_username']}/status/{tweet['id']}"

    embed = {
        "title": f"Draft #{draft_id:03d} — @{tweet['author_username']}",
        "url": tweet_url,
        "color": 0x1DA1F2,
        "fields": [
            {
                "name": "Original tweet",
                "value": tweet["text"][:500],
                "inline": False,
            },
            {
                "name": "Your quote tweet",
                "value": f"```{reply_text}```",
                "inline": False,
            },
            {
                "name": "Approve",
                "value": f"Type `{draft_id}` to post  •  `no {draft_id}` to skip",
                "inline": False,
            },
        ],
        "footer": {"text": "Treeline Trading"},
    }

    payload = {"embeds": [embed]}
    if ping:
        payload["content"] = "<@605286827682693161> New drafts ready for review!"

    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload)
    if resp.status_code not in (200, 204):
        print(f"Discord webhook error: {resp.status_code} {resp.text}")

    # Send quote text as separate message for easy mobile copy
    requests.post(DISCORD_WEBHOOK_URL, json={"content": reply_text})


def listen_for_approvals():
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    last_message_id = None

    while True:
        try:
            url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages?limit=10"
            if last_message_id:
                url += f"&after={last_message_id}"

            resp = requests.get(url, headers=headers)
            if resp.status_code != 200:
                time.sleep(5)
                continue

            messages = resp.json()
            if not messages:
                time.sleep(3)
                continue

            latest_id = max(int(m["id"]) for m in messages)
            if last_message_id is None or latest_id > int(last_message_id):
                last_message_id = str(latest_id)

            for msg in messages:
                if msg.get("author", {}).get("bot", False):
                    continue
                if msg.get("webhook_id"):
                    continue

                content = msg["content"].strip().lower()

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
                        requests.post(
                            f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
                            headers={**headers, "Content-Type": "application/json"},
                            json={"content": confirm_msg},
                        )

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


# ── Scan logic ──
def run_scan():
    global draft_counter

    tweets = get_list_tweets()
    print(f"Found {len(tweets)} tweets")

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

        is_last = (i == len(eligible) - 1)
        send_draft_to_discord(draft_id, tweet, reply, ping=is_last)
        new_count += 1
        print(f"Draft #{draft_id:03d} created for @{tweet['author_username']}")
        time.sleep(2)

    print(f"Created {new_count} new drafts. Total pending: {len(drafts)}")


def scan_loop():
    while True:
        try:
            now = datetime.now(timezone.utc)
            if is_within_schedule():
                print(f"[{now.isoformat()}] Scanning Reply Hunt list...")
                run_scan()
            else:
                print(f"[{now.isoformat()}] Outside schedule (8am EST — 8pm MT). Sleeping.")
        except Exception as e:
            print(f"Scan error: {e}")

        time.sleep(SCAN_INTERVAL)


# ── Flask app ──
app = Flask(__name__)
auth_state = {}


@app.route("/")
def health():
    return jsonify({
        "status": "running",
        "pending_drafts": len(drafts),
        "seen_tweets": len(seen_tweets),
        "last_draft_id": draft_counter,
        "in_schedule": is_within_schedule(),
    })


@app.route("/scan")
def manual_scan():
    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"status": "scan triggered"})


@app.route("/test_post")
def test_post_route():
    result = test_post()
    return jsonify({"can_post": result})


@app.route("/auth")
def oauth2_authorize():
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
    global oauth2_access_token, oauth2_refresh_token

    code = flask_request.args.get("code")
    state = flask_request.args.get("state")

    if state != auth_state.get("state"):
        return "Invalid state parameter", 400

    resp = requests.post(
        "https://api.x.com/2/oauth2/token",
        auth=(OAUTH2_CLIENT_ID, OAUTH2_CLIENT_SECRET),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://treelinereplybot-production.up.railway.app/callback",
            "code_verifier": auth_state.get("code_verifier"),
        },
    )

    if resp.status_code == 200:
        data = resp.json()
        oauth2_access_token = data["access_token"]
        oauth2_refresh_token = data.get("refresh_token", oauth2_refresh_token)
        print(f"New OAuth2 tokens obtained!")
        return "<h1>Authorized!</h1><p>Bot is now connected to @TreeLineTrade. You can close this tab.</p>"
    else:
        print(f"Token exchange failed: {resp.status_code} {resp.text}")
        return f"Authorization failed: {resp.text}", 400


if __name__ == "__main__":
    scan_thread = threading.Thread(target=scan_loop, daemon=True)
    scan_thread.start()

    approval_thread = threading.Thread(target=listen_for_approvals, daemon=True)
    approval_thread.start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
