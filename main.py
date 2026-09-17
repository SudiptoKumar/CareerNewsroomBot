import os
import re
import json
import time
import html
import argparse
import logging
import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, urljoin, quote
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from io import BytesIO

import requests
import feedparser
import trafilatura
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageFile
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from exa_py import Exa
from cerebras.cloud.sdk import Cerebras


# ============================================================
# CONFIGURATION
# ============================================================

EXA_API_KEY = os.environ["EXA_API_KEY"]
CEREBRAS_API_KEY = os.environ["CEREBRAS_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

TELEGRAM_CHANNEL = (os.environ.get("TELEGRAM_CHANNEL") or "@CareerNewsroom").strip()

# Optional. If set, feed-down alerts go here (a private chat/DM with the
# bot, not the public channel). If empty, alerts only go to the run log.
TELEGRAM_ADMIN_CHAT_ID = (os.environ.get("TELEGRAM_ADMIN_CHAT_ID") or "").strip()

NEWS_MODE = (os.environ.get("NEWS_MODE") or "update").strip().lower()

VALID_NEWS_MODES = {"update"}
if NEWS_MODE not in VALID_NEWS_MODES:
    raise ValueError(
        f"Invalid NEWS_MODE={NEWS_MODE!r}; expected one of {sorted(VALID_NEWS_MODES)}"
    )

CEREBRAS_MODEL = os.environ.get(
    "CEREBRAS_MODEL",
    "gpt-oss-120b",
)

POSTED_FILE = "posted_urls.txt"
STATE_FILE = "news_state.json"

BD_TZ = ZoneInfo("Asia/Dhaka")

# Version 1 editorial target: publish only clearly important tech stories.
# All tech stories compete in one ranked pool. Six is a safety cap, not a quota.
MAX_STORIES_PER_RUN = 6
RANKING_POOL_SIZE = 24
DISCOVERY_LOOKBACK_HOURS = 72

# Reliability / quality
POST_DELAY_SECONDS = 3.5
ROLLING_DISCOVERY_HOURS = DISCOVERY_LOOKBACK_HOURS
FUTURE_TOLERANCE_MINUTES = 10
QUEUE_RETENTION_DAYS = 4
EVENT_RETENTION_DAYS = 30
MAX_RSS_CANDIDATES = 240
MAX_EXA_CANDIDATES = 60
MAX_GOOGLE_NEWS_CANDIDATES = 40
THIN_EXCERPT_CHARS = 150
MAX_EXCERPT_ENRICH = 12
MAX_SOURCE_PER_RUN = 99
MAX_RICH_CHARACTERS = 32768

# Lightweight English stopwords used only by the conservative event/entity
# deduplication layer. This is deliberately small so technology entities and
# meaningful short terms such as AI, OS, UI, and VR are retained.
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for",
    "from", "with", "by", "at", "as", "is", "are", "was", "were",
    "be", "been", "being", "has", "have", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "can",
    "this", "that", "these", "those", "it", "its", "their", "they",
    "them", "he", "she", "his", "her", "we", "our", "you", "your",
    "new", "after", "before", "over", "into", "than", "about", "from",
}

# RSS-first sources. Exa remains a fallback/gap filler.
RSS_FEEDS = [
    {"name": "TechCrunch", "region": "Tech", "url": "https://techcrunch.com/feed/"},
    {"name": "The Verge", "region": "Tech", "url": "https://www.theverge.com/rss/index.xml"},
    {"name": "WIRED", "region": "Tech", "url": "https://www.wired.com/feed/rss"},
    {"name": "Ars Technica", "region": "Tech", "url": "https://feeds.arstechnica.com/arstechnica/index"},
    {"name": "Engadget", "region": "Tech", "url": "https://www.engadget.com/rss.xml"},
    {"name": "MIT Technology Review", "region": "Tech", "url": "https://www.technologyreview.com/feed/"},
    {"name": "Hacker News", "region": "Tech", "url": "https://news.ycombinator.com/rss"},
    {"name": "VentureBeat", "region": "Tech", "url": "https://venturebeat.com/feed/"},
    {"name": "Techmeme", "region": "Tech", "url": "https://www.techmeme.com/feed.xml"},
    {"name": "TechRadar", "region": "Tech", "url": "https://www.techradar.com/feeds/articletype/news"},
    {"name": "ZDNET", "region": "Tech", "url": "https://www.zdnet.com/news/rss.xml"},
    {"name": "9to5Google", "region": "Tech", "url": "https://9to5google.com/feed/"},
    {"name": "WABetaInfo", "region": "Tech", "url": "https://wabetainfo.com/feed/"},
    {"name": "TestingCatalog", "region": "Tech", "url": "https://www.testingcatalog.com/feed/"},
    {"name": "AI News", "region": "Tech", "url": "https://www.artificialintelligence-news.com/feed/"},
    {"name": "Unite.AI", "region": "Tech", "url": "https://unite.ai/feed/"},
    {"name": "The Decoder", "region": "Tech", "url": "https://the-decoder.com/feed/"},
    {"name": "SiliconANGLE", "region": "Tech", "url": "https://siliconangle.com/feed/"},
    {"name": "Android Authority", "region": "Tech", "url": "https://www.androidauthority.com/feed/"},
    {"name": "MacRumors", "region": "Tech", "url": "https://www.macrumors.com/macrumors.xml"},
]




# ============================================================
# TAXONOMY: TECH NEWS

TOPICS = {
    "Tech": [
        "Consumer Technology",
        "AI Models and Products",
        "Smartphones",
        "Operating Systems",
        "Browsers",
        "Search",
        "Social Platforms",
        "Cloud Platforms",
        "App Stores",
        "Cybersecurity",
        "Privacy",
        "Major Tech Companies",
        "New Products",
        "Technology Industry",
        "Open Source",
        "GitHub Trends",
        "Startups",
        "Y Combinator",
        "Hugging Face",
        "Major Outages",
        "Acquisitions and Mergers",
        "Layoffs and Restructuring",
        "Pricing and Subscriptions",
    ]
}

INSTITUTIONS = [
    "Apple", "Google", "Microsoft", "OpenAI", "Meta", "Amazon", "Anthropic",
    "NVIDIA", "Samsung", "Qualcomm", "Intel", "AMD", "ByteDance", "TikTok",
    "X", "Tesla", "Cloudflare", "GitHub", "GitLab", "Hugging Face", "Y Combinator",
]

SOURCE_NAMES = {
    "techcrunch.com": "TechCrunch",
    "theverge.com": "The Verge",
    "wired.com": "WIRED",
    "arstechnica.com": "Ars Technica",
    "engadget.com": "Engadget",
    "technologyreview.com": "MIT Technology Review",
    "news.ycombinator.com": "Hacker News",
    "venturebeat.com": "VentureBeat",
    "techmeme.com": "Techmeme",
    "techradar.com": "TechRadar",
    "zdnet.com": "ZDNET",
    "9to5google.com": "9to5Google",
    "wabetainfo.com": "WABetaInfo",
    "testingcatalog.com": "TestingCatalog",
    "artificialintelligence-news.com": "AI News",
    "unite.ai": "Unite.AI",
    "the-decoder.com": "The Decoder",
    "siliconangle.com": "SiliconANGLE",
    "androidauthority.com": "Android Authority",
    "macrumors.com": "MacRumors",
}

CATEGORY_HASHTAGS = {
    "Consumer Technology": ["#Tech", "#ConsumerTech"],
    "AI Models and Products": ["#AI", "#ArtificialIntelligence"],
    "Smartphones": ["#Smartphones", "#MobileTech"],
    "Operating Systems": ["#OperatingSystems", "#Tech"],
    "Browsers": ["#Browsers", "#Internet"],
    "Search": ["#Search", "#Tech"],
    "Social Platforms": ["#SocialMedia", "#Tech"],
    "Cloud Platforms": ["#Cloud", "#Tech"],
    "App Stores": ["#AppStores", "#Tech"],
    "Cybersecurity": ["#Cybersecurity", "#Security"],
    "Privacy": ["#Privacy", "#Tech"],
    "Major Tech Companies": ["#BigTech", "#Tech"],
    "New Products": ["#Tech", "#NewProduct"],
    "Technology Industry": ["#TechIndustry", "#Tech"],
    "Open Source": ["#OpenSource", "#Tech"],
    "GitHub Trends": ["#GitHub", "#OpenSource"],
    "Startups": ["#Startups", "#Tech"],
    "Y Combinator": ["#YC", "#Startups"],
    "Hugging Face": ["#HuggingFace", "#AI"],
    "Major Outages": ["#Tech", "#Outage"],
    "Acquisitions and Mergers": ["#TechIndustry", "#Mergers"],
    "Layoffs and Restructuring": ["#TechIndustry", "#Layoffs"],
    "Pricing and Subscriptions": ["#Tech", "#Subscriptions"],
}

CATEGORY_GROUPS = {
    "AI": {"AI Models and Products", "Hugging Face"},
    "Platforms": {"Smartphones", "Operating Systems", "Browsers", "Search", "Social Platforms", "Cloud Platforms", "App Stores", "Major Outages"},
    "Security": {"Cybersecurity", "Privacy"},
    "Industry": {"Major Tech Companies", "Technology Industry", "Acquisitions and Mergers", "Layoffs and Restructuring", "Startups", "Y Combinator"},
    "Products and Ecosystem": {"Consumer Technology", "New Products", "Pricing and Subscriptions", "Open Source", "GitHub Trends"},
}

TOPIC_ALIASES = {
    "ai": "AI Models and Products",
    "artificial intelligence": "AI Models and Products",
    "smartphone": "Smartphones",
    "phones": "Smartphones",
    "android": "Operating Systems",
    "ios": "Operating Systems",
    "windows": "Operating Systems",
    "linux": "Operating Systems",
    "browser": "Browsers",
    "search": "Search",
    "social": "Social Platforms",
    "cloud": "Cloud Platforms",
    "app store": "App Stores",
    "security": "Cybersecurity",
    "privacy": "Privacy",
    "outage": "Major Outages",
    "github": "GitHub Trends",
    "open source": "Open Source",
    "startup": "Startups",
    "yc": "Y Combinator",
    "hugging face": "Hugging Face",
    "acquisition": "Acquisitions and Mergers",
    "merger": "Acquisitions and Mergers",
    "layoffs": "Layoffs and Restructuring",
    "subscription": "Pricing and Subscriptions",
}



def coverage_state():
    return STATE.setdefault("category_coverage", {})


def update_category_coverage(story):
    topic = safe_text(story.get("topic"))
    if topic:
        coverage_state()[topic] = now_iso()


def refresh_category_coverage():
    coverage = coverage_state()
    for event in STATE.get("events", {}).values():
        if event.get("status") != "published":
            continue
        published_at = parse_datetime(event.get("published_at"))
        if not published_at or published_at.date() != NOW_BD.date():
            continue
        topic = safe_text(event.get("topic"))
        if topic:
            coverage[topic] = event.get("selected_at", published_at.isoformat())


# ============================================================
# LOGGING + HTTP
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("career-news-bot")

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 50_000_000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    )
}

session = requests.Session()
session.headers.update(HEADERS)

retry_policy = Retry(
    total=4,
    connect=4,
    read=4,
    backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
    respect_retry_after_header=True,
)

adapter = HTTPAdapter(
    max_retries=retry_policy,
    pool_connections=20,
    pool_maxsize=20,
)

session.mount("https://", adapter)
session.mount("http://", adapter)


# ============================================================
# HELPERS
# ============================================================

def safe_text(value):
    return "" if value is None else str(value).strip()


def canonical_url(url):
    raw = safe_text(url)
    if not raw:
        return ""

    parsed = urlparse(raw)

    host = (
        parsed.netloc.lower()
        .removeprefix("www.")
        .removeprefix("amp.")
    )

    path = parsed.path or "/"
    path = path.rstrip("/")
    path = re.sub(r"/amp$", "", path, flags=re.I)
    path = re.sub(r"\.amp$", "", path, flags=re.I)

    return f"{host}{path}"


def normalize_title(title):
    text = safe_text(title).lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_tokens(text):
    text = normalize_title(text)
    return {
        token
        for token in text.split()
        if len(token) >= 3
    }


def token_jaccard(a, b):
    aa = title_tokens(a)
    bb = title_tokens(b)
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / max(1, len(aa | bb))


def title_similarity(a, b):
    na = normalize_title(a)
    nb = normalize_title(b)
    if not na or not nb:
        return 0.0
    sequence = SequenceMatcher(None, na, nb).ratio()
    jaccard = token_jaccard(na, nb)
    return max(sequence, jaccard)


def event_similarity(a, b):
    """Cheap event-level similarity without an embedding dependency."""
    sequence = SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()
    jaccard = token_jaccard(a, b)
    return (0.55 * sequence) + (0.45 * jaccard)


def likely_same_event(a, b):
    return (
        title_similarity(a, b) >= 0.90
        or event_similarity(a, b) >= 0.80
    )


def parse_datetime(value):
    raw = safe_text(value)
    if not raw:
        return None

    try:
        dt = datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )
        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )
        return dt.astimezone(BD_TZ)
    except Exception:
        pass

    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )
        return dt.astimezone(BD_TZ)
    except Exception:
        return None


def feed_entry_datetime(entry):
    for key in (
        "published_parsed",
        "updated_parsed",
        "created_parsed",
    ):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(
                    *parsed[:6],
                    tzinfo=timezone.utc,
                ).astimezone(BD_TZ)
            except Exception:
                pass

    for key in (
        "published",
        "updated",
        "created",
    ):
        dt = parse_datetime(
            entry.get(key)
        )
        if dt:
            return dt

    return None


def trim_source_text(text, limit):
    """Trim source text before rendering. Never appends ellipses."""
    text = safe_text(text)
    if len(text) <= limit:
        return text

    trimmed = text[:limit].rstrip()
    if " " in trimmed:
        trimmed = trimmed.rsplit(" ", 1)[0]

    return trimmed.rstrip(" ,:;-/—")


def clean_generated_text(text):
    text = safe_text(text)

    # Prevent visible truncation artifacts.
    text = re.sub(r"\.{2,}", ".", text)
    text = text.replace("\u2026", "")

    # Remove incomplete endings.
    text = re.sub(
        r"\s*[,;:]\s*$",
        "",
        text,
    )
    text = re.sub(
        r"\s*[-—]\s*$",
        "",
        text,
    )

    return text.strip()


def complete_text(text):
    raw = safe_text(text)
    if not raw:
        return False

    # A text that clean_generated_text() would mutilate
    # (trailing dash/comma/colon) is INCOMPLETE.
    if re.search(r"[\s,;:\-—…]+$", raw):
        return False

    text = clean_generated_text(raw)
    if not text:
        return False

    return not text.endswith(
        (",", ";", ":", "-", "—", "…")
    )






def now_iso():
    return datetime.now(
        BD_TZ
    ).isoformat()


# ============================================================
# STATE: QUEUE + EVENTS + KNOWLEDGE
# ============================================================

def default_state():
    return {
        "feeds": {},
        "queue": {},
        "events": {},
        "event_clusters": {},
        "posted_event_ids": [],
        "recent_titles": [],
    }


def load_state():
    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        if not isinstance(
            data,
            dict,
        ):
            return default_state()

        base = default_state()
        base.update(data)

        return base

    except Exception:
        return default_state()


def save_state(state):
    tmp = STATE_FILE + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(
        tmp,
        STATE_FILE,
    )


def load_posted_urls():
    try:
        with open(
            POSTED_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            return {
                canonical_url(line)
                for line in f
                if safe_text(line)
            }
    except FileNotFoundError:
        return set()


def save_posted_url(canonical):
    if not canonical:
        return

    with open(
        POSTED_FILE,
        "a",
        encoding="utf-8",
    ) as f:
        f.write(
            canonical
            + "\n"
        )


STATE = load_state()
POSTED_URLS = load_posted_urls()


def prune_state():
    cutoff_queue = (
        datetime.now(BD_TZ)
        - timedelta(
            days=QUEUE_RETENTION_DAYS
        )
    )

    cutoff_events = (
        datetime.now(BD_TZ)
        - timedelta(
            days=EVENT_RETENTION_DAYS
        )
    )

    queue = STATE.get(
        "queue",
        {},
    )

    keep_queue = {}

    for key, item in queue.items():
        dt = parse_datetime(
            item.get("last_seen")
            or item.get("published_date")
        )

        if (
            dt
            and dt >= cutoff_queue
        ):
            keep_queue[key] = item

    STATE["queue"] = keep_queue

    events = STATE.get(
        "events",
        {},
    )

    keep_events = {}

    for key, event in events.items():
        dt = parse_datetime(
            event.get("published_at")
            or event.get("selected_at")
        )

        if (
            dt
            and dt >= cutoff_events
        ):
            keep_events[key] = event

    STATE["events"] = keep_events

    titles = STATE.get(
        "recent_titles",
        [],
    )

    STATE["recent_titles"] = titles[-400:]


# ============================================================
# TIME WINDOWS
# ============================================================

NOW_BD = datetime.now(
    BD_TZ
)

TODAY_START = NOW_BD.replace(
    hour=0,
    minute=0,
    second=0,
    microsecond=0,
)

YESTERDAY_START = (
    TODAY_START
    - timedelta(days=1)
)

DISCOVERY_START = (
    NOW_BD
    - timedelta(
        hours=ROLLING_DISCOVERY_HOURS
    )
)
DISCOVERY_END = (
    NOW_BD
    + timedelta(
        minutes=FUTURE_TOLERANCE_MINUTES
    )
)

DISCOVERY_TARGET_PER_REGION = 18


# ============================================================
# CLIENTS
# ============================================================

exa = None
cerebras = None


def get_exa():
    global exa
    if exa is None:
        exa = Exa(api_key=EXA_API_KEY)
    return exa


def get_cerebras():
    global cerebras
    if cerebras is None:
        cerebras = Cerebras(api_key=CEREBRAS_API_KEY)
    return cerebras


# ============================================================
# CANDIDATE FILTERING
# ============================================================

BAD_PATH_RE = re.compile(
    r"/(opinion|editorial|sponsored|"
    r"tag|topic|live-blog|liveblog|"
    r"photo|photos|video)(/|$)",
    re.I,
)

BAD_TITLE_RE = re.compile(
    r"\b(sponsored|advertisement|"
    r"promo|opinion|editorial)\b",
    re.I,
)




def title_duplicate_against_state(title):
    for previous in STATE.get(
        "recent_titles",
        [],
    )[-250:]:
        if title_similarity(
            title,
            previous,
        ) >= 0.88:
            return True

    return False


def title_duplicate_against_list(
    title,
    candidates,
    threshold=0.88,
):
    for candidate in candidates:
        if title_similarity(
            title,
            candidate["title"],
        ) >= threshold:
            return True

    return False


# ============================================================
# RSS INGESTION + PERSISTENT QUEUE
# ============================================================

def extract_entry_image(
    entry,
    page_url,
):
    for key in (
        "media_content",
        "media_thumbnail",
    ):
        for item in entry.get(
            key,
            [],
        ):
            image_url = safe_text(
                item.get("url")
            )

            if image_url:
                return urljoin(
                    page_url,
                    image_url,
                )

    for enclosure in entry.get(
        "enclosures",
        [],
    ):
        href = safe_text(
            enclosure.get("href")
        )

        mime = safe_text(
            enclosure.get("type")
        ).lower()

        if (
            href
            and (
                not mime
                or mime.startswith(
                    "image/"
                )
            )
        ):
            return urljoin(
                page_url,
                href,
            )

    return ""


def queue_candidate(item):
    canonical = item["canonical"]

    existing = STATE["queue"].get(
        canonical
    )

    if existing:
        existing.update(
            {
                "last_seen": now_iso(),
                "image": (
                    item.get("image")
                    or existing.get("image", "")
                ),
            }
        )
        return

    STATE["queue"][canonical] = {
        **item,
        "status": "pending",
        "first_seen": now_iso(),
        "last_seen": now_iso(),
    }


def fetch_rss_feed(
    feed_def,
):
    url = feed_def["url"]

    old = STATE["feeds"].get(
        url,
        {},
    )

    headers = dict(
        HEADERS
    )

    if old.get("etag"):
        headers["If-None-Match"] = old[
            "etag"
        ]

    if old.get(
        "last_modified"
    ):
        headers["If-Modified-Since"] = old[
            "last_modified"
        ]

    try:
        response = session.get(
            url,
            headers=headers,
            timeout=20,
        )

        # 304 means the queue remains intact. The feed is reachable,
        # so this counts as healthy and clears any fail streak.
        if response.status_code == 304:
            logger.info(
                "RSS 304: %s",
                feed_def["name"],
            )
            mark_feed_healthy(feed_def, old)
            return 0

        if response.status_code >= 400:
            logger.warning(
                "RSS %s returned %s",
                feed_def["name"],
                response.status_code,
            )
            mark_feed_failed(feed_def, old)
            return 0

        STATE["feeds"][url] = {
            "etag": response.headers.get(
                "ETag",
                old.get("etag"),
            ),
            "last_modified": response.headers.get(
                "Last-Modified",
                old.get("last_modified"),
            ),
            "last_checked": now_iso(),
            "fail_count": 0,
            "alerted": False,
        }

        parsed = feedparser.parse(
            response.content
        )

        added = 0

        for entry in parsed.entries:
            published_dt = feed_entry_datetime(
                entry
            )

            date_estimated = False

            if not published_dt:
                # Some feeds send a date format we cannot parse.
                # Do not throw the story away: use fetch time instead,
                # and mark it so downstream code knows it is a guess.
                published_dt = datetime.now(
                    BD_TZ
                )
                date_estimated = True

            article_url = urljoin(
                url,
                safe_text(
                    entry.get("link")
                ),
            )

            title = safe_text(
                entry.get("title")
            )

            if not article_url or not title:
                continue

            item = {
                "title": title,
                "url": article_url,
                "canonical": canonical_url(
                    article_url
                ),
                "published_dt": published_dt.isoformat(),
                "published_date": published_dt.isoformat(),
                "source": feed_def["name"],
                "region": feed_def["region"],
                "excerpt": BeautifulSoup(
                    safe_text(
                        entry.get(
                            "summary"
                        )
                        or entry.get(
                            "description"
                        )
                    ),
                    "html.parser",
                ).get_text(
                    " ",
                    strip=True,
                )[:2000],
                "image": extract_entry_image(
                    entry,
                    article_url,
                ),
                "discovery": "rss",
                "date_estimated": date_estimated,
            }

            if not candidate_basic_allowed(
                {
                    **item,
                    "published_dt": published_dt,
                }
            ):
                continue

            if (
                item["canonical"]
                in POSTED_URLS
            ):
                continue

            before = item["canonical"] in STATE[
                "queue"
            ]

            queue_candidate(
                item
            )

            if not before:
                added += 1

        return added

    except Exception as exc:
        logger.warning(
            "RSS failed %s: %s",
            feed_def["name"],
            exc,
        )
        mark_feed_failed(feed_def, old)
        return 0


# Consecutive failed runs before we alert about a broken feed.
FEED_FAIL_ALERT_THRESHOLD = 3


def mark_feed_healthy(feed_def, old):
    STATE["feeds"][feed_def["url"]] = {
        **old,
        "last_checked": now_iso(),
        "fail_count": 0,
        "alerted": False,
    }


def mark_feed_failed(feed_def, old):
    fail_count = int(old.get("fail_count", 0)) + 1

    STATE["feeds"][feed_def["url"]] = {
        **old,
        "last_checked": now_iso(),
        "fail_count": fail_count,
    }

    if fail_count >= FEED_FAIL_ALERT_THRESHOLD and not old.get("alerted"):
        alert_feed_down(feed_def, fail_count)
        STATE["feeds"][feed_def["url"]]["alerted"] = True


def alert_feed_down(feed_def, fail_count):
    """Tell the admin a source has gone quiet, instead of failing silently forever."""
    message = (
        f"Feed down: {feed_def['name']} ({feed_def['region']})\n"
        f"Failed {fail_count} runs in a row.\n"
        f"URL: {feed_def['url']}\n"
        f"It will keep retrying, but this source is not feeding the bot right now."
    )

    if TELEGRAM_ADMIN_CHAT_ID:
        try:
            telegram_call(
                "sendMessage",
                data={
                    "chat_id": TELEGRAM_ADMIN_CHAT_ID,
                    "text": message,
                },
            )
        except Exception as exc:
            logger.warning(
                "Feed-down alert failed to send: %s",
                exc,
            )

    logger.error(message)


def collect_rss():
    added = 0

    for feed_def in RSS_FEEDS:
        added += fetch_rss_feed(
            feed_def
        )

    # Critical: queue is saved together with feed validators.
    # A later 304 cannot erase unposted queued stories.
    save_state(
        STATE
    )

    logger.info(
        "RSS queue additions: %d",
        added,
    )

    return added


# ============================================================
# EXA GAP-FILL DISCOVERY
# ============================================================

# ============================================================
# SOURCE UNIVERSE
# ============================================================
PRIMARY_TECH_DOMAINS = [
    "techcrunch.com", "theverge.com", "wired.com", "arstechnica.com",
    "engadget.com", "technologyreview.com", "news.ycombinator.com", "venturebeat.com",
    "techmeme.com", "techradar.com", "zdnet.com", "9to5google.com",
    "wabetainfo.com", "testingcatalog.com", "artificialintelligence-news.com", "unite.ai",
    "the-decoder.com", "siliconangle.com", "androidauthority.com", "macrumors.com",
]
FALLBACK_TECH_DOMAINS = []
ALL_PRIMARY_DOMAINS = PRIMARY_TECH_DOMAINS
ALL_FALLBACK_DOMAINS = FALLBACK_TECH_DOMAINS
ALL_ALLOWED_DOMAINS = ALL_PRIMARY_DOMAINS

def normalized_domain(url_or_source):
    raw = safe_text(url_or_source).lower()
    if "://" in raw:
        raw = urlparse(raw).netloc
    return raw.split(":")[0].removeprefix("www.").strip().rstrip("/")

def is_domain_allowed(url, domains):
    domain = normalized_domain(url)
    return any(domain == d or domain.endswith("." + d) for d in domains)

def primary_domain_allowed(url, region=None):
    return is_domain_allowed(url, ALL_PRIMARY_DOMAINS)

def fallback_domain_allowed(url, region=None):
    return is_domain_allowed(url, ALL_FALLBACK_DOMAINS)

def allowed_source_for_region(url, region=None):
    return primary_domain_allowed(url, region) or fallback_domain_allowed(url, region)

# ============================================================
# GOOGLE NEWS RSS: FREE GAP FILL
# ============================================================

GOOGLE_NEWS_QUERIES = {
    "Tech": [
        "major consumer technology news AI smartphones operating systems",
        "major AI model product launch technology",
        "major cybersecurity privacy breach technology outage",
        "Apple Google Microsoft OpenAI Meta Amazon major news",
        "major technology industry acquisition layoffs startup unicorn",
        "GitHub trending new tool capability technology",
        "Hugging Face major open model leaderboard technology",
        "Y Combinator major product launch milestone",
    ],
}

GOOGLE_NEWS_LOCALE = {"Tech": ("en-US", "US", "US:en")}


def resolve_google_news_url(link):
    """Google News RSS gives a redirect link, not the publisher URL.
    Follow it once (without downloading the full page) to get the
    real article URL. Return "" if it cannot be resolved safely."""
    try:
        response = session.get(
            link,
            timeout=10,
            allow_redirects=True,
            headers=HEADERS,
            stream=True,
        )
        real_url = safe_text(response.url)
        response.close()

        if not real_url or "news.google.com" in real_url:
            return ""

        return real_url

    except Exception:
        return ""


def google_news_gap_fill(
    region,
    existing_count,
    needed,
):
    # Same thin-coverage trigger as Exa, tried first because it is free.
    if existing_count >= max(
        6,
        needed * 3,
    ):
        return 0

    queries = GOOGLE_NEWS_QUERIES.get("Tech", [])
    hl, gl, ceid = GOOGLE_NEWS_LOCALE.get("Tech", ("en-US", "US", "US:en"))

    added = 0

    for query in queries:
        try:
            feed_url = (
                "https://news.google.com/rss/search?q="
                + quote(f"{query} when:2d")
                + f"&hl={hl}&gl={gl}&ceid={ceid}"
            )

            response = session.get(
                feed_url,
                timeout=15,
                headers=HEADERS,
            )

            if response.status_code >= 400:
                continue

            parsed = feedparser.parse(
                response.content
            )

            for entry in parsed.entries[:6]:
                title = safe_text(
                    entry.get("title")
                )
                link = safe_text(
                    entry.get("link")
                )

                if not title or not link:
                    continue

                real_url = resolve_google_news_url(
                    link
                )

                if not real_url:
                    continue

                published_dt = feed_entry_datetime(
                    entry
                )
                date_estimated = False

                if not published_dt:
                    published_dt = datetime.now(
                        BD_TZ
                    )
                    date_estimated = True

                item = {
                    "title": title,
                    "url": real_url,
                    "canonical": canonical_url(
                        real_url
                    ),
                    "published_dt": published_dt.isoformat(),
                    "published_date": published_dt.isoformat(),
                    "source": source_name(
                        real_url
                    ),
                    "region": region,
                    "excerpt": BeautifulSoup(
                        safe_text(
                            entry.get("summary")
                        ),
                        "html.parser",
                    ).get_text(
                        " ",
                        strip=True,
                    )[:2000],
                    "image": "",
                    "discovery": "google_news",
                    "date_estimated": date_estimated,
                }

                if not primary_domain_allowed(real_url, region):
                    continue

                if not candidate_basic_allowed(
                    {
                        **item,
                        "published_dt": published_dt,
                    }
                ):
                    continue

                if item["canonical"] in POSTED_URLS:
                    continue

                if item["canonical"] in STATE["queue"]:
                    continue

                queue_candidate(
                    item
                )
                added += 1

                if added >= MAX_GOOGLE_NEWS_CANDIDATES:
                    return added

        except Exception as exc:
            logger.warning(
                "Google News gap fill failed %s: %s",
                region,
                exc,
            )

    return added


def exa_gap_fill(region, existing_count, needed, fallback=False):
    if existing_count >= max(12, needed * 3):
        return 0

    domains = FALLBACK_TECH_DOMAINS if fallback else PRIMARY_TECH_DOMAINS
    if not domains:
        return 0
    queries = [
        "latest major consumer technology AI smartphone operating system news",
        "latest major AI model product launch technology",
        "latest major cybersecurity privacy breach outage technology",
        "latest Apple Google Microsoft OpenAI Meta Amazon technology news",
        "latest technology industry acquisition layoffs startup unicorn news",
        "latest trending GitHub new tool capability",
        "latest Hugging Face open model release leaderboard",
        "latest Y Combinator major product launch milestone",
    ]

    added = 0
    for query in queries:
        try:
            results = get_exa().search_and_contents(
                query, type="auto", category="news", num_results=8,
                include_domains=domains,
                start_published_date=DISCOVERY_START.isoformat(),
                end_published_date=DISCOVERY_END.isoformat(),
                contents={"highlights": {"max_characters": 900}},
            )
            for result in results.results:
                url = safe_text(getattr(result, "url", ""))
                title = safe_text(getattr(result, "title", ""))
                published_dt = parse_datetime(getattr(result, "published_date", ""))
                if not url or not title or not published_dt:
                    continue
                if fallback:
                    if not fallback_domain_allowed(url, region):
                        continue
                elif not primary_domain_allowed(url, region):
                    continue
                item = {
                    "title": title, "url": url, "canonical": canonical_url(url),
                    "published_dt": published_dt.isoformat(), "published_date": published_dt.isoformat(),
                    "source": source_name(url), "region": "Tech",
                    "excerpt": safe_text(" ".join(getattr(result, "highlights", []) if isinstance(getattr(result, "highlights", []), list) else str(getattr(result, "highlights", ""))))[:2000],
                    "image": safe_text(getattr(result, "image", "")),
                    "discovery": "exa_fallback" if fallback else "exa",
                    "source_pool": "fallback" if fallback else "primary",
                }
                if not candidate_basic_allowed({**item, "published_dt": published_dt}):
                    continue
                if item["canonical"] in POSTED_URLS or item["canonical"] in STATE["queue"]:
                    continue
                queue_candidate(item)
                added += 1
                if added >= MAX_EXA_CANDIDATES:
                    return added
        except Exception as exc:
            logger.warning("Exa %s discovery failed: %s", "fallback" if fallback else "primary", exc)
    return added


def queue_candidates_for_region(
    region,
):
    count = 0

    for item in STATE[
        "queue"
    ].values():
        if (
            item.get("region")
            == region
            and item.get("status")
            == "pending"
        ):
            published = parse_datetime(
                item.get(
                    "published_date"
                )
            )

            if (
                published
                and DISCOVERY_START
                <= published
                <= DISCOVERY_END
            ):
                count += 1

    return count


# ============================================================
# CANDIDATE NORMALIZATION
# ============================================================

# ============================================================
# VERSION 1 EDITORIAL RANKING
# ============================================================

RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "ranked": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "rank": {"type": "integer", "minimum": 1},
                    "score": {"type": "integer", "minimum": 0, "maximum": 10},
                    "important": {"type": "boolean"},
                    "topic": {"type": "string"},
                    "institution": {"type": "string"},
                    "event_key": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "rank", "score", "important", "topic", "institution", "event_key", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ranked"],
    "additionalProperties": False,
}


def enrich_thin_excerpt(item):
    """Best-effort article enrichment. Failure never removes a candidate."""
    try:
        downloaded = trafilatura.fetch_url(item["url"])
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded)
        return safe_text(text)[:1200] if text else None
    except Exception:
        return None


def enrich_thin_excerpts(regional):
    enriched = 0
    for item in regional:
        if enriched >= MAX_EXCERPT_ENRICH:
            break
        excerpt = safe_text(item.get("excerpt", ""))
        if len(excerpt) >= THIN_EXCERPT_CHARS:
            continue
        fuller = enrich_thin_excerpt(item)
        if fuller and len(fuller) > len(excerpt):
            item["excerpt"] = fuller
            enriched += 1
    return regional


def _rank_prompt(region):
    topic_list = ", ".join(TOPICS[region])
    return f"""
You are the editor-in-chief of @CareerNewsroom.

Rank this batch of tech news candidates by REAL editorial importance in the previous 24 hours.
The channel is for everyday technology users. Do not rank by headline excitement alone.
Do not invent facts. Return EVERY candidate in this batch.

IMPORTANT: There is NO requirement to publish a story from every sector. Diversity is a
selection preference only after importance is established. Never lower a score just because
another story covers the same sector, and never raise a weak story to fill a sector.

Priority areas, when genuinely important:
1. Major AI model releases, frontier capability changes, and AI products with broad impact.
2. Major AI acquisitions, strategic deals, or partnerships that materially affect the industry.
3. Major cybersecurity incidents, privacy incidents, critical vulnerabilities, and outages.
4. Major smartphone, operating-system, browser, search, social, cloud, and app-store changes.
5. Major moves by Apple, Google, Microsoft, OpenAI, Meta, Amazon, NVIDIA and other major tech companies.
6. Important new consumer technology products.
7. Trending GitHub repositories only when the repository is a genuinely useful new tool/capability,
   not ordinary developer churn.
8. Startups reaching unicorn status or shipping something with broad real-world impact.
9. Y Combinator companies only for major product launches or milestones, not routine funding.
10. Hugging Face open-model releases or leaderboard changes that materially move the state of the art.

Normally score low or reject:
- reviews, hands-ons, unboxings, rumors, leaks, speculation
- EV/car/robotaxi/vehicle news
- healthtech, biotech, medtech, medical or pharmaceutical news
- routine startup funding, VC, finance, legal or policy commentary
- energy, batteries, utilities and climate/energy policy
- low-level engineering stories aimed at engineers
- podcasts, webinars, event recordings, opinion/promotional pieces
- minor app features, routine patches and insignificant updates

Scoring:
9-10 exceptional, broad user or industry impact
7-8 clearly important and publishable
4-6 interesting but normally not publishable
0-3 low-value, repetitive, promotional, speculative, niche or excluded

A score of 7+ is required for publication. Return EVERY candidate with:
id, rank, score, important, topic, institution, event_key, reason.
The important field MUST be true only when score >= 7.

Allowed topics:
{topic_list}
"""


def _rank_batch(batch, region, batch_no):
    lines = []
    for idx, item in enumerate(batch, start=1):
        published = item.get("published_date", "")
        age_note = ""
        dt = parse_datetime(published)
        if dt:
            age_hours = max(0.0, (NOW_BD - dt).total_seconds() / 3600)
            age_note = f"Age: {age_hours:.1f} hours"
        lines.append("\n".join([
            f"ID: {idx}",
            f"Title: {item.get('title','')}",
            f"Source: {item.get('source','')}",
            f"Published: {published}",
            age_note,
            f"Description/Excerpt: {trim_source_text(item.get('excerpt',''), 650)}",
            "",
        ]))

    try:
        response = get_cerebras().chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[
                {"role": "system", "content": _rank_prompt(region)},
                {"role": "user", "content": "\n".join(lines)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": f"tech_news_rank_batch_{batch_no}",
                    "strict": True,
                    "schema": RANK_SCHEMA,
                },
            },
            reasoning_effort="low",
            temperature=0.0,
            max_completion_tokens=3500,
        )
        data = json.loads(safe_text(response.choices[0].message.content))
        return data.get("ranked", [])
    except Exception as exc:
        logger.error("Ranking batch %d failed: %s", batch_no, exc)
        return []




# ============================================================
# VERSION 1 EVENT DEDUPLICATION
# ============================================================

def extract_entities(text):
    words = re.findall(r"[A-Za-z][A-Za-z&'-]{1,}", safe_text(text).lower())
    return {w for w in words if w not in STOPWORDS}


def entity_overlap(a, b):
    ea = extract_entities(f"{a.get('title','')} {a.get('excerpt','')}")
    eb = extract_entities(f"{b.get('title','')} {b.get('excerpt','')}")
    if not ea or not eb:
        return 0.0
    return len(ea & eb) / max(1, min(len(ea), len(eb)))


def event_similarity_v04(a, b):
    title_score = title_similarity(a.get("title", ""), b.get("title", ""))
    entity_score = entity_overlap(a, b)
    return (0.75 * title_score) + (0.25 * entity_score)


def same_event_window(a, b, hours=30):
    da = parse_datetime(a.get("published_date"))
    db = parse_datetime(b.get("published_date"))
    if not da or not db:
        return False
    return abs((da - db).total_seconds()) <= hours * 3600


def cluster_ranked_events(ranked):
    """Conservative event clustering. Uncertain items are always kept."""
    clusters = []
    ordered = sorted(
        ranked,
        key=lambda x: x.get("editor_rank", 9999),
    )
    for item in ordered:
        placed = False
        for cluster in clusters:
            representative = cluster[0]
            item_event_key = safe_text(item.get("event_key"))
            rep_event_key = safe_text(representative.get("event_key"))
            same_key = bool(
                item_event_key
                and rep_event_key
                and item_event_key == rep_event_key
                and (
                    entity_overlap(item, representative) >= 0.25
                    or title_similarity(item.get("title", ""), representative.get("title", "")) >= 0.55
                )
            )
            if same_key or (
                same_event_window(item, representative)
                and event_similarity_v04(item, representative) >= 0.88
            ):
                cluster.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])

    output = []
    for index, cluster in enumerate(clusters, start=1):
        representative = cluster[0]
        stable_key = normalize_title(representative.get("title", "")) or representative.get("canonical", "")
        digest = hashlib.sha1(stable_key.encode("utf-8")).hexdigest()[:10]
        cluster_id = f"evt_{digest}"
        sources = sorted({safe_text(x.get("source")) for x in cluster if safe_text(x.get("source"))})
        for member in cluster:
            row = dict(member)
            row.update({
                "event_cluster_id": cluster_id,
                "event_cluster_size": len(cluster),
                "event_sources": sources,
                "event_source_count": len(sources),
                "event_confidence": 1.0 if len(cluster) > 1 else 0.6,
            })
            output.append(row)
    return output


def collapse_event_clusters(ranked):
    clustered = cluster_ranked_events(ranked)
    winners = {}
    for item in clustered:
        key = item.get("event_cluster_id") or item.get("canonical")
        old = winners.get(key)
        if old is None:
            winners[key] = item
            continue
        # Preserve the highest editorial rank, then newest story.
        item_key = (
            item.get("editor_rank", 9999),
            -(parse_datetime(item.get("published_date")).timestamp() if parse_datetime(item.get("published_date")) else 0),
        )
        old_key = (
            old.get("editor_rank", 9999),
            -(parse_datetime(old.get("published_date")).timestamp() if parse_datetime(old.get("published_date")) else 0),
        )
        if item_key < old_key:
            winners[key] = item
    return sorted(winners.values(), key=lambda x: x.get("editor_rank", 9999))


def persist_event_cluster_state(ranked):
    clusters = STATE.setdefault("event_clusters", {})
    for item in ranked:
        event_id = item.get("event_cluster_id")
        if not event_id:
            continue
        clusters[event_id] = {
            "event_id": event_id,
            "topic": item.get("topic", ""),
            "region": item.get("region", ""),
            "sources": item.get("event_sources", []),
            "source_count": item.get("event_source_count", 0),
            "confidence": item.get("event_confidence", 0),
            "last_seen": now_iso(),
            "headline": item.get("title", ""),
        }


def remember_posted_event(story):
    event_id = story.get("event_cluster_id") or make_event_id(story)
    ids = STATE.setdefault("posted_event_ids", [])
    if event_id and event_id not in ids:
        ids.append(event_id)
    STATE["posted_event_ids"] = ids[-500:]
    return event_id


# ============================================================
# ARTICLE EXTRACTION
# ============================================================

def find_image_candidates(
    url,
    page_html=None,
    final_url=None,
    preferred_image="",
):
    """Return ordered article-image candidates from RSS and page metadata.

    The caller must validate candidates by actually downloading them. This
    prevents one broken RSS URL from blocking a perfectly valid og:image or
    JSON-LD image later in the pipeline.
    """
    candidates = []

    def add(value):
        value = safe_text(value).strip()
        if not value or value.startswith("data:"):
            return
        try:
            value = urljoin(final_url or url, value)
        except Exception:
            return
        if value not in candidates:
            candidates.append(value)

    add(preferred_image)

    try:
        base_url = final_url or url
        if page_html is None:
            response = session.get(
                url,
                headers={**HEADERS, "Referer": url},
                timeout=20,
            )
            if response.status_code >= 400:
                return candidates
            page_html = response.text
            base_url = response.url

        soup = BeautifulSoup(page_html, "html.parser")

        for attrs in (
            {"property": "og:image"},
            {"property": "og:image:url"},
            {"name": "twitter:image"},
            {"name": "twitter:image:src"},
        ):
            for tag in soup.find_all("meta", attrs=attrs):
                add(urljoin(base_url, safe_text(tag.get("content", ""))))

        for link in soup.find_all("link"):
            rel = {safe_text(x).lower() for x in (link.get("rel") or [])}
            href = safe_text(link.get("href", ""))
            if "preload" in rel and safe_text(link.get("as", "")).lower() == "image":
                add(urljoin(base_url, href))
            elif rel & {"image_src", "image"}:
                add(urljoin(base_url, href))

        # JSON-LD often has the cleanest article image URL.
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text("", strip=True)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            stack = data if isinstance(data, list) else [data]
            while stack:
                obj = stack.pop()
                if isinstance(obj, dict):
                    img = obj.get("image")
                    if isinstance(img, str):
                        add(img)
                    elif isinstance(img, list):
                        for val in img:
                            add(val if isinstance(val, str) else (val or {}).get("url", "") if isinstance(val, dict) else "")
                    elif isinstance(img, dict):
                        add(img.get("url", "") or img.get("contentUrl", ""))
                    for key in ("@graph", "mainEntity", "mainEntityOfPage"):
                        child = obj.get(key)
                        if isinstance(child, (dict, list)):
                            stack.extend(child if isinstance(child, list) else [child])

        # Lazy-loaded and srcset images. Keep only plausible image URLs.
        for tag in soup.find_all(["img", "source"]):
            for attr in ("data-src", "data-lazy-src", "data-original", "data-image", "src"):
                add(tag.get(attr, ""))
            srcset = safe_text(tag.get("srcset", ""))
            if srcset:
                for part in srcset.split(","):
                    add(part.strip().split(" ")[0])
            data_srcset = safe_text(tag.get("data-srcset", ""))
            if data_srcset:
                for part in data_srcset.split(","):
                    add(part.strip().split(" ")[0])

    except Exception as exc:
        logger.debug("Image candidate discovery failed %s: %s", url, exc)

    return candidates


def find_og_image(url, page_html=None, final_url=None):
    candidates = find_image_candidates(url, page_html, final_url)
    return candidates[0] if candidates else ""




# ============================================================
# STORY + KNOWLEDGE GENERATION
# ============================================================

STORY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "highlights": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 5,
        },
        "the_context": {"type": "string"},
        "bottom_line": {"type": "string"},
        "bold_terms": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 16,
        },
    },
    "required": [
        "headline",
        "summary",
        "highlights",
        "the_context",
        "bottom_line",
        "bold_terms",
    ],
    "additionalProperties": False,
}


def first_sentence(text):
    text = clean_generated_text(
        text
    )

    # Conservative sentence extraction. Avoids common tech
    # abbreviations and decimals splitting incorrectly.
    protected = {
        "U.S.": "US_SENTINEL",
        "U.K.": "UK_SENTINEL",
        "E.U.": "EU_SENTINEL",
        "No.": "NO_SENTINEL",
        "Inc.": "INC_SENTINEL",
        "Ltd.": "LTD_SENTINEL",
        "Dr.": "DR_SENTINEL",
        "Mr.": "MR_SENTINEL",
        "Mrs.": "MRS_SENTINEL",
        "Ms.": "MS_SENTINEL",
    }

    working = text

    for old, marker in protected.items():
        working = working.replace(
            old,
            marker,
        )

    match = re.search(
        r"(.+?[.!?])(?:\s|$)",
        working,
    )

    if match:
        sentence = match.group(1)
    else:
        sentence = working

    for old, marker in protected.items():
        sentence = sentence.replace(
            marker,
            old,
        )

    return clean_generated_text(
        sentence
    )




# ============================================================
# NUMERIC GROUNDING
# ============================================================

NUMBER_RE = re.compile(
    r"""
    (?:
        (?:US|U\.S\.|HK|HK\$|Tk|BDT|USD|EUR|GBP|JPY|CNY|INR|৳|\$|€|£|¥)
        \s*
    )?
    \d[\d,]*(?:\.\d+)?
    \s*
    (?:
        million|billion|trillion|
        crore|lakh|bn|mn|b|m|k|%
    )?
    """,
    re.I | re.X,
)

YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")

_NUMERIC_SCALES = {
    "": 1.0,
    "k": 1e3,
    "m": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
    "trillion": 1e12,
    "t": 1e12,
    "crore": 1e7,
    "lakh": 1e5,
}
_NUMERIC_CURRENCIES = {
    "$": "usd",
    "usd": "usd",
    "tk": "bdt",
    "bdt": "bdt",
    "৳": "bdt",
    "€": "eur",
    "eur": "eur",
    "£": "gbp",
    "gbp": "gbp",
    "¥": "jpy",
    "jpy": "jpy",
    "cny": "cny",
    "inr": "inr",
    "₹": "inr",
    "hk$": "hkd",
    "hk": "hkd",
}

def _numeric_signature(raw):
    text = safe_text(raw).strip().lower()
    if not text:
        return None

    currency = None
    for symbol in sorted(_NUMERIC_CURRENCIES, key=len, reverse=True):
        if text.startswith(symbol):
            currency = _NUMERIC_CURRENCIES[symbol]
            text = text[len(symbol):].strip()
            break

    if text.endswith("%"):
        unit = "%"
        text = text[:-1].strip()
    else:
        m = re.search(r"(trillion|billion|million|crore|lakh|bn|mn|[kmbt])$", text)
        unit = m.group(1) if m else ""
        if m:
            text = text[:m.start()].strip()

    text = text.replace(",", "")
    try:
        value = float(text)
    except ValueError:
        return None

    if unit == "%":
        scale = 1.0
        percent = True
    else:
        scale = _NUMERIC_SCALES.get(unit, 1.0)
        percent = False

    return {
        "value": value * scale,
        "percent": percent,
        "currency": currency,
    }

def normalize_number(raw):
    sig = _numeric_signature(raw)
    if not sig:
        return ""
    value = sig["value"]
    value_key = f"{value:.12g}"
    currency = sig["currency"] or ""
    percent = "%" if sig["percent"] else ""
    return f"{currency}|{percent}|{value_key}"

def numeric_tokens(text):
    tokens = []
    for match in NUMBER_RE.finditer(safe_text(text)):
        token = safe_text(match.group(0)).strip()
        sig = _numeric_signature(token)
        if not sig:
            continue

        # Ignore bare years, but retain years with a financial/percent/unit marker.
        numeric_value = sig["value"]
        if (
            numeric_value.is_integer()
            and YEAR_RE.match(str(int(numeric_value)))
            and not sig["percent"]
            and not sig["currency"]
        ):
            continue

        if token:
            tokens.append(token)
    return tokens

def _numeric_equivalent(source_sig, generated_sig):
    if not source_sig or not generated_sig:
        return False
    if source_sig["percent"] != generated_sig["percent"]:
        return False

    # If both explicitly name currencies, they must agree.
    # If only one names a currency, accept the numeric equivalent because
    # source extraction often drops currency symbols around abbreviated forms.
    if (
        source_sig["currency"]
        and generated_sig["currency"]
        and source_sig["currency"] != generated_sig["currency"]
    ):
        return False

    return abs(source_sig["value"] - generated_sig["value"]) <= max(
        1e-9, abs(source_sig["value"]) * 1e-9
    )

def numeric_grounded(story, article_text):
    source_sigs = [
        _numeric_signature(x)
        for x in numeric_tokens(article_text)
    ]
    source_sigs = [x for x in source_sigs if x]

    generated_text = " ".join(
        [
            story.get("headline", ""),
            story.get("summary", ""),
            *story.get("highlights", []),
        ]
    )

    for token in numeric_tokens(generated_text):
        generated_sig = _numeric_signature(token)
        if not generated_sig:
            continue

        if not any(
            _numeric_equivalent(source_sig, generated_sig)
            for source_sig in source_sigs
        ):
            return False, token

    return True, ""


# ============================================================
# BOLD TERMS
# ============================================================

def derive_bold_terms(
    story,
):
    terms = [
        safe_text(x)
        for x in story.get(
            "bold_terms",
            [],
        )
        if safe_text(x)
    ]

    combined = " ".join(
        [
            story.get(
                "summary",
                "",
            ),
            *story.get(
                "highlights",
                [],
            ),
        ]
    )

    # Financial figures, but do not bold bare years.
    for match in NUMBER_RE.finditer(
        combined
    ):
        token = safe_text(
            match.group(0)
        )

        numeric_only = re.sub(
            r"[^\d.]",
            "",
            token,
        )

        if (
            YEAR_RE.match(
                numeric_only
            )
            and not re.search(
                r"(Tk|BDT|USD|EUR|GBP|JPY|CNY|INR|৳|\$|€|£|¥|%|million|billion|crore|lakh)",
                token,
                re.I,
            )
        ):
            continue

        if token:
            terms.append(
                token
            )

    unique = []
    seen = set()

    for term in sorted(
        terms,
        key=len,
        reverse=True,
    ):
        key = term.lower()

        if (
            len(term) >= 2
            and key not in seen
        ):
            seen.add(key)
            unique.append(term)

    return unique[:16]


def escape_rich_html(
    text,
):
    return html.escape(
        clean_generated_text(text),
        quote=False,
    )


def bold_terms_html(
    text,
    terms,
):
    text = clean_generated_text(
        text
    )

    if not text:
        return ""

    result = text

    # Use letter-only markers to avoid collisions with numeric terms.
    replacements = []

    for index, term in enumerate(
        sorted(
            {
                safe_text(x)
                for x in terms
                if safe_text(x)
            },
            key=len,
            reverse=True,
        )
    ):
        marker = (
            f"__RICHBOLD_{chr(65 + (index % 26))}"
            f"{index // 26}__"
        )

        pattern = re.compile(
            re.escape(term),
            re.I,
        )

        match = pattern.search(
            result
        )

        if match:
            original = match.group(
                0
            )
            result = (
                result[:match.start()]
                + marker
                + result[match.end():]
            )
            replacements.append(
                (
                    marker,
                    original,
                )
            )

    escaped = html.escape(
        result,
        quote=False,
    )

    for marker, original in replacements:
        escaped = escaped.replace(
            marker,
            "<b>"
            + html.escape(
                original,
                quote=False,
            )
            + "</b>",
        )

    return escaped


# ============================================================
# DYNAMIC RICH MESSAGE HTML
# ============================================================



def rich_visible_length(text):
    """Return Telegram-visible character count for Rich HTML text.

    Telegram limits the rendered text, not the raw HTML markup, so strip
    tags and decode HTML entities before counting characters.
    """
    no_tags = re.sub(r"<[^>]+>", "", text)
    no_attrs = re.sub(
        r"\[[^\]]+\]\([^)]+\)",
        lambda m: m.group(0).split("]")[0][1:],
        no_tags,
    )
    return len(html.unescape(no_attrs))





# ============================================================
# IMAGE BRANDING: ONLY @CareerNewsroom
# ============================================================

def find_font(
    bold=False,
):
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]
    )

    for path in candidates:
        if os.path.exists(path):
            return path

    return None


def download_image(
    url,
    referer,
):
    if not url:
        return None

    try:
        response = session.get(
            url,
            headers={
                **HEADERS,
                "Referer": referer,
            },
            timeout=20,
            stream=True,
        )

        if response.status_code >= 400:
            return None

        content_type = (
            response.headers.get(
                "content-type",
                "",
            )
            .lower()
        )

        if (
            content_type
            and not content_type.startswith(
                "image/"
            )
        ):
            return None

        buf = BytesIO()

        for chunk in response.iter_content(
            65536
        ):
            if not chunk:
                continue

            buf.write(
                chunk
            )

            if buf.tell() > 8_000_000:
                return None

        buf.seek(0)

        image = Image.open(
            buf
        )
        image.load()

        if (
            image.width < 400
            or image.height < 250
        ):
            return None

        return image.convert(
            "RGB"
        )

    except Exception as exc:
        logger.warning(
            "Image download failed: %s",
            exc,
        )
        return None


def crop_cover(
    image,
    size=(1200, 675),
):
    target_w, target_h = size

    ratio = max(
        target_w / image.width,
        target_h / image.height,
    )

    resized = image.resize(
        (
            int(
                image.width
                * ratio
            ),
            int(
                image.height
                * ratio
            ),
        ),
        Image.Resampling.LANCZOS,
    )

    left = (
        resized.width
        - target_w
    ) // 2

    top = (
        resized.height
        - target_h
    ) // 2

    return resized.crop(
        (
            left,
            top,
            left + target_w,
            top + target_h,
        )
    )


def image_average_brightness(
    image,
):
    small = image.resize(
        (1, 1)
    ).convert(
        "L"
    )
    return small.getpixel(
        (0, 0)
    )


def display_source_name(source):
    """Return a reader-friendly publication label for the fallback card."""
    raw = safe_text(source).strip()
    if not raw:
        return "Source"
    aliases = {
        "WIRED": "Wired",
        "ZDNET": "ZDNET",
        "9to5Google": "9to5Google",
        "MacRumors": "MacRumors",
        "WABetaInfo": "WABetaInfo",
        "TestingCatalog": "TestingCatalog",
        "AI News": "AI News",
        "Unite.AI": "Unite.AI",
        "The Decoder": "The Decoder",
        "SiliconANGLE": "SiliconANGLE",
        "Techmeme": "Techmeme",
        "MIT Technology Review": "MIT Technology Review",
    }
    return aliases.get(raw, raw)




def download_source_logo(source, article_url=""):
    """Download a reasonably large source logo/icon from the publisher site."""
    homepage = source_homepage(source, article_url)
    if not homepage:
        return None

    candidates = []
    try:
        response = session.get(
            homepage,
            headers={**HEADERS, "Referer": article_url or homepage},
            timeout=15,
        )
        if response.status_code < 400:
            soup = BeautifulSoup(response.text, "html.parser")
            base = response.url
            for rel in ("apple-touch-icon", "icon", "shortcut icon"):
                for tag in soup.find_all("link", rel=lambda x: x and rel in [str(v).lower() for v in (x if isinstance(x, list) else [x])]):
                    href = safe_text(tag.get("href", ""))
                    if href:
                        candidates.append(urljoin(base, href))
            for attrs in ({"property": "og:image"}, {"name": "twitter:image"}):
                for tag in soup.find_all("meta", attrs=attrs):
                    value = safe_text(tag.get("content", ""))
                    if value:
                        candidates.append(urljoin(base, value))
    except Exception as exc:
        logger.debug("Source-logo page discovery failed %s: %s", source, exc)

    # Direct favicon is a strong last-resort source-site identity image.
    parsed = urlparse(homepage)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    candidates.extend([
        f"{origin}/apple-touch-icon.png",
        f"{origin}/favicon.ico",
        f"{origin}/favicon.png",
    ])

    # Google favicon proxy is only the last fallback, not the primary source.
    candidates.append(
        "https://www.google.com/s2/favicons?domain="
        + parsed.netloc
        + "&sz=256"
    )

    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        image = download_image(candidate, homepage)
        if image is not None:
            return image
    return None


def _contrast_palette(image):
    brightness = image_average_brightness(image)
    if brightness < 120:
        return (245, 245, 245, 235), (18, 22, 28, 255)
    return (24, 29, 35, 225), (250, 250, 250, 255)




def branded_card(photo, source, source_position="left"):
    """Brand a real article image with only @CareerNewsroom bottom-right."""
    base = crop_cover(photo).convert("RGBA")
    chip_bg, chip_fg = _contrast_palette(base)
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font_path = find_font(bold=True)
    font = ImageFont.truetype(font_path, 24) if font_path else ImageFont.load_default()
    rect, text_pos, channel_text = _draw_channel_chip(draw, font, base.size)
    draw.rounded_rectangle(rect, radius=16, fill=chip_bg)
    draw.text(text_pos, channel_text, font=font, fill=chip_fg)
    return Image.alpha_composite(base, overlay).convert("RGB")


def fallback_logo_card(logo, source):
    """Large centered source logo + channel chip, with contrast-aware background."""
    canvas = Image.new("RGB", (1200, 675), (238, 240, 243))
    # Sample source logo average to choose a contrasting neutral background.
    logo_brightness = image_average_brightness(logo)
    bg = (28, 34, 42) if logo_brightness > 150 else (238, 240, 243)
    canvas.paste(bg, (0, 0, canvas.width, canvas.height))
    logo = logo.convert("RGBA")
    # Preserve logo aspect ratio and make it visually large without touching edges.
    max_w, max_h = 780, 430
    scale = min(max_w / logo.width, max_h / logo.height)
    new_size = (max(1, int(logo.width * scale)), max(1, int(logo.height * scale)))
    logo = logo.resize(new_size, Image.Resampling.LANCZOS)
    x = (1200 - logo.width) // 2
    y = (675 - logo.height) // 2
    # Keep transparency where possible; add a subtle neutral plate only when needed.
    canvas_rgba = canvas.convert("RGBA")
    canvas_rgba.alpha_composite(logo, (x, y))

    overlay = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font_path = find_font(bold=True)
    font = ImageFont.truetype(font_path, 24) if font_path else ImageFont.load_default()
    chip_bg, chip_fg = _contrast_palette(canvas_rgba)
    rect, text_pos, channel_text = _draw_channel_chip(draw, font, canvas_rgba.size)
    draw.rounded_rectangle(rect, radius=16, fill=chip_bg)
    draw.text(text_pos, channel_text, font=font, fill=chip_fg)
    return Image.alpha_composite(canvas_rgba, overlay).convert("RGB")


def fallback_source_name_card(source):
    """Source name centered in bold + channel chip bottom-right."""
    canvas = Image.new("RGB", (1200, 675), (30, 39, 51))
    draw = ImageDraw.Draw(canvas)
    font_path = find_font(bold=True)
    name = display_source_name(source)
    if font_path:
        size = 110
        while size >= 48:
            font = ImageFont.truetype(font_path, size)
            bbox = draw.textbbox((0, 0), name, font=font)
            if bbox[2] - bbox[0] <= 1000:
                break
            size -= 4
    else:
        font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), name, font=font)
    bbox = draw.textbbox((0, 0), name, font=font)
    x = (1200 - (bbox[2] - bbox[0])) // 2
    y = (675 - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((x, y), name, font=font, fill="white")

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    chip_font = ImageFont.truetype(font_path, 24) if font_path else ImageFont.load_default()
    chip_bg, chip_fg = _contrast_palette(canvas.convert("RGBA"))
    rect, text_pos, channel_text = _draw_channel_chip(odraw, chip_font, canvas.size)
    odraw.rounded_rectangle(rect, radius=16, fill=chip_bg)
    odraw.text(text_pos, channel_text, font=chip_font, fill=chip_fg)
    return Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")


def prepare_image(story, index):
    # Retry every discovered article-image candidate. A broken RSS image must
    # never block a valid image available in page metadata.
    candidates = []
    for value in story.get("image_candidates", []):
        if value and value not in candidates:
            candidates.append(value)
    if story.get("image_url") and story.get("image_url") not in candidates:
        candidates.append(story.get("image_url"))

    image = None
    for candidate in candidates:
        image = download_image(candidate, story.get("url", ""))
        if image is not None:
            logger.info("Article image recovered: %s", candidate)
            break

    if image is not None:
        branded = branded_card(image, story.get("source", "Source"))
    else:
        source = story.get("source", "Source")
        logo = download_source_logo(source, story.get("url", ""))
        if logo is not None:
            logger.info("Using source-logo fallback: %s", source)
            branded = fallback_logo_card(logo, source)
        else:
            logger.info("Using source-name fallback: %s", source)
            branded = fallback_source_name_card(source)

    path = f"/tmp/news_{index}.jpg"
    branded.save(path, "JPEG", quality=88, optimize=True)
    return path


# ============================================================
# TELEGRAM RICH MESSAGES
# ============================================================

def telegram_call(
    method,
    data=None,
    files=None,
):
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/"
        f"{method}"
    )

    last = {
        "ok": False,
        "description": "Unknown error",
    }

    for attempt in range(
        1,
        6,
    ):
        try:
            response = session.post(
                url,
                data=data or {},
                files=files,
                timeout=90,
            )

            result = response.json()

            if result.get(
                "ok"
            ):
                return result

            last = result

            if response.status_code == 429:
                retry_after = int(
                    result.get(
                        "parameters",
                        {},
                    ).get(
                        "retry_after",
                        5,
                    )
                )

                logger.warning(
                    "Telegram 429; waiting %ss",
                    retry_after,
                )

                time.sleep(
                    max(
                        1,
                        retry_after,
                    )
                )
                continue

            if response.status_code >= 500:
                time.sleep(
                    2 * attempt
                )
                continue

            break

        except Exception as exc:
            last = {
                "ok": False,
                "description": str(exc),
            }

            time.sleep(
                2 * attempt
            )

    return last



def send_bot_api_fallback(image_path, rich_html):
    """Last-resort Bot API photo send with a safe caption length."""
    text = re.sub(r"<br\s*/?>", "\n", rich_html, flags=re.I)
    text = re.sub(r"</(p|h1|h2|h3|footer|summary|details|tr|td)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > 900:
        text = text[:900].rsplit(" ", 1)[0].rstrip() + "..."

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        with open(image_path, "rb") as photo:
            response = session.post(
                url,
                data={"chat_id": TELEGRAM_CHANNEL, "caption": text},
                files={"photo": photo},
                timeout=90,
            )
        return response.json()
    except Exception as exc:
        return {"ok": False, "description": str(exc)}

def send_rich_photo(
    image_path,
    rich_html,
):
    rich_message = {
        "html": rich_html,
        "media": [
            {
                "id": "newsphoto",
                "media": {
                    "type": "photo",
                    "media": "attach://photo",
                },
            }
        ],
        "skip_entity_detection": False,
    }

    with open(
        image_path,
        "rb",
    ) as photo:

        return telegram_call(
            "sendRichMessage",
            data={
                "chat_id": TELEGRAM_CHANNEL,
                "rich_message": json.dumps(
                    rich_message,
                    ensure_ascii=False,
                ),
            },
            files={
                "photo": photo
            },
        )


# ============================================================
# KNOWLEDGE / EVENT RECORD
# ============================================================

def make_event_id(
    story,
):
    event_key = safe_text(
        story.get(
            "event_key"
        )
    )

    if event_key:
        return (
            re.sub(
                r"[^a-z0-9]+",
                "_",
                event_key.lower(),
            ).strip("_")
        )

    return canonical_url(
        story["url"]
    )


def store_event(
    story,
    published=False,
    message_id=None,
):
    event_id = make_event_id(
        story
    )

    event = {
        "event_id": event_id,
        "canonical_url": story[
            "canonical"
        ],
        "original_url": story[
            "url"
        ],
        "source": story[
            "source"
        ],
        "region": story[
            "region"
        ],
        "topic": story.get(
            "topic",
            "",
        ),
        "institution": story.get(
            "institution",
            "",
        ),
        "event_cluster_id": story.get(
            "event_cluster_id",
            event_id,
        ),
        "event_confidence": story.get(
            "event_confidence",
            0,
        ),
        "event_source_count": story.get(
            "event_source_count",
            0,
        ),
        "headline": story[
            "headline"
        ],
        "summary": story[
            "summary"
        ],
        "highlights": story[
            "highlights"
        ],
        "concepts": story.get(
            "concepts",
            [],
        ),
        "key_numbers": story.get(
            "key_numbers",
            [],
        ),
        "published_at": story[
            "published_date"
        ],
        "selected_at": now_iso(),
        "status": (
            "published"
            if published
            else "selected"
        ),
        "message_id": message_id,
    }

    STATE["events"][
        event_id
    ] = event

    return event_id


# ============================================================
# ============================================================



# ============================================================
# VERSION 1 FALLBACK POOLS
# ============================================================

def build_candidate_pool(ranked, needed):
    """Build a verification pool that favors important stories and sector diversity.

    Diversity is a soft preference. A high-scoring story always beats a low-scoring story,
    and no sector is forced when the latest news does not support it.
    """
    if not ranked:
        return []

    eligible = [dict(x) for x in ranked if x.get("importance_score", 0) >= 7 and x.get("important") is True]
    target = max(RANKING_POOL_SIZE, needed * 2)
    target = min(target, len(eligible))

    # Preferred high-signal topics. These are not quotas; they only help break ties.
    preferred = {
        "GitHub Trends": 0,
        "Startups": 0,
        "Acquisitions and Mergers": 0,
        "AI Models and Products": 0,
        "Hugging Face": 0,
        "Cybersecurity": 0,
        "Major Outages": 0,
        "Operating Systems": 0,
        "Smartphones": 0,
        "Major Tech Companies": 0,
    }

    selected = []
    used_topics = set()
    remaining = list(eligible)

    # First pass: preserve the best story from distinct important sectors.
    for item in remaining:
        topic = item.get("topic", "")
        if topic not in used_topics and len(selected) < target:
            selected.append(item)
            used_topics.add(topic)

    # Second pass: fill by global editorial rank. No quota is imposed.
    for item in remaining:
        if len(selected) >= target:
            break
        if item not in selected:
            selected.append(item)

    selected.sort(key=lambda x: (
        -x.get("importance_score", 0),
        x.get("editor_rank", 9999),
    ))
    return selected


VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "unsupported_claims": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 3,
        },
    },
    "required": ["supported", "unsupported_claims"],
    "additionalProperties": False,
}


def claims_grounded(story, article_text):
    """Second-pass editorial verification for non-numeric factual claims."""
    claims = [
        story.get("headline", ""),
        story.get("summary", ""),
        *story.get("highlights", []),
    ]
    claims = [safe_text(x) for x in claims if safe_text(x)]

    prompt = """
You are a strict fact-checking editor. Compare the generated claims with the source article.
Mark supported=true only if every material factual claim in the headline, summary and highlights
is directly supported by the source article, either explicitly or by a faithful paraphrase.
Do not reject normal wording changes. Reject invented facts, unsupported causal claims, wrong dates,
wrong institutions, wrong people, wrong figures, exaggerated rankings, or claims stronger than the source.
Return only the JSON schema.
"""

    user = (
        "SOURCE ARTICLE:\n" + article_text[:12000]
        + "\n\nGENERATED CLAIMS:\n- " + "\n- ".join(claims)
    )

    try:
        response = get_cerebras().chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "story_claim_verification",
                    "strict": True,
                    "schema": VERIFY_SCHEMA,
                },
            },
            reasoning_effort="low",
            temperature=0.0,
            max_completion_tokens=500,
        )
        data = json.loads(safe_text(response.choices[0].message.content))
        return bool(data.get("supported")), data.get("unsupported_claims", [])
    except Exception as exc:
        logger.warning("Claim verification failed: %s", exc)
        # Verification infrastructure failure must not silently become a hard drop.
        # Numeric grounding remains mandatory; this pass is advisory on verifier outage.
        return True, []




# ============================================================
# CAREER NEWSROOM V1 OVERRIDES
# ============================================================

TELEGRAM_CHANNEL = (os.environ.get("TELEGRAM_CHANNEL") or "@CareerNewsroom").strip()
MAX_STORIES_PER_RUN = 6
RANKING_POOL_SIZE = 30
DISCOVERY_LOOKBACK_HOURS = 72
ROLLING_DISCOVERY_HOURS = 72
FUTURE_TOLERANCE_MINUTES = 10
QUEUE_RETENTION_DAYS = 4
EVENT_RETENTION_DAYS = 45
MAX_RSS_CANDIDATES = 300
MAX_EXA_CANDIDATES = 80
MAX_GOOGLE_NEWS_CANDIDATES = 80
THIN_EXCERPT_CHARS = 180
MAX_EXCERPT_ENRICH = 18
MAX_SOURCE_PER_RUN = 99
MAX_RICH_CHARACTERS = 32768
DEADLINE_MIN_DAYS = 7

CAREER_SOURCE_INFO = {
    "projobsbd.com": "ProjobsBD",
    "bdgovtjobs.com": "BD Govt Jobs",
    "jobpagol.com": "JobPagol",
    "bd-pratidin.com": "Bangladesh Pratidin",
    "jagonews24.com": "JagoNews24",
    "banglatribune.com": "Bangla Tribune",
    "bd24live.com": "BD24Live",
    "risingbd.com": "RisingBD",
    "bd-journal.com": "Bangladesh Journal",
    "prothomalo.com": "Prothom Alo",
    "jugantor.com": "Jugantor",
    "kalerkantho.com": "Kaler Kantho",
    "thedailystar.net": "The Daily Star",
    "ittefaq.com.bd": "The Daily Ittefaq",
    "bdjobs.com": "Bdjobs",
    "dohaj.com": "Dohaj",
    "job.com.bd": "Job.com.bd",
    "smartjob.portal.gov.bd": "Smart Job",
    "alljobs.teletalk.com.bd": "Alljobs Teletalk",
    "jobs.teletalk.com.bd": "Teletalk Jobs",
    "bpsc.gov.bd": "Bangladesh Public Service Commission",
    "erecruitment.bcc.gov.bd": "BCC e-Recruitment",
    "jobsnoticebd.com": "JobsNoticeBD",
    "jobsinfo.bd": "JobsInfo",
    "jobfeeds.online": "JobFeeds",
    "circularbd.com": "CircularBD",
    "bangladesherkhabor.net": "Bangladesher Khabor",
    "dhakapost.com": "Dhaka Post",
    "dhakatribune.com": "Dhaka Tribune",
    "bdjobslive.com": "BDJobs Live",
}

CAREER_ALLOWED_DOMAINS = sorted(CAREER_SOURCE_INFO.keys())
PRIMARY_CAREER_DOMAINS = CAREER_ALLOWED_DOMAINS
FALLBACK_CAREER_DOMAINS = []
ALL_PRIMARY_DOMAINS = PRIMARY_CAREER_DOMAINS
ALL_FALLBACK_DOMAINS = FALLBACK_CAREER_DOMAINS
ALL_ALLOWED_DOMAINS = ALL_PRIMARY_DOMAINS
SOURCE_NAMES = CAREER_SOURCE_INFO.copy()

RSS_FEEDS = [
    {"name": "ProjobsBD", "region": "Career", "url": "https://projobsbd.com/feed/"},
    {"name": "ProjobsBD Running Jobs", "region": "Career", "url": "https://projobsbd.com/category/running-job-circular/feed/"},
    {"name": "BD Govt Jobs", "region": "Career", "url": "https://bdgovtjobs.com/feed/"},
    {"name": "JobPagol", "region": "Career", "url": "https://jobpagol.com/feed/"},
    {"name": "Bangladesh Pratidin Jobs", "region": "Career", "url": "https://www.bd-pratidin.com/rss/online/1"},
    {"name": "JagoNews24 Jobs", "region": "Career", "url": "https://www.jagonews24.com/rss/rss.xml"},
    {"name": "Bangla Tribune Jobs", "region": "Career", "url": "https://www.banglatribune.com/feed"},
    {"name": "BD24Live", "region": "Career", "url": "https://www.bd24live.com/bangla/feed"},
    {"name": "RisingBD", "region": "Career", "url": "https://www.risingbd.com/rss/rss.xml"},
    {"name": "Bangladesh Journal", "region": "Career", "url": "https://www.bd-journal.com/feed/latest-rss.xml"},
    {"name": "Prothom Alo", "region": "Career", "url": "https://www.prothomalo.com/feed/"},
    {"name": "Jugantor", "region": "Career", "url": "https://www.jugantor.com/feed/rss.xml"},
    {"name": "Kaler Kantho", "region": "Career", "url": "https://www.kalerkantho.com/rss.xml"},
    {"name": "The Daily Star", "region": "Career", "url": "https://www.thedailystar.net/frontpage/rss.xml"},
    {"name": "The Daily Ittefaq", "region": "Career", "url": "https://www.ittefaq.com.bd/rss.xml"},
]

TOPICS = {
    "Career": [
        "Government Jobs", "Bank Jobs", "Corporate Jobs", "NGO Jobs",
        "Education Jobs", "Healthcare Jobs", "Engineering Jobs", "IT Jobs",
        "Sales and Marketing Jobs", "Finance and Accounting Jobs", "HR Jobs",
        "Internships", "Management Trainee", "Graduate Jobs", "Remote Jobs",
        "Factory and Manufacturing Jobs", "Legal Jobs", "Research Jobs",
    ]
}

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "from", "with", "by", "at", "as",
    "is", "are", "was", "were", "be", "been", "being", "has", "have", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "can", "this", "that", "these", "those", "it", "its",
    "their", "they", "them", "he", "she", "his", "her", "we", "our", "you", "your", "new", "after",
    "before", "over", "into", "than", "about",
}

BANGLA_MONTHS = {
    "জানুয়ারি": 1, "জানুয়ারি": 1, "ফেব্রুয়ারি": 2, "ফেব্রুয়ারি": 2, "মার্চ": 3, "এপ্রিল": 4,
    "মে": 5, "জুন": 6, "জুলাই": 7, "আগস্ট": 8, "সেপ্টেম্বর": 9, "অক্টোবর": 10, "নভেম্বর": 11, "ডিসেম্বর": 12,
}
EN_MONTHS = {name.lower(): idx for idx, names in enumerate([
    ("January", "Jan"), ("February", "Feb"), ("March", "Mar"), ("April", "Apr"), ("May",), ("June", "Jun"),
    ("July", "Jul"), ("August", "Aug"), ("September", "Sep", "Sept"), ("October", "Oct"),
    ("November", "Nov"), ("December", "Dec"),
], start=1) for name in names}

JOB_SIGNAL_RE = re.compile(
    r"\b(job|jobs|vacancy|vacancies|recruit|recruitment|career|careers|hiring|hire|apply|application|"
    r"circular|appointment|employment|internship|intern|management trainee|officer|executive|"
    r"teacher|lecturer|engineer|accountant|analyst|associate|manager|বাংলাদেশি|নিয়োগ|নিয়োগ|চাকরি|"
    r"চাকরির|নিয়োগ বিজ্ঞপ্তি|নিয়োগ বিজ্ঞপ্ত|আবেদন|পদ|শূন্যপদ)\b|"
    r"(চাকরি|নিয়োগ|আবেদন|শূন্যপদ)", re.I
)
OVERSEAS_RE = re.compile(
    r"\b(india|indian|pakistan|pakistani|nepal|sri lanka|uae|dubai|saudi arabia|qatar|kuwait|oman|"
    r"bahrain|malaysia|singapore|japan|south korea|uk|united kingdom|usa|united states|canada|australia|"
    r"germany|italy|france|europe|overseas|thailand|russia|maldives)\b", re.I
)
BANGLADESH_RE = re.compile(
    r"\b(bangladesh|bangladeshi|dhaka|chattogram|chittagong|rajshahi|khulna|barisal|sylhet|rangpur|mymensingh|"
    r"comilla|cumilla|gazipur|narayanganj|jashore|jessore|bogura|bogra|patuakhali|naogaon|natore|dinajpur|"
    r"cox.?s bazar|bangladesh government|govt of bangladesh|bangladesh citizen|citizens of bangladesh)\b|"
    r"(বাংলাদেশ|ঢাকা|চট্টগ্রাম|রাজশাহী|খুলনা|বরিশাল|সিলেট|রংপুর|ময়মনসিংহ|নিয়োগ|বাংলাদেশি নাগরিক)", re.I
)
BAD_JOB_RE = re.compile(
    r"\b(job fair|career fair|seminar|webinar|workshop|training course|career advice|how to get a job|"
    r"salary guide|job preparation|result|admit card|exam schedule|exam result|answer key|scholarship only|"
    r"sponsored|advertisement|opinion|editorial)\b", re.I
)

def normalized_domain(url_or_source):
    raw = safe_text(url_or_source).lower()
    if "://" in raw:
        raw = urlparse(raw).netloc
    return raw.split(":")[0].removeprefix("www.").strip().rstrip("/")


def source_name(url):
    domain = normalized_domain(url)
    for allowed, label in CAREER_SOURCE_INFO.items():
        if domain == allowed or domain.endswith("." + allowed):
            return label
    return domain or "Source"


def display_source_name(source):
    return safe_text(source).strip() or "Source"


def source_homepage(source, article_url=""):
    domain_map = {name: f"https://{domain}" for domain, name in CAREER_SOURCE_INFO.items()}
    domain_map.update({
        "Smart Job": "https://smartjob.portal.gov.bd/",
        "Alljobs Teletalk": "https://alljobs.teletalk.com.bd/",
        "Teletalk Jobs": "https://jobs.teletalk.com.bd/",
        "Bangladesh Public Service Commission": "https://bpsc.gov.bd/",
        "BCC e-Recruitment": "https://erecruitment.bcc.gov.bd/",
    })
    if source in domain_map:
        return domain_map[source]
    host = normalized_domain(article_url)
    return f"https://{host}" if host else ""

def article_region(url):
    return "Career"


def career_now():
    return datetime.now(BD_TZ)


def refresh_career_window():
    global NOW_BD, DISCOVERY_START, DISCOVERY_END, TODAY_START, YESTERDAY_START
    NOW_BD = career_now()
    TODAY_START = NOW_BD.replace(hour=0, minute=0, second=0, microsecond=0)
    YESTERDAY_START = TODAY_START - timedelta(days=1)
    DISCOVERY_START = NOW_BD - timedelta(hours=DISCOVERY_LOOKBACK_HOURS)
    DISCOVERY_END = NOW_BD + timedelta(minutes=FUTURE_TOLERANCE_MINUTES)


def canonical_topic(topic, region="Career"):
    raw = safe_text(topic).strip()
    aliases = {
        "govt": "Government Jobs", "government": "Government Jobs", "government job": "Government Jobs",
        "bank": "Bank Jobs", "banking": "Bank Jobs", "private": "Corporate Jobs", "corporate": "Corporate Jobs",
        "ngo": "NGO Jobs", "education": "Education Jobs", "teacher": "Education Jobs",
        "engineering": "Engineering Jobs", "it": "IT Jobs", "technology": "IT Jobs",
        "sales": "Sales and Marketing Jobs", "marketing": "Sales and Marketing Jobs",
        "finance": "Finance and Accounting Jobs", "accounting": "Finance and Accounting Jobs",
        "hr": "HR Jobs", "internship": "Internships", "management trainee": "Management Trainee",
        "graduate": "Graduate Jobs", "remote": "Remote Jobs",
    }
    key = raw.lower()
    if key in aliases:
        return aliases[key]
    for topic_name in TOPICS["Career"]:
        if topic_name.lower() == key:
            return topic_name
    return "Corporate Jobs"

def parse_date_text(raw):
    text = safe_text(raw).strip()
    if not text:
        return None
    text = (text.replace("০", "0").replace("১", "1").replace("২", "2").replace("৩", "3")
                .replace("৪", "4").replace("৫", "5").replace("৬", "6").replace("৭", "7")
                .replace("৮", "8").replace("৯", "9"))
    dt = parse_datetime(text)
    if dt:
        return dt.astimezone(BD_TZ)

    for name, month in EN_MONTHS.items():
        m = re.search(rf"\b(\d{{1,2}})\s+{re.escape(name)}(?:\s*,?\s*(\d{{4}}))?\b", text, re.I)
        if m:
            year = int(m.group(2) or career_now().year)
            try:
                return datetime(year, month, int(m.group(1)), tzinfo=BD_TZ)
            except ValueError:
                return None
        m = re.search(rf"\b{re.escape(name)}\s+(\d{{1,2}}),?\s*(20\d{{2}})\b", text, re.I)
        if m:
            try:
                return datetime(int(m.group(2)), month, int(m.group(1)), tzinfo=BD_TZ)
            except ValueError:
                return None

    for name, month in BANGLA_MONTHS.items():
        m = re.search(rf"(\d{{1,2}})\s*{re.escape(name)}\s*(\d{{4}})?", text)
        if m:
            year = int(m.group(2) or career_now().year)
            try:
                return datetime(year, month, int(m.group(1)), tzinfo=BD_TZ)
            except ValueError:
                return None

    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", text)
    if m:
        a, b, c = map(int, m.groups())
        year = c + 2000 if c < 100 else c
        candidates = [(a, b), (b, a)]
        for day, month in candidates:
            if 1 <= month <= 12:
                try:
                    return datetime(year, month, day, tzinfo=BD_TZ)
                except ValueError:
                    pass
    return None


def extract_deadline_candidates(text):
    text = safe_text(text)
    date_patterns = [
        r"\b(?:0?[1-9]|[12]\d|3[01])[\s./-](?:0?[1-9]|1[0-2])[\s./-](?:20\d{2}|\d{2})\b",
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember|t)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(?:0?[1-9]|[12]\d|3[01]),?\s+20\d{2}\b",
        r"\b(?:0?[1-9]|[12]\d|3[01])\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember|t)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+20\d{2}\b",
        r"\b(?:0?[1-9]|[12]\d|3[01])\s*(?:জানুয়ারি|জানুয়ারি|ফেব্রুয়ারি|ফেব্রুয়ারি|মার্চ|এপ্রিল|মে|জুন|জুলাই|আগস্ট|সেপ্টেম্বর|অক্টোবর|নভেম্বর|ডিসেম্বর)\s*20\d{2}\b",
    ]
    date_re = re.compile("|".join(f"(?:{x})" for x in date_patterns), re.I)
    marker_re = re.compile(r"(deadline|last date|closing date|application deadline|apply by|আবেদনের শেষ|শেষ তারিখ|শেষ সময়|আবেদনের শেষ সময়)", re.I)
    found = []
    for m in date_re.finditer(text):
        raw = m.group(0)
        dt = parse_date_text(raw)
        if not dt:
            continue
        lo = max(0, m.start() - 180)
        hi = min(len(text), m.end() + 180)
        context = text[lo:hi]
        date_pos = m.start() - lo
        distances = [abs(date_pos - mm.start()) for mm in marker_re.finditer(context)]
        if distances and min(distances) <= 150:
            score = 2
        elif marker_re.search(context):
            score = 1
        else:
            score = 0
        found.append((score, dt, raw))
    found.sort(key=lambda x: (-x[0], -x[1].timestamp()))
    return found


def choose_deadline(text):
    candidates = extract_deadline_candidates(text)
    if not candidates:
        return None
    return candidates[0][1]


def deadline_is_eligible(deadline_dt):
    """Require at least 7 full days remaining. Date-only deadlines count through 23:59:59."""
    if not deadline_dt:
        return False
    now = career_now()
    candidate = deadline_dt.astimezone(BD_TZ) if deadline_dt.tzinfo else deadline_dt.replace(tzinfo=BD_TZ)
    if candidate.hour == 0 and candidate.minute == 0 and candidate.second == 0 and candidate.microsecond == 0:
        candidate = candidate.replace(hour=23, minute=59, second=59, microsecond=0)
    return candidate >= now + timedelta(days=DEADLINE_MIN_DAYS) - timedelta(seconds=1)


def deadline_grounded(deadline_text, article_text):
    out_dt = parse_date_text(deadline_text)
    if not out_dt:
        return False
    candidates = extract_deadline_candidates(article_text)
    return any(dt.date() == out_dt.date() for _, dt, _ in candidates)


def posted_at_from_html(page_html):
    soup = BeautifulSoup(page_html or "", "html.parser")
    values = []
    for selector in [
        ("meta", {"property": "article:published_time"}),
        ("meta", {"name": "article:published_time"}),
        ("meta", {"itemprop": "datePublished"}),
        ("meta", {"name": "datePublished"}),
        ("time", {"itemprop": "datePublished"}),
    ]:
        for tag in soup.find_all(selector[0], attrs=selector[1]):
            value = tag.get("content") or tag.get("datetime") or tag.get_text(" ", strip=True)
            dt = parse_datetime(value)
            if dt:
                values.append(dt.astimezone(BD_TZ))
    for script in soup.find_all("script", type="application/ld+json"):
        raw = safe_text(script.string or script.get_text(" ", strip=True))
        if not raw:
            continue
        try:
            data = json.loads(raw)
            stack = data if isinstance(data, list) else [data]
            for obj in stack:
                if isinstance(obj, dict):
                    for key in ("datePublished", "dateCreated"):
                        dt = parse_datetime(obj.get(key, ""))
                        if dt:
                            values.append(dt.astimezone(BD_TZ))
        except Exception:
            continue
    if not values:
        return None
    return min(values)


def extract_apply_url(page_url, page_html):
    soup = BeautifulSoup(page_html or "", "html.parser")
    signals = re.compile(r"(apply now|apply here|apply online|apply|application|আবেদন|আবেদন করুন|এখনই আবেদন)", re.I)
    scored = []
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        href = safe_text(a.get("href"))
        if not href or href.startswith(("javascript:", "mailto:", "tel:")):
            continue
        score = 0
        if signals.search(label):
            score += 4
        if re.search(r"(apply|application|career|jobs|vacancy|recruit)", href, re.I):
            score += 2
        if score:
            scored.append((score, urljoin(page_url, href)))
    scored.sort(key=lambda x: (-x[0], len(x[1])))
    return scored[0][1] if scored else ""


def candidate_basic_allowed(item):
    title = safe_text(item.get("title"))
    url = safe_text(item.get("url"))
    excerpt = safe_text(item.get("excerpt"))
    if not title or not url:
        return False
    published_dt = parse_datetime(item.get("published_date", item.get("published_dt", "")))
    if item.get("date_estimated") or not published_dt or not (DISCOVERY_START <= published_dt <= DISCOVERY_END):
        return False
    if not primary_domain_allowed(url):
        return False
    blob = f"{title} {excerpt}"
    if BAD_JOB_RE.search(blob) or not JOB_SIGNAL_RE.search(blob):
        return False
    overseas = OVERSEAS_RE.search(blob)
    bangladesh = BANGLADESH_RE.search(blob)
    official = item.get("source_type") == "official_job_portal"
    known_bd_portal = normalized_domain(url) in {
        "bdjobs.com", "dohaj.com", "job.com.bd", "smartjob.portal.gov.bd",
        "alljobs.teletalk.com.bd", "jobs.teletalk.com.bd", "bpsc.gov.bd", "erecruitment.bcc.gov.bd"
    }
    # Foreign-only opportunities are always rejected. Bangladesh portals are allowed when
    # the listing itself does not repeat the country name, but explicit foreign locations win.
    if overseas and not bangladesh:
        return False
    if not (bangladesh or official or known_bd_portal):
        return False
    return True

def primary_domain_allowed(url, region=None):
    domain = normalized_domain(url)
    return any(domain == d or domain.endswith("." + d) for d in CAREER_ALLOWED_DOMAINS)


def fallback_domain_allowed(url, region=None):
    return False


def allowed_source_for_region(url, region=None):
    return primary_domain_allowed(url, region)


def resolve_google_news_url(link):
    try:
        response = session.get(link, timeout=10, allow_redirects=True, headers=HEADERS, stream=True)
        real_url = safe_text(response.url)
        response.close()
        if not real_url or "news.google.com" in real_url:
            return ""
        return real_url
    except Exception:
        return ""


GOOGLE_NEWS_QUERIES = {
    "Career": [
        'Bangladesh job circular recruitment hiring vacancy',
        'Bangladesh government job circular নিয়োগ চাকরি',
        'Bangladesh bank job circular recruitment',
        'Bangladesh private company recruitment hiring',
        'Bangladesh NGO job circular recruitment',
        'Bangladesh internship graduate management trainee',
        'Bangladesh teacher engineer accountant officer job',
        'Bangladesh careers vacancy apply deadline',
    ]
}
GOOGLE_NEWS_LOCALE = {"Career": ("bn-BD", "BD", "BD:bn")}


def google_news_gap_fill(region, existing_count, needed):
    if existing_count >= max(8, needed * 2):
        return 0
    added = 0
    for query in GOOGLE_NEWS_QUERIES["Career"]:
        try:
            feed_url = "https://news.google.com/rss/search?q=" + quote(f"{query} when:3d") + "&hl=bn-BD&gl=BD&ceid=BD:bn"
            response = session.get(feed_url, timeout=15, headers=HEADERS)
            if response.status_code >= 400:
                continue
            parsed = feedparser.parse(response.content)
            for entry in parsed.entries[:10]:
                title = safe_text(entry.get("title"))
                link = safe_text(entry.get("link"))
                published_dt = feed_entry_datetime(entry)
                if not title or not link or not published_dt:
                    continue
                real_url = resolve_google_news_url(link)
                if not real_url or not primary_domain_allowed(real_url):
                    continue
                item = {
                    "title": title,
                    "url": real_url,
                    "canonical": canonical_url(real_url),
                    "published_dt": published_dt.isoformat(),
                    "published_date": published_dt.isoformat(),
                    "source": source_name(real_url),
                    "source_type": "google_news",
                    "region": "Career",
                    "excerpt": BeautifulSoup(safe_text(entry.get("summary")), "html.parser").get_text(" ", strip=True)[:2500],
                    "image": "",
                    "discovery": "google_news",
                    "date_estimated": False,
                }
                if not candidate_basic_allowed(item):
                    continue
                if item["canonical"] in POSTED_URLS or item["canonical"] in STATE["queue"]:
                    continue
                queue_candidate(item)
                added += 1
                if added >= MAX_GOOGLE_NEWS_CANDIDATES:
                    return added
        except Exception as exc:
            logger.warning("Google News career gap fill failed: %s", exc)
    return added


def exa_gap_fill(region, existing_count, needed, fallback=False):
    if existing_count >= max(12, needed * 2):
        return 0
    queries = [
        'latest Bangladesh job circular recruitment vacancy deadline',
        'latest Bangladesh government recruitment job circular',
        'latest Bangladesh private company hiring vacancy',
        'latest Bangladesh bank NGO education IT job recruitment',
        'latest Bangladesh internship management trainee graduate job',
    ]
    added = 0
    for query in queries:
        try:
            results = get_exa().search_and_contents(
                query, type="auto", category="news", num_results=10,
                include_domains=CAREER_ALLOWED_DOMAINS,
                start_published_date=DISCOVERY_START.isoformat(),
                end_published_date=DISCOVERY_END.isoformat(),
                contents={"highlights": {"max_characters": 1200}},
            )
            for result in results.results:
                url = safe_text(getattr(result, "url", ""))
                title = safe_text(getattr(result, "title", ""))
                published_dt = parse_datetime(getattr(result, "published_date", ""))
                if not url or not title or not published_dt:
                    continue
                item = {
                    "title": title,
                    "url": url,
                    "canonical": canonical_url(url),
                    "published_dt": published_dt.astimezone(BD_TZ).isoformat(),
                    "published_date": published_dt.astimezone(BD_TZ).isoformat(),
                    "source": source_name(url),
                    "source_type": "exa",
                    "region": "Career",
                    "excerpt": safe_text(" ".join(getattr(result, "highlights", []) if isinstance(getattr(result, "highlights", []), list) else [getattr(result, "highlights", "")]))[:2500],
                    "image": safe_text(getattr(result, "image", "")),
                    "discovery": "exa",
                }
                if not candidate_basic_allowed(item):
                    continue
                if item["canonical"] in POSTED_URLS or item["canonical"] in STATE["queue"]:
                    continue
                queue_candidate(item)
                added += 1
                if added >= MAX_EXA_CANDIDATES:
                    return added
        except Exception as exc:
            logger.warning("Exa career discovery failed: %s", exc)
    return added


def queue_candidates_for_region(region):
    count = 0
    for item in STATE["queue"].values():
        published = parse_datetime(item.get("published_date"))
        if item.get("region") == region and item.get("status") == "pending" and published and DISCOVERY_START <= published <= DISCOVERY_END:
            count += 1
    return count


def _career_rank_prompt():
    return """
You are the editorial ranking engine for Telegram channel @CareerNewsroom.
Return EVERY candidate in JSON. Rank ONLY real job vacancies/recruitment opportunities relevant to people in Bangladesh.
The source item MUST have been published within the last 72 hours.
A candidate can be published only if the application deadline is at least 7 calendar days from today in Bangladesh.
Do not invent or infer a deadline. Missing/unclear deadline means reject later.
Reject job advice, preparation articles, exam/result news, scholarships with no vacancy, job fairs without a specific vacancy,
promotional training without a vacancy, expired jobs, overseas-only jobs, and duplicate versions of the same vacancy.
Prefer actual vacancy notices, official recruitment notices, and clear job circulars with identifiable company/organization and deadline.
Score 0-10: 9-10 exceptional usefulness/importance, 7-8 clearly publishable, 5-6 potentially useful but weak/duplicate/unclear, 0-4 reject.
Return fields: id, rank, score, important, topic, institution, event_key, reason.
"""


def _rank_batch(batch, region, batch_no):
    lines = []
    for idx, item in enumerate(batch, start=1):
        lines.append("\n".join([
            f"ID: {idx}",
            f"Title: {item.get('title', '')}",
            f"Source: {item.get('source', '')}",
            f"Published: {item.get('published_date', '')}",
            f"Excerpt: {trim_source_text(item.get('excerpt', ''), 1000)}",
            "",
        ]))
    try:
        response = get_cerebras().chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[
                {"role": "system", "content": _career_rank_prompt()},
                {"role": "user", "content": "\n".join(lines)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": f"career_rank_{batch_no}", "strict": True, "schema": RANK_SCHEMA},
            },
            reasoning_effort="low", temperature=0.0, max_completion_tokens=3500,
        )
        return json.loads(safe_text(response.choices[0].message.content)).get("ranked", [])
    except Exception as exc:
        logger.error("Career ranking batch %d failed: %s", batch_no, exc)
        return []


def rank_candidates(candidates, region):
    if not candidates:
        return []
    regional = sorted(candidates, key=lambda x: parse_datetime(x.get("published_date")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:100]
    ranked_rows = []
    for offset in range(0, len(regional), 15):
        batch = regional[offset:offset + 15]
        rows = _rank_batch(batch, region, offset // 15 + 1)
        by_id = {idx: item for idx, item in enumerate(batch, start=1)}
        for row in rows:
            try:
                idx = int(row["id"])
                score = max(0, min(10, int(row.get("score", 0))))
            except Exception:
                continue
            if idx not in by_id:
                continue
            item = dict(by_id[idx])
            item.update({
                "importance_score": score,
                "important": bool(row.get("important")) and score >= 7,
                "topic": safe_text(row.get("topic")) or "Corporate Jobs",
                "institution": safe_text(row.get("institution")),
                "event_key": safe_text(row.get("event_key")),
                "rank_reason": safe_text(row.get("reason")),
                "batch_rank": int(row.get("rank", 9999)),
            })
            ranked_rows.append(item)
    ranked_rows.sort(key=lambda x: (-x.get("importance_score", 0), x.get("batch_rank", 9999), -(parse_datetime(x.get("published_date")).timestamp() if parse_datetime(x.get("published_date")) else 0)))
    for rank, item in enumerate(ranked_rows, start=1):
        item["editor_rank"] = rank
    return ranked_rows


def is_already_published_candidate(item):
    canonical = safe_text(item.get("canonical"))
    if canonical and canonical in POSTED_URLS:
        return True
    title = safe_text(item.get("title"))
    for event in STATE.get("events", {}).values():
        if event.get("status") != "published":
            continue
        event_time = parse_datetime(event.get("selected_at") or event.get("published_at"))
        if not event_time or (career_now() - event_time).total_seconds() > EVENT_RETENTION_DAYS * 86400:
            continue
        if title and title_similarity(title, safe_text(event.get("headline"))) >= 0.88:
            return True
        if canonical and canonical == safe_text(event.get("canonical_url")):
            return True
        ek = safe_text(item.get("event_key"))
        if ek and ek == safe_text(event.get("event_key")):
            return True
    return False


def collapse_event_clusters(ranked):
    clusters = []
    for item in ranked:
        if item.get("importance_score", 0) < 7 or not item.get("important"):
            continue
        matched = None
        ek = normalize_title(item.get("event_key", ""))
        for cluster in clusters:
            rep = cluster[0]
            rep_ek = normalize_title(rep.get("event_key", ""))
            same_key = ek and rep_ek and ek == rep_ek
            similar = title_similarity(item.get("title", ""), rep.get("title", "")) >= 0.86
            if same_key or similar:
                matched = cluster
                break
        if matched is None:
            clusters.append([item])
        else:
            matched.append(item)
    result = []
    for cluster in clusters:
        best = sorted(cluster, key=lambda x: (-x.get("importance_score", 0), -len(x.get("excerpt", "")), x.get("editor_rank", 9999)))[0]
        best = dict(best)
        best["event_source_count"] = len(cluster)
        best["event_cluster_id"] = hashlib.sha1("|".join(sorted(x.get("canonical", "") for x in cluster)).encode("utf-8")).hexdigest()[:16]
        result.append(best)
    result.sort(key=lambda x: (-x.get("importance_score", 0), x.get("editor_rank", 9999)))
    return result


def build_candidate_pool(ranked, needed):
    pool = [x for x in ranked if x.get("importance_score", 0) >= 7 and x.get("important")]
    return pool[:max(needed * 3, needed)]


def posted_date_is_valid(item, article_text="", page_posted_date=None):
    dt = page_posted_date or parse_datetime(item.get("published_date"))
    if not dt:
        return False
    if not (DISCOVERY_START <= dt <= DISCOVERY_END):
        return False
    return True


CAREER_STORY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "company": {"type": "string"},
        "location": {"type": "string"},
        "job_type": {"type": "string"},
        "education": {"type": "string"},
        "experience": {"type": "string"},
        "salary": {"type": "string"},
        "deadline": {"type": "string"},
        "suitable_for": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 5},
        "highlights": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 5},
        "apply_url": {"type": "string"},
        "source": {"type": "string"},
        "bold_terms": {"type": "array", "items": {"type": "string"}, "maxItems": 18},
    },
    "required": ["headline", "company", "location", "job_type", "education", "experience", "salary", "deadline", "suitable_for", "highlights", "apply_url", "source", "bold_terms"],
    "additionalProperties": False,
}


def generate_story(item, article_text):
    prompt = """
You are a senior Bangladesh jobs editor for @CareerNewsroom.
Create one factual Telegram job vacancy card from the supplied source article/page.
Only return JSON matching the schema.

Rules:
- Headline: the actual job title, 3-12 words, title case or source wording.
- Company: exact employer/organization name. Never invent.
- Location: exact location from source, or Remote only when explicitly stated.
- Type: concise category such as Full-time, Part-time, Internship, Management Trainee, Government.
- Education: concise eligibility. Do not invent degree requirements.
- Experience: concise eligibility. Use Fresh graduates / No experience only when source supports it.
- Salary: exact source wording, or Not specified.
- Deadline: exact application deadline from source. Never guess. Use DD Month YYYY.
- Suitable For: 1-5 concise audiences strictly supported by education/experience/type.
- Highlights: 3-5 concise factual points. Include vacancy count when explicitly available.
- Apply URL: actual application URL if present, otherwise the source page URL.
- Source: publication/source name.
- No hashtags, Markdown or HTML in JSON fields.
"""
    user = (
        f"SOURCE: {item.get('source','')}\nTITLE: {item.get('title','')}\nPUBLISHED: {item.get('published_date','')}\n"
        f"ARTICLE/PAGE:\n{article_text[:14000]}\n"
        f"DISCOVERED APPLY URL: {item.get('apply_url','') }\n"
        f"DISCOVERED DEADLINE: {item.get('deadline_hint','') }"
    )
    for attempt in range(3):
        try:
            response = get_cerebras().chat.completions.create(
                model=CEREBRAS_MODEL,
                messages=[{"role": "system", "content": prompt}, {"role": "user", "content": user}],
                response_format={"type": "json_schema", "json_schema": {"name": "career_job_story_v1", "strict": True, "schema": CAREER_STORY_SCHEMA}},
                reasoning_effort="low", temperature=0.1, max_completion_tokens=1800,
            )
            data = json.loads(safe_text(response.choices[0].message.content))
            story = {
                **item,
                "headline": trim_source_text(clean_generated_text(data.get("headline")), 120),
                "company": trim_source_text(clean_generated_text(data.get("company")), 120),
                "location": trim_source_text(clean_generated_text(data.get("location")), 120),
                "job_type": trim_source_text(clean_generated_text(data.get("job_type")), 80),
                "education": trim_source_text(clean_generated_text(data.get("education")), 240),
                "experience": trim_source_text(clean_generated_text(data.get("experience")), 180),
                "salary": trim_source_text(clean_generated_text(data.get("salary")), 120),
                "deadline": trim_source_text(clean_generated_text(data.get("deadline")), 80),
                "suitable_for": [trim_source_text(clean_generated_text(x), 80) for x in data.get("suitable_for", []) if clean_generated_text(x)],
                "highlights": [trim_source_text(clean_generated_text(x), 150) for x in data.get("highlights", []) if clean_generated_text(x)],
                "apply_url": safe_text(data.get("apply_url")) or safe_text(item.get("apply_url")) or safe_text(item.get("url")),
                "source": safe_text(data.get("source")) or safe_text(item.get("source")),
                "bold_terms": [safe_text(x) for x in data.get("bold_terms", []) if safe_text(x)],
            }
            if not story["headline"] or not story["company"] or not story["location"] or not story["deadline"]:
                raise ValueError("Missing required career fields")
            if not (1 <= len(story["suitable_for"]) <= 5 and 3 <= len(story["highlights"]) <= 5):
                raise ValueError("Invalid Suitable For / Highlights length")
            return story
        except Exception as exc:
            logger.warning("Career story generation attempt %d failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(1)
    return None


def extract_article(item):
    url = item["url"]
    page_posted = None
    apply_url = safe_text(item.get("apply_url"))
    try:
        response = session.get(url, headers={**HEADERS, "Referer": url}, timeout=25)
        if response.status_code < 400:
            page_html = response.text
            final_url = response.url
            page_posted = posted_at_from_html(page_html)
            apply_url = apply_url or extract_apply_url(final_url, page_html)
            deadline_hint_dt = choose_deadline(page_html)
            if deadline_hint_dt:
                item["deadline_hint"] = deadline_hint_dt.strftime("%d %B %Y")
            item["deadline_evidence"] = BeautifulSoup(page_html, "html.parser").get_text(" ", strip=True)[:20000]
            item["page_posted_date"] = page_posted.isoformat() if page_posted else ""
            item["apply_url"] = apply_url
            text = trafilatura.extract(page_html, include_comments=False, include_tables=False, favor_precision=True)
            image_candidates = find_image_candidates(url, page_html, final_url, preferred_image=item.get("image", ""))
            if text and len(safe_text(text)) >= 500:
                return safe_text(text), image_candidates
    except Exception as exc:
        logger.warning("Career local extraction failed %s: %s", url, exc)
    try:
        result_set = get_exa().get_contents([url], text={"max_characters": 14000})
        if result_set.results:
            result = result_set.results[0]
            text = safe_text(getattr(result, "text", ""))
            exa_image = safe_text(getattr(result, "image", ""))
            image_candidates = find_image_candidates(url, preferred_image=item.get("image", "") or exa_image)
            if exa_image and exa_image not in image_candidates:
                image_candidates.append(exa_image)
            if text:
                item["apply_url"] = apply_url or safe_text(item.get("url"))
                return text, image_candidates
    except Exception as exc:
        logger.warning("Career Exa extraction failed %s: %s", url, exc)
    return "", [item.get("image", "")] if item.get("image") else []


def derive_bold_terms(story):
    fields = [story.get("headline", ""), story.get("company", ""), story.get("location", ""), story.get("education", ""), story.get("experience", ""), story.get("salary", ""), story.get("deadline", "")]
    terms = []
    for term in story.get("bold_terms", []):
        clean = safe_text(term).strip()
        if clean and clean.lower() not in {x.lower() for x in terms}:
            terms.append(clean)
    for field in fields:
        for match in re.findall(r"\b[A-Z][A-Za-z0-9&.-]{2,}(?:\s+[A-Z][A-Za-z0-9&.-]{2,}){0,2}", safe_text(field)):
            if match not in terms:
                terms.append(match)
    for required in [story.get("company", ""), story.get("headline", "")]:
        if required and required not in terms:
            terms.insert(0, required)
    return terms[:20]


def category_hashtags(story):
    tags = ["#CareerNewsroom"]
    mapping = {
        "Government": "#GovernmentJob", "Bank": "#BankJob", "Intern": "#Internship", "Trainee": "#ManagementTrainee",
        "IT": "#ITJob", "Engineering": "#EngineeringJob", "Teacher": "#TeachingJob", "NGO": "#NGOJob",
        "Remote": "#RemoteJob", "Fresh": "#FreshGraduate",
    }
    blob = " ".join(str(story.get(k, "")) for k in ("headline", "job_type", "education", "experience", "suitable_for"))
    for key, tag in mapping.items():
        if re.search(re.escape(key), blob, re.I) and tag not in tags:
            tags.append(tag)
    for tag in ["#BBA", "#MBA", "#Graduate", "#EntryLevel"]:
        if re.search(tag[1:], blob, re.I) and tag not in tags:
            tags.append(tag)
    return tags[:6]


def escape_rich_html(text):
    return html.escape(safe_text(text), quote=False)


def bold_terms_html(text, terms):
    raw = safe_text(text)
    replacements = []
    for idx, term in enumerate(sorted([x for x in terms if safe_text(x)], key=len, reverse=True)):
        pattern = re.compile(re.escape(term), re.I)
        match = pattern.search(raw)
        if not match:
            continue
        marker = f"__CAREER_BOLD_{idx}__"
        original = match.group(0)
        raw = raw[:match.start()] + marker + raw[match.end():]
        replacements.append((marker, original))
    result = html.escape(raw, quote=False)
    for marker, original in replacements:
        result = result.replace(marker, "<b>" + html.escape(original, quote=False) + "</b>", 1)
    return result


def dynamic_rich_html(story):
    terms = derive_bold_terms(story)
    lines = [
        '<img src="tg://photo?id=newsphoto">',
        "<h1>📣 " + escape_rich_html(story["headline"]) + "</h1>",
        "<p>🏢 <b>Company:</b> " + bold_terms_html(story.get("company", "Not specified"), terms) + "</p>",
        "<p>📍 <b>Location:</b> " + bold_terms_html(story.get("location", "Not specified"), terms) + "</p>",
        "<p>💼 <b>Type:</b> " + bold_terms_html(story.get("job_type", "Not specified"), terms) + "</p>",
        "<p>🎓 <b>Education:</b> " + bold_terms_html(story.get("education", "Not specified"), terms) + "</p>",
        "<p>👨‍💼 <b>Experience:</b> " + bold_terms_html(story.get("experience", "Not specified"), terms) + "</p>",
        "<p>💰 <b>Salary:</b> " + bold_terms_html(story.get("salary", "Not specified"), terms) + "</p>",
        "<p>📅 <b>Deadline:</b> " + bold_terms_html(story.get("deadline", "Not specified"), terms) + "</p>",
        "<h2>🎯 Suitable For</h2>",
        "<p>" + "<br>".join("• " + bold_terms_html(x, terms) for x in story.get("suitable_for", [])) + "</p>",
        "<h2>📌 Key Highlights</h2>",
        "<p>" + "<br>".join("• " + bold_terms_html(x, terms) for x in story.get("highlights", [])) + "</p>",
        "<h2>📝 Apply Now</h2>",
        "<p>" + f'<a href="{html.escape(story.get("apply_url") or story.get("url", ""), quote=True)}">Apply Here</a>' + "</p>",
        "<h2>🔎 Source</h2>",
        "<p>" + f'<a href="{html.escape(story.get("url", ""), quote=True)}">{escape_rich_html(story.get("source", "Source"))}</a>' + "</p>",
    ]
    hashtags = " ".join(category_hashtags(story))
    if hashtags:
        lines.append("<p>" + escape_rich_html(hashtags) + "</p>")
    return "\n".join(lines)


def fit_rich_html(story):
    variants = [260, 220, 190, 165]
    for limit in variants:
        candidate = dict(story)
        candidate["education"] = trim_source_text(story.get("education", ""), limit)
        candidate["experience"] = trim_source_text(story.get("experience", ""), 160)
        candidate["highlights"] = [trim_source_text(x, 145) for x in story.get("highlights", [])]
        candidate["suitable_for"] = [trim_source_text(x, 75) for x in story.get("suitable_for", [])]
        rendered = dynamic_rich_html(candidate)
        if rich_visible_length(rendered) <= MAX_RICH_CHARACTERS:
            return rendered
    return dynamic_rich_html(story)




def career_numeric_grounded(story, article_text):
    source = {normalize_number(x) for x in numeric_tokens(article_text)}
    fields = [story.get("headline", ""), story.get("company", ""), story.get("location", ""),
              story.get("education", ""), story.get("experience", ""), story.get("salary", ""),
              story.get("deadline", ""), *story.get("suitable_for", []), *story.get("highlights", [])]
    for token in numeric_tokens(" ".join(fields)):
        norm = normalize_number(token)
        if norm and norm not in source:
            return False, token
    return True, ""


def career_claims_grounded(story, article_text):
    claims = [
        f"Job title: {story.get('headline','')}",
        f"Company: {story.get('company','')}",
        f"Location: {story.get('location','')}",
        f"Type: {story.get('job_type','')}",
        f"Education: {story.get('education','')}",
        f"Experience: {story.get('experience','')}",
        f"Salary: {story.get('salary','')}",
        f"Deadline: {story.get('deadline','')}",
        *story.get("highlights", []),
    ]
    prompt = """You are a strict recruitment fact-checking editor. Compare every generated job field with the source.
Return supported=true only when all material facts are directly supported by the source text. Reject invented employer, location,
degree, experience, salary, vacancy count, deadline, job type, eligibility or application instructions. Faithful paraphrases are allowed.
Return only the supplied JSON schema."""
    try:
        response = get_cerebras().chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "SOURCE:\n" + article_text[:14000] + "\n\nGENERATED:\n- " + "\n- ".join(claims)},
            ],
            response_format={"type": "json_schema", "json_schema": {"name": "career_claim_verification", "strict": True, "schema": VERIFY_SCHEMA}},
            reasoning_effort="low", temperature=0.0, max_completion_tokens=600,
        )
        data = json.loads(safe_text(response.choices[0].message.content))
        return bool(data.get("supported")), data.get("unsupported_claims", [])
    except Exception as exc:
        logger.warning("Career claim verification unavailable: %s", exc)
        return True, []


def process_story_candidate(item):
    article_text, image_candidates = extract_article(item)
    if not article_text:
        logger.warning("DROP extraction: %s", item.get("title"))
        return None

    page_dt = parse_datetime(item.get("page_posted_date"))
    if page_dt and not (DISCOVERY_START <= page_dt <= DISCOVERY_END):
        logger.info("DROP outside 72-hour page publication window: %s", item.get("title"))
        return None
    if not posted_date_is_valid(item, article_text, page_dt):
        logger.info("DROP outside 72-hour publication window: %s", item.get("title"))
        return None

    detected_deadline = parse_date_text(item.get("deadline_hint", "")) or choose_deadline(article_text)
    if not detected_deadline or not deadline_is_eligible(detected_deadline):
        logger.info("DROP deadline < %dd or missing: %s", DEADLINE_MIN_DAYS, item.get("title"))
        return None
    item["deadline_hint"] = detected_deadline.strftime("%d %B %Y")

    story = generate_story(item, article_text)
    if not story:
        return None
    generated_deadline = parse_date_text(story.get("deadline"))
    if not generated_deadline or not deadline_is_eligible(generated_deadline):
        return None
    evidence_text = article_text + "\n" + safe_text(item.get("deadline_evidence", ""))
    if not deadline_grounded(story.get("deadline"), evidence_text):
        logger.info("DROP ungrounded deadline: %s", story.get("headline"))
        return None

    grounded, bad_number = career_numeric_grounded(story, article_text)
    if not grounded:
        logger.info("DROP numeric hallucination: %s", bad_number)
        retry = generate_story({**item, "grounding_warning": bad_number}, article_text)
        if not retry:
            return None
        retry_deadline = parse_date_text(retry.get("deadline"))
        if not retry_deadline or not deadline_is_eligible(retry_deadline) or not deadline_grounded(retry.get("deadline"), evidence_text):
            return None
        retry_grounded, _ = career_numeric_grounded(retry, article_text)
        if not retry_grounded:
            return None
        story = retry
        generated_deadline = retry_deadline

    verified, unsupported = career_claims_grounded(story, article_text)
    if not verified:
        logger.info("Career claim verification failed: %s", unsupported)
        retry = generate_story({**item, "grounding_warning": ", ".join(unsupported[:3])}, article_text)
        if not retry:
            return None
        retry_deadline = parse_date_text(retry.get("deadline"))
        if not retry_deadline or not deadline_is_eligible(retry_deadline) or not deadline_grounded(retry.get("deadline"), evidence_text):
            return None
        retry_grounded, _ = career_numeric_grounded(retry, article_text)
        if not retry_grounded:
            return None
        verified_retry, _ = career_claims_grounded(retry, article_text)
        if not verified_retry:
            return None
        story = retry
        generated_deadline = retry_deadline

    story["deadline_iso"] = generated_deadline.date().isoformat()
    story["published_date"] = page_dt.isoformat() if page_dt else item.get("published_date", "")
    story["image_candidates"] = list(image_candidates or [])
    story["image_url"] = story["image_candidates"][0] if story["image_candidates"] else ""
    story["category_hashtags"] = category_hashtags(story)
    story["topic"] = canonical_topic(story.get("topic") or item.get("topic"))
    story["event_key"] = item.get("event_key") or normalize_title(f"{story.get('company','')} {story.get('headline','')} {story.get('deadline','')}")
    story["event_cluster_id"] = item.get("event_cluster_id", "")
    story["event_source_count"] = item.get("event_source_count", 1)
    return story

def process_ranked_region(region, ranked):
    valid = []
    attempted = 0
    for item in build_candidate_pool(ranked, MAX_STORIES_PER_RUN):
        if len(valid) >= MAX_STORIES_PER_RUN:
            break
        attempted += 1
        story = process_story_candidate(item)
        if not story:
            continue
        if is_already_published_candidate({**item, "canonical": item.get("canonical"), "title": story.get("headline", item.get("title")), "event_key": story.get("event_key", "")}):
            continue
        if any(title_similarity(story.get("headline", ""), x.get("headline", "")) >= 0.88 for x in valid):
            continue
        valid.append(story)
    logger.info("Career final valid=%d attempted=%d", len(valid), attempted)
    return valid


def store_event(story, published=False, message_id=None):
    event_id = make_event_id(story)
    STATE["events"][event_id] = {
        "event_id": event_id,
        "event_key": story.get("event_key", ""),
        "canonical_url": story.get("canonical", canonical_url(story.get("url", ""))),
        "original_url": story.get("url", ""),
        "source": story.get("source", ""),
        "region": "Career",
        "headline": story.get("headline", ""),
        "company": story.get("company", ""),
        "deadline": story.get("deadline", ""),
        "deadline_iso": story.get("deadline_iso", ""),
        "location": story.get("location", ""),
        "selected_at": now_iso(),
        "published_at": story.get("published_date", ""),
        "status": "published" if published else "selected",
        "message_id": message_id,
    }
    return event_id


def prepare_ranked_region(region, candidates):
    ranked = rank_candidates(candidates, region)
    clustered = collapse_event_clusters(ranked)
    eligible = [x for x in clustered if x.get("importance_score", 0) >= 7 and x.get("important")]
    return eligible


# ============================================================
# DIRECT JOB PORTAL DISCOVERY
# ============================================================

DIRECT_JOB_SOURCES = [
    {"name": "Bdjobs", "url": "https://jobs.bdjobs.com/jobsearch.asp", "type": "job_portal"},
    {"name": "Dohaj", "url": "https://dohaj.com/jobs", "type": "job_portal"},
    {"name": "Job.com.bd", "url": "https://job.com.bd/jobs/new_jobs/", "type": "job_portal"},
    {"name": "Smart Job", "url": "https://smartjob.portal.gov.bd/", "type": "official_job_portal"},
    {"name": "Alljobs Teletalk", "url": "https://alljobs.teletalk.com.bd/", "type": "official_job_portal"},
    {"name": "BPSC", "url": "https://bpsc.gov.bd/", "type": "official_job_portal"},
    {"name": "BCC e-Recruitment", "url": "https://erecruitment.bcc.gov.bd/", "type": "official_job_portal"},
    {"name": "JobsNoticeBD", "url": "https://jobsnoticebd.com/", "type": "job_portal"},
    {"name": "JobsInfo", "url": "https://jobsinfo.bd/", "type": "job_portal"},
    {"name": "JobFeeds", "url": "https://jobfeeds.online/", "type": "job_portal"},
    {"name": "CircularBD", "url": "https://www.circularbd.com/alljobs", "type": "job_portal"},
    {"name": "Dhaka Post", "url": "https://www.dhakapost.com/jobs-career/", "type": "news_jobs"},
    {"name": "Dhaka Tribune", "url": "https://bangla.dhakatribune.com/jobs", "type": "news_jobs"},
    {"name": "Bangla Tribune", "url": "https://www.banglatribune.com/jobs", "type": "news_jobs"},
    {"name": "JagoNews24", "url": "https://www.jagonews24.com/topic/%E0%A6%9A%E0%A6%BE%E0%A6%95%E0%A6%B0%E0%A6%BF", "type": "news_jobs"},
    {"name": "Prothom Alo", "url": "https://www.prothomalo.com/collection/chakri-all", "type": "news_jobs"},
]



def parse_relative_listing_date(text, base=None):
    base = base or career_now()
    raw = safe_text(text).strip()
    normalized = raw.translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))
    if re.search(r"\b(just now|now|এইমাত্র|এই মাত্র)\b", normalized, re.I):
        return base
    m = re.search(r"(\d+)\s*(minute|minutes|min|mins|ঘণ্টা|ঘন্টা|hour|hours|hr|hrs|day|days|দিন)\s*(ago|আগে)?", normalized, re.I)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith(("minute", "min")):
            return base - timedelta(minutes=n)
        if unit in {"ঘণ্টা", "ঘন্টা"} or unit.startswith(("hour", "hr")):
            return base - timedelta(hours=n)
        return base - timedelta(days=n)
    if re.search(r"\b(yesterday|গতকাল)\b", normalized, re.I):
        return base - timedelta(days=1)
    return parse_date_text(normalized)


def listing_published_date(text):
    raw = safe_text(text)
    marker_patterns = [
        r"(?:posted|published|date|added|প্রকাশিত|প্রকাশ|যোগ করা হয়েছে|পোস্ট করা হয়েছে)\s*[:\-]?\s*([^|•]+)",
    ]
    for pattern in marker_patterns:
        for m in re.finditer(pattern, raw, re.I):
            dt = parse_relative_listing_date(m.group(1), career_now())
            if dt:
                return dt
    return parse_relative_listing_date(raw, career_now())


def direct_portal_gap_fill():
    added = 0
    seen_links = set()
    for source in DIRECT_JOB_SOURCES:
        try:
            response = session.get(source["url"], headers=HEADERS, timeout=20)
            if response.status_code >= 400:
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            for anchor in soup.find_all("a", href=True):
                title = safe_text(anchor.get_text(" ", strip=True))
                if len(title) < 8 or len(title) > 220:
                    continue
                href = safe_text(anchor.get("href"))
                link = urljoin(response.url, href)
                if not link or link in seen_links or not primary_domain_allowed(link):
                    continue
                if not JOB_SIGNAL_RE.search(title):
                    continue
                container = anchor
                for _ in range(4):
                    if getattr(container, "parent", None) is None:
                        break
                    candidate_text = safe_text(container.get_text(" ", strip=True))
                    if len(candidate_text) >= 80:
                        break
                    container = container.parent
                listing_text = safe_text(container.get_text(" ", strip=True))
                published_dt = listing_published_date(listing_text)
                if not published_dt or not (DISCOVERY_START <= published_dt <= DISCOVERY_END):
                    continue
                deadline_dt = choose_deadline(listing_text)
                if deadline_dt and not deadline_is_eligible(deadline_dt):
                    continue
                item = {
                    "title": title,
                    "url": link,
                    "canonical": canonical_url(link),
                    "published_dt": published_dt.isoformat(),
                    "published_date": published_dt.isoformat(),
                    "source": source["name"],
                    "source_type": source["type"],
                    "region": "Career",
                    "excerpt": listing_text[:3000],
                    "image": "",
                    "discovery": "direct_portal",
                    "date_estimated": False,
                    "deadline_hint": deadline_dt.strftime("%d %B %Y") if deadline_dt else "",
                }
                if not candidate_basic_allowed(item):
                    continue
                if item["canonical"] in POSTED_URLS or item["canonical"] in STATE["queue"]:
                    continue
                queue_candidate(item)
                seen_links.add(link)
                added += 1
                if added >= MAX_RSS_CANDIDATES:
                    return added
        except Exception as exc:
            logger.warning("Direct portal discovery failed %s: %s", source["name"], exc)
    return added


# ============================================================
# CAREER NEWSROOM V1 CLEAN RUNTIME
# ============================================================

def _draw_channel_chip(draw, font, canvas_size):
    channel_text = "@CareerNewsroom"
    bbox = draw.textbbox((0, 0), channel_text, font=font)
    padding_x, padding_y = 18, 9
    margin_x, margin_y = 28, 24
    chip_w = (bbox[2] - bbox[0]) + padding_x * 2
    chip_h = (bbox[3] - bbox[1]) + padding_y * 2
    x2 = canvas_size[0] - margin_x
    y2 = canvas_size[1] - margin_y
    x1 = x2 - chip_w
    y1 = y2 - chip_h
    return (x1, y1, x2, y2), (x1 + padding_x, y1 + padding_y - 1), channel_text


def run():
    refresh_career_window()
    logger.info("CAREER NEWSROOM V1 UPDATE-ONLY")
    logger.info("Channel=%s | 72h=%s -> %s | deadline >= %s", TELEGRAM_CHANNEL, DISCOVERY_START.isoformat(), DISCOVERY_END.isoformat(), (NOW_BD + timedelta(days=DEADLINE_MIN_DAYS)).isoformat())
    prune_state()
    refresh_category_coverage()
    collect_rss()
    direct_count = direct_portal_gap_fill()
    queued_count = queue_candidates_for_region("Career")
    google_count = google_news_gap_fill("Career", queued_count + direct_count, DISCOVERY_TARGET_PER_REGION)
    exa_count = exa_gap_fill("Career", queued_count + direct_count + google_count, DISCOVERY_TARGET_PER_REGION)
    logger.info("DISCOVERY direct=%d google=%d exa=%d", direct_count, google_count, exa_count)
    save_state(STATE)

    candidates = available_candidates("Career", source_pool="primary")
    filtered = []
    for item in candidates:
        if candidate_basic_allowed(item) and not is_already_published_candidate(item):
            if not any(title_similarity(item.get("title", ""), x.get("title", "")) >= 0.92 for x in filtered):
                filtered.append(item)
    logger.info("CAREER CANDIDATES=%d", len(filtered))

    ranked = prepare_ranked_region("Career", filtered[:MAX_RSS_CANDIDATES])
    stories = process_ranked_region("Career", ranked)
    logger.info("CAREER FINAL STORIES=%d/%d", len(stories), MAX_STORIES_PER_RUN)

    published_count = 0
    for index, story in enumerate(stories, start=1):
        rich_html = fit_rich_html(story)
        if rich_visible_length(rich_html) > MAX_RICH_CHARACTERS:
            logger.warning("DROP rich HTML over limit: %s", story.get("headline"))
            continue
        image_path = prepare_image(story, index)
        result = send_rich_photo(image_path, rich_html)
        if not result.get("ok"):
            logger.warning("Rich Message publish failed; using Bot API fallback: %s", result.get("description"))
            result = send_bot_api_fallback(image_path, rich_html)
        if result.get("ok"):
            published_count += 1
            message = result.get("result", {})
            message_id = message.get("message_id") if isinstance(message, dict) else None
            canonical = story.get("canonical") or canonical_url(story.get("url", ""))
            if canonical:
                POSTED_URLS.add(canonical)
                save_posted_url(canonical)
                q = STATE["queue"].get(canonical)
                if q:
                    q["status"] = "posted"
                    q["posted_at"] = now_iso()
            store_event(story, published=True, message_id=message_id)
            STATE["recent_titles"].append(normalize_title(story.get("headline", "")))
            logger.info("Published %d/%d: %s", published_count, len(stories), story.get("headline", ""))
        else:
            logger.error("Telegram failed: %s", result.get("description"))
        save_state(STATE)
        time.sleep(POST_DELAY_SECONDS)
    save_state(STATE)
    logger.info("Finished. Published=%d/%d", published_count, len(stories))


def self_test():
    refresh_career_window()
    assert TELEGRAM_CHANNEL == "@CareerNewsroom"
    assert DISCOVERY_LOOKBACK_HOURS == 72
    assert DEADLINE_MIN_DAYS == 7
    assert normalized_domain("https://jobs.bdjobs.com/example") == "jobs.bdjobs.com"
    assert primary_domain_allowed("https://jobs.bdjobs.com/example")
    assert not primary_domain_allowed("https://example.com")
    assert parse_date_text("25 September 2026").date() == datetime(2026, 9, 25, tzinfo=BD_TZ).date()
    assert deadline_is_eligible(career_now() + timedelta(days=7))
    assert not deadline_is_eligible(career_now() + timedelta(days=6, hours=23))
    # Date-only deadline is interpreted as end-of-day, so a calendar deadline seven days out is eligible.
    seven_day_date = (career_now() + timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    assert deadline_is_eligible(seven_day_date)
    assert choose_deadline("Application Deadline: 25 September 2026").date() == datetime(2026, 9, 25, tzinfo=BD_TZ).date()
    assert not candidate_basic_allowed({"title": "India Jobs Open Now", "url": "https://www.banglatribune.com/jobs/test", "published_date": NOW_BD.isoformat(), "excerpt": "India recruitment jobs", "source": "Bangla Tribune"})
    assert candidate_basic_allowed({"title": "Management Trainee Job Circular", "url": "https://www.banglatribune.com/jobs/test", "published_date": NOW_BD.isoformat(), "excerpt": "Dhaka Bangladesh company recruitment management trainee", "source": "Bangla Tribune"})

    sample = {
        "headline": "Management Trainee", "company": "Example Company Ltd.", "location": "Dhaka",
        "job_type": "Management Trainee", "education": "BBA / MBA", "experience": "Fresh graduates may apply",
        "salary": "Not specified", "deadline": "25 September 2026",
        "suitable_for": ["BBA", "MBA", "Fresh Graduate"],
        "highlights": ["Corporate management opportunity", "Fresh graduates eligible", "Career development opportunity"],
        "apply_url": "https://example.com/apply", "source": "Official Career Page",
        "url": "https://example.com/job", "bold_terms": ["Management Trainee", "Example Company Ltd.", "BBA", "MBA"],
    }
    rendered = fit_rich_html(sample)
    assert "📣 Management Trainee" in rendered
    for marker in ("🎯 Suitable For", "📌 Key Highlights", "📝 Apply Now", "🔎 Source"):
        assert marker in rendered
    assert 'href="https://example.com/apply"' in rendered
    assert "#CareerNewsroom" in rendered
    assert rich_visible_length(rendered) <= MAX_RICH_CHARACTERS

    # Deterministic image fallback tests, same recovery chain as the reference bot.
    original_download_image = globals()["download_image"]
    original_download_source_logo = globals()["download_source_logo"]
    try:
        article_img = Image.new("RGB", (1600, 900), (110, 125, 145))
        logo_img = Image.new("RGB", (900, 700), (250, 250, 250))
        calls = []
        def fake_download_image(url, referer=""):
            calls.append(url)
            if "broken" in safe_text(url): return None
            if "good" in safe_text(url): return article_img.copy()
            return None
        globals()["download_image"] = fake_download_image
        recovered = prepare_image({"url": "https://example.com/story", "source": "Bangla Tribune", "image_candidates": ["https://example.com/broken.jpg", "https://example.com/good.jpg"]}, 990)
        assert os.path.exists(recovered) and Image.open(recovered).size == (1200, 675)
        assert calls[:2] == ["https://example.com/broken.jpg", "https://example.com/good.jpg"]
        globals()["download_image"] = lambda url, referer="": None
        globals()["download_source_logo"] = lambda source, article_url="": logo_img.copy()
        logo_path = prepare_image({"url": "https://example.com/story", "source": "Bangla Tribune", "image_candidates": []}, 991)
        assert os.path.exists(logo_path) and Image.open(logo_path).size == (1200, 675)
        globals()["download_source_logo"] = lambda source, article_url="": None
        name_path = prepare_image({"url": "https://example.com/story", "source": "Bangla Tribune", "image_candidates": []}, 992)
        assert os.path.exists(name_path) and Image.open(name_path).size == (1200, 675)
    finally:
        globals()["download_image"] = original_download_image
        globals()["download_source_logo"] = original_download_source_logo

    logger.info("CareerNewsBot V1 self-test passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        run()
