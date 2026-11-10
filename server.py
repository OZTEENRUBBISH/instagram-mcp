"""
Instagram MCP Server

Exposes Instagram Graph API (publishing, insights, comments, DMs) as MCP tools.
Built using the mcp-server-builder skill workflow.

Requirements:
- Instagram Business or Creator account linked to a Facebook Page
- Meta Developer app with instagram_basic, instagram_content_publish,
  instagram_manage_comments, instagram_manage_messages, pages_show_list,
  pages_read_engagement permissions
- Long-lived Page access token (60-day, refreshable)

Setup:
    uv init instagram-mcp && cd instagram-mcp
    uv add "fastmcp>=3.0" httpx python-dotenv

Create a .env file in the same directory with:
    META_ACCESS_TOKEN=EAAxxxxxxxxxxxxxxxxxx
    IG_USER_ID=178414xxxxxxxxxx
    PAGE_ID=104xxxxxxxxxxxxx        # optional, only needed for DMs

Test:
    npx @modelcontextprotocol/inspector uv run server.py
"""

import os
import sys
import time
from pathlib import Path
from typing import Annotated, Literal

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from pydantic import Field

# Load .env from the same directory as this script
load_dotenv(Path(__file__).parent / ".env")

ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN")
IG_USER_ID = os.environ.get("IG_USER_ID")
# ACCOUNT_NAME labels this instance so Claude knows which account it's talking to.
# Set it per-instance: "Dr. Ashraf (Personal)", "Surgicare ICU", "Pushpam", "Anita AI"
ACCOUNT_NAME = os.environ.get("ACCOUNT_NAME", "Instagram")

# API_FLOW selects which Instagram API surface to use:
#   "instagram_login" (default) — graph.instagram.com, IGAA token, simple setup, subset of features
#   "facebook_login"            — graph.facebook.com, EAA token, requires linked FB Page,
#                                 unlocks likes, delete_post, events, ads, full hashtag search
API_FLOW = os.environ.get("API_FLOW", "instagram_login")
PAGE_ID = os.environ.get("PAGE_ID")  # required only for facebook_login DM tools

if not ACCESS_TOKEN:
    print("FATAL: META_ACCESS_TOKEN env var required. Create a .env file.", file=sys.stderr)
    sys.exit(1)
if not IG_USER_ID:
    print("FATAL: IG_USER_ID env var required (your IG Business account ID).", file=sys.stderr)
    sys.exit(1)

if API_FLOW == "facebook_login":
    GRAPH = "https://graph.facebook.com/v21.0"
    BASE = f"/{IG_USER_ID}"               # account addressed by numeric ID
    DM_BASE = f"/{PAGE_ID}" if PAGE_ID else f"/{IG_USER_ID}"  # DMs run through the linked Page
else:
    GRAPH = "https://graph.instagram.com/v21.0"
    BASE = "/me"                          # account addressed by /me
    DM_BASE = "/me"

# Hardened: the MCP endpoint lives at a secret path (MCP_SECRET), so only someone who
# knows the full URL can call it. Only publishing tools are exposed.
# MCP server name = account name so Claude's tool picker shows the right label
mcp = FastMCP(ACCOUNT_NAME)


# ────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ────────────────────────────────────────────────────────────────────────────

def _client() -> httpx.Client:
    """Build an HTTP client. Access token is passed per-request as a query param
    because that's what the Graph API expects."""
    return httpx.Client(timeout=60.0)


def _check(resp: httpx.Response, context: str) -> dict:
    """Raise a helpful error if the Graph API call failed, else return JSON."""
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error", {})
            msg = err.get("message", resp.text[:300])
            code = err.get("code", resp.status_code)
            sub = err.get("error_subcode", "")
            raise ValueError(
                f"{context} failed (code {code}, subcode {sub}): {msg}. "
                f"Common causes: expired token, missing permission, or container not ready."
            )
        except ValueError:
            raise
        except Exception:
            raise ValueError(f"{context} failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def _wait_for_container(client: httpx.Client, container_id: str, max_wait_s: int = 120) -> None:
    """Poll a media container until it's FINISHED or fail. IG requires this for video/reel."""
    start = time.time()
    while time.time() - start < max_wait_s:
        resp = client.get(
            f"{GRAPH}/{container_id}",
            params={"fields": "status_code,status", "access_token": ACCESS_TOKEN},
        )
        data = _check(resp, "Container status check")
        status = data.get("status_code", "")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise ValueError(f"Container processing failed: {data.get('status', 'no detail')}")
        time.sleep(3)
    raise ValueError(f"Container {container_id} not ready after {max_wait_s}s. Try again or check media URL.")


# ────────────────────────────────────────────────────────────────────────────
# Tools — Account & Insights
# ────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_account_info() -> dict:
    """Fetch basic info about the connected Instagram Business account.

    Use this first to verify the token and IG user ID are valid. Returns username,
    follower count, media count, and account type.
    """
    with _client() as client:
        resp = client.get(
            f"{GRAPH}{BASE}",
            params={
                "fields": "username,name,biography,followers_count,follows_count,media_count,profile_picture_url",
                "access_token": ACCESS_TOKEN,
            },
        )
        return _check(resp, "get_account_info")


@mcp.tool()
def publish_photo(
    image_url: Annotated[str, Field(description="PUBLIC https URL of the image. Must be JPEG, < 8MB, accessible without auth.")],
    caption: Annotated[str, Field(default="", max_length=2200, description="Caption with hashtags. Max 30 hashtags.")] = "",
) -> dict:
    """Publish a single photo to the Instagram feed.

    THIS IS A WRITE OPERATION. The image URL must be publicly accessible — Instagram fetches
    it from your URL. Local file paths won't work; upload to S3/Cloudinary/imgur first.
    Returns the published post ID and permalink.
    """
    with _client() as client:
        # Step 1: create container
        resp = client.post(
            f"{GRAPH}{BASE}/media",
            params={
                "image_url": image_url,
                "caption": caption,
                "access_token": ACCESS_TOKEN,
            },
        )
        container = _check(resp, "publish_photo (create container)")
        container_id = container["id"]

        # Photos don't usually need polling, but check briefly
        _wait_for_container(client, container_id, max_wait_s=30)

        # Step 2: publish
        resp = client.post(
            f"{GRAPH}{BASE}/media_publish",
            params={"creation_id": container_id, "access_token": ACCESS_TOKEN},
        )
        published = _check(resp, "publish_photo (publish)")
        post_id = published["id"]

        # Fetch permalink
        resp = client.get(
            f"{GRAPH}/{post_id}",
            params={"fields": "permalink", "access_token": ACCESS_TOKEN},
        )
        info = _check(resp, "publish_photo (get permalink)")
        return {"post_id": post_id, "permalink": info.get("permalink", "")}


@mcp.tool()
def publish_reel(
    video_url: Annotated[str, Field(description="PUBLIC https URL of an MP4 video. Must be < 1GB, < 90 seconds, accessible without auth.")],
    caption: Annotated[str, Field(default="", max_length=2200, description="Caption with hashtags")] = "",
    share_to_feed: Annotated[bool, Field(default=True, description="Also show in main feed grid")] = True,
) -> dict:
    """Publish a Reel to Instagram.

    THIS IS A WRITE OPERATION. Video URL must be publicly accessible. Reel processing takes
    30-60s on Instagram's side — this tool waits. Returns post ID and permalink.
    """
    with _client() as client:
        # Step 1: create reel container
        resp = client.post(
            f"{GRAPH}{BASE}/media",
            params={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "share_to_feed": share_to_feed,
                "access_token": ACCESS_TOKEN,
            },
        )
        container = _check(resp, "publish_reel (create container)")
        container_id = container["id"]

        # Reels need polling — video processing
        _wait_for_container(client, container_id, max_wait_s=180)

        # Step 2: publish
        resp = client.post(
            f"{GRAPH}{BASE}/media_publish",
            params={"creation_id": container_id, "access_token": ACCESS_TOKEN},
        )
        published = _check(resp, "publish_reel (publish)")
        post_id = published["id"]

        resp = client.get(
            f"{GRAPH}/{post_id}",
            params={"fields": "permalink", "access_token": ACCESS_TOKEN},
        )
        info = _check(resp, "publish_reel (get permalink)")
        return {"post_id": post_id, "permalink": info.get("permalink", "")}


@mcp.tool()
def publish_carousel(
    image_urls: Annotated[list[str], Field(min_length=2, max_length=10, description="2-10 PUBLIC image URLs. Must all be accessible JPEGs.")],
    caption: Annotated[str, Field(default="", max_length=2200, description="Caption applied to the whole carousel")] = "",
) -> dict:
    """Publish a carousel (multi-image) post. Accepts 2-10 images.

    THIS IS A WRITE OPERATION. Each image must be publicly accessible. Creates a child
    container per image, then a carousel container, then publishes. Slow — expect 30-60s.
    """
    with _client() as client:
        # Step 1: create child containers
        children: list[str] = []
        for url in image_urls:
            resp = client.post(
                f"{GRAPH}{BASE}/media",
                params={
                    "image_url": url,
                    "is_carousel_item": True,
                    "access_token": ACCESS_TOKEN,
                },
            )
            child = _check(resp, f"publish_carousel (create child for {url[:50]})")
            children.append(child["id"])

        # Step 2: create carousel container
        resp = client.post(
            f"{GRAPH}{BASE}/media",
            params={
                "media_type": "CAROUSEL",
                "children": ",".join(children),
                "caption": caption,
                "access_token": ACCESS_TOKEN,
            },
        )
        container = _check(resp, "publish_carousel (create carousel container)")
        container_id = container["id"]

        _wait_for_container(client, container_id, max_wait_s=60)

        # Step 3: publish
        resp = client.post(
            f"{GRAPH}{BASE}/media_publish",
            params={"creation_id": container_id, "access_token": ACCESS_TOKEN},
        )
        published = _check(resp, "publish_carousel (publish)")
        post_id = published["id"]

        resp = client.get(
            f"{GRAPH}/{post_id}",
            params={"fields": "permalink", "access_token": ACCESS_TOKEN},
        )
        info = _check(resp, "publish_carousel (get permalink)")
        return {"post_id": post_id, "permalink": info.get("permalink", ""), "image_count": len(image_urls)}


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "http":
        secret = os.environ.get("MCP_SECRET", "")
        if len(secret) < 24 or not secret.isalnum():
            print("FATAL: MCP_SECRET env var required (24+ letters and digits only).", file=sys.stderr)
            sys.exit(1)
        port = int(os.environ.get("PORT", 8000))
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port, path=f"/{secret}/mcp")
    else:
        mcp.run()
