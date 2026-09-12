#!/usr/bin/env python3
"""X.com 推文详情查询.

用法:
    # 通过推文 URL
    uv run read.py https://x.com/ScenarioOfElon/status/2062521510938779729

    # 通过推文 ID
    uv run read.py 2062521510938779729

    # 简洁输出（仅文本）
    uv run read.py <url/id> --brief
"""

import argparse
import json
import re
import sys
from pathlib import Path

from utils.session_manager import get_client, load_cookies_from_file, save_cookies_to_config

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config" / "settings.json"


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def extract_tweet_id(raw: str) -> str:
    m = re.search(r"/status/(\d+)", raw)
    if m:
        return m[1]
    if raw.isdigit():
        return raw
    print(f"[✗] 无法解析推文 ID: {raw}")
    print("[*] 格式: https://x.com/user/status/<id> 或纯数字 ID")
    sys.exit(1)


def fetch_tweet(client, tweet_id: str, cfg: dict) -> dict:
    query_id = cfg["api"].get("tweet_detail_query_id", "6uCvnic3m5reVuehkvHa3w")
    url = f"{cfg['api']['base_url']}/graphql/{query_id}/TweetDetail"

    variables = {
        "focalTweetId": tweet_id,
        "with_rux_injections": False,
        "rankingMode": "Relevance",
        "includePromotedContent": True,
        "withCommunity": True,
        "withQuickPromoteEligibilityTweetFields": True,
        "withBirdwatchNotes": True,
        "withVoice": True,
    }

    params = {
        "variables": json.dumps(variables),
        "features": json.dumps(cfg["features"]),
        "fieldToggles": json.dumps({
            "withArticleRichContentState": True,
            "withArticlePlainText": False,
            "withArticleSummaryText": True,
            "withArticleVoiceOver": True,
            "withGrokAnalyze": False,
            "withDisallowedReplyControls": False,
        }),
    }

    r = client.get(url, params=params)
    if r.status_code != 200:
        print(f"[✗] 请求失败: HTTP {r.status_code}")
        print(f"    响应: {r.text[:500]}")
        sys.exit(1)

    data = r.json()
    instructions = (
        data.get("data", {})
        .get("threaded_conversation_with_injections_v2", {})
        .get("instructions", [])
    )
    for instr in instructions:
        if instr.get("entries"):
            for entry in instr["entries"]:
                if entry.get("entryId", "").startswith("tweet-"):
                    result = (
                        entry.get("content", {})
                        .get("itemContent", {})
                        .get("tweet_results", {})
                        .get("result", {})
                    )
                    if result:
                        return result

    print("[✗] 未找到推文数据")
    sys.exit(1)


def format_tweet(result: dict, brief: bool = False) -> str:
    legacy = result.get("legacy", {})
    core = result.get("core", {}).get("user_results", {}).get("result", {})
    user_legacy = core.get("legacy", {})
    user_core = core.get("core", {})

    screen_name = user_core.get("screen_name", "?")
    display_name = user_core.get("name", "?")
    tweet_id = result.get("rest_id", "?")
    full_text = legacy.get("full_text", "")
    created_at = legacy.get("created_at", "")
    source = result.get("source", "")
    source_clean = re.sub(r"<[^>]+>", "", source) if source else ""
    views = result.get("views", {}).get("count", "?")

    if brief:
        return f"@{screen_name}: {full_text}"

    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"  @{screen_name}  ({display_name})")
    lines.append(f"{'='*60}")
    lines.append(f"  推文 ID: {tweet_id}")
    lines.append(f"  内容:")
    for line in full_text.split("\n"):
        lines.append(f"    {line}")
    lines.append(f"  时间: {created_at}")
    lines.append(f"  设备: {source_clean}")
    lines.append(f"{'='*60}")
    lines.append(f"  点赞: {legacy.get('favorite_count', 0):,}")
    lines.append(f"  回复: {legacy.get('reply_count', 0):,}")
    lines.append(f"  转发: {legacy.get('retweet_count', 0):,}")
    lines.append(f"  引用: {legacy.get('quote_count', 0):,}")
    lines.append(f"  书签: {legacy.get('bookmark_count', 0):,}")
    lines.append(f"  浏览: {views}")
    lines.append(f"{'='*60}")
    lines.append(f"  关注: {user_legacy.get('friends_count', 0):,}")
    lines.append(f"  粉丝: {user_legacy.get('followers_count', 0):,}")
    lines.append(f"  发帖: {user_legacy.get('statuses_count', 0):,}")
    lines.append(f"  认证: {'✓' if user_legacy.get('verified') or core.get('is_blue_verified') else '✗'}")
    lines.append(f"  简介: {user_legacy.get('description', '')}")

    # Media
    media_list = legacy.get("extended_entities", {}).get("media", [])
    if media_list:
        lines.append(f"{'='*60}")
        lines.append(f"  媒体 ({len(media_list)} 个):")
        for m in media_list:
            mtype = m.get("type", "unknown")
            if mtype == "photo":
                lines.append(f"    [图片] {m.get('media_url_https', '?')}")
            elif mtype == "video":
                lines.append(f"    [视频] {m.get('expanded_url', '?')}")
            elif mtype == "animated_gif":
                lines.append(f"    [GIF] {m.get('expanded_url', '?')}")

    # Quoted tweet
    quoted = result.get("quoted_status_result", {}).get("result")
    if quoted:
        q_legacy = quoted.get("legacy", {})
        q_user = quoted.get("core", {}).get("user_results", {}).get("result", {})
        q_screen_name = (
            q_user.get("core", {}).get("screen_name")
            or q_user.get("legacy", {}).get("screen_name")
            or "?"
        )
        lines.append(f"{'='*60}")
        lines.append(f"  [引用推文] @{q_screen_name}")
        lines.append(f"    内容: {q_legacy.get('full_text', '')[:120]}...")

    # Community note
    note = result.get("birdwatch_pivot", {})
    if note:
        lines.append(f"{'='*60}")
        lines.append(f"  [社区笔记] {note}")

    lines.append(f"{'='*60}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="X.com 推文详情查询")
    parser.add_argument("tweet", help="推文 URL 或 ID")
    parser.add_argument("--brief", "-b", action="store_true", help="简洁输出")
    parser.add_argument("--json", "-j", action="store_true", help="输出原始 JSON")
    parser.add_argument("--cookies", help="cookie 文件路径")
    parser.add_argument("--auth-token", help="auth_token cookie")
    parser.add_argument("--ct0", help="ct0 cookie")
    parser.add_argument("--save-cookies", action="store_true", help="持久化 cookie")
    args = parser.parse_args()

    tweet_id = extract_tweet_id(args.tweet)

    auth_token = args.auth_token
    ct0 = args.ct0
    if args.cookies:
        token, csrf = load_cookies_from_file(args.cookies)
        auth_token = auth_token or token
        ct0 = ct0 or csrf

    try:
        client = get_client(auth_token=auth_token, ct0=ct0)
    except ValueError as e:
        print(f"[✗] {e}")
        sys.exit(1)

    if args.save_cookies and auth_token and ct0:
        save_cookies_to_config(auth_token, ct0)

    cfg = load_config()
    result = fetch_tweet(client, tweet_id, cfg)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_tweet(result, brief=args.brief))


if __name__ == "__main__":
    main()
