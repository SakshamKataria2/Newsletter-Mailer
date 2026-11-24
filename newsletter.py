#!/usr/bin/env python3
"""
Cybersecurity Automated Newsletter Pipeline
-------------------------------------------
Fetches security feeds defined in topics.json, structures articles into
categories, and generates a formatted HTML email digest.

Supports:
- Running via Windows Task Scheduler or direct CLI
- --dry-run flag: renders newsletter_preview.html locally without sending mail
- SMTP dispatch using credentials stored in .env
"""

import argparse
import datetime
import html
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import warnings
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# External dependencies
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    import requests
except ImportError:
    requests = None

try:
    import feedparser
except ImportError:
    feedparser = None

try:
    from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
    warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
except ImportError:
    BeautifulSoup = None

# Configure logger (does not log credentials)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cybersec_newsletter")

# Robust directory resolution for Windows Task Scheduler
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
TOPICS_PATH = BASE_DIR / "topics.json"
PREVIEW_PATH = BASE_DIR / "newsletter_preview.html"


def load_environment():
    """Load environment variables from .env located in the script directory."""
    if load_dotenv:
        if ENV_PATH.exists():
            load_dotenv(dotenv_path=ENV_PATH)
            logger.info("Loaded environment variables from %s", ENV_PATH)
        else:
            logger.warning(".env file not found at %s", ENV_PATH)
    else:
        logger.warning("python-dotenv is not installed; relying on existing OS environment.")


def load_topics_config():
    """Load topic categories, feeds, and settings from topics.json."""
    if not TOPICS_PATH.exists():
        logger.error("Configuration file missing: %s", TOPICS_PATH)
        sys.exit(1)

    try:
        with open(TOPICS_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
            logger.info("Loaded topics configuration with %d categories.", len(config.get("categories", [])))
            return config
    except Exception as exc:
        logger.error("Failed to parse %s: %s", TOPICS_PATH, exc)
        sys.exit(1)


def clean_html_summary(raw_html: str, max_chars: int = 280) -> str:
    """Strip HTML tags and truncate summary cleanly."""
    if not raw_html:
        return ""

    if BeautifulSoup:
        text = BeautifulSoup(raw_html, "html.parser").get_text(separator=" ")
    else:
        text = re.sub(r"<[^>]+>", " ", raw_html)

    # Normalize whitespace and unescape HTML entities
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "..."
    return text


def parse_published_date(entry) -> str:
    """Format published or updated date string cleanly."""
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None) if hasattr(entry, attr) else entry.get(attr)
        if parsed:
            try:
                dt = datetime.datetime(*parsed[:6], tzinfo=datetime.timezone.utc)
                return dt.strftime("%b %d, %Y")
            except Exception:
                pass

    for attr in ("published", "pubDate", "updated", "date"):
        raw_val = entry.get(attr) if isinstance(entry, dict) else getattr(entry, attr, None)
        if raw_val and isinstance(raw_val, str):
            return raw_val[:16].strip()

    return datetime.datetime.now(datetime.timezone.utc).strftime("%b %d, %Y")


def fetch_feed_articles(feed_url: str, max_items: int, timeout_sec: int, user_agent: str):
    """Fetch and parse feed entries with custom User-Agent and timeout."""
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/rss+xml, application/rdf+xml, application/atom+xml, application/xml, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    feed_data = None
    if requests and feedparser:
        try:
            resp = requests.get(feed_url, headers=headers, timeout=timeout_sec)
            if resp.status_code == 200:
                feed_data = feedparser.parse(resp.content)
            else:
                logger.warning("HTTP %d when fetching %s via requests, trying direct feedparser fallback", resp.status_code, feed_url)
                feed_data = feedparser.parse(feed_url)
        except Exception as exc:
            logger.warning("Request failed for %s: %s, trying direct feedparser fallback", feed_url, exc)
            try:
                feed_data = feedparser.parse(feed_url)
            except Exception:
                pass
    elif feedparser:
        try:
            feed_data = feedparser.parse(feed_url)
        except Exception as exc:
            logger.warning("feedparser failed for %s: %s", feed_url, exc)
    else:
        logger.error("Neither requests nor feedparser is available to fetch feeds.")
        return []

    if not feed_data or not getattr(feed_data, "entries", None):
        return []

    articles = []
    for entry in feed_data.entries[:max_items]:
        title = getattr(entry, "title", "").strip()
        link = getattr(entry, "link", "").strip()

        if not title or not link:
            continue

        raw_summary = (
            getattr(entry, "summary", "")
            or getattr(entry, "description", "")
            or (entry.get("content", [{}])[0].get("value", "") if hasattr(entry, "content") else "")
        )

        articles.append({
            "title": title,
            "link": link,
            "summary": clean_html_summary(raw_summary),
            "date": parse_published_date(entry),
        })

    return articles


def collect_newsletter_data(config):
    """Scrape and aggregate all categorized articles."""
    settings = config.get("settings", {})
    max_items = settings.get("max_items_per_feed", 4)
    timeout_sec = settings.get("request_timeout_seconds", 15)
    user_agent = settings.get(
        "user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )

    seen_links = set()
    categories_data = []
    total_articles = 0

    for cat in config.get("categories", []):
        cat_name = cat.get("name", "General Security")
        cat_desc = cat.get("description", "")
        cat_color = cat.get("color", "#0ea5e9")
        cat_subtopics = cat.get("subtopics", [])
        cat_articles = []

        logger.info("Processing Category: %s", cat_name)

        for feed_cfg in cat.get("feeds", []):
            feed_name = feed_cfg.get("name", "Unknown Feed")
            feed_url = feed_cfg.get("url")
            if not feed_url:
                continue

            logger.info("  Fetching feed: %s (%s)", feed_name, feed_url)
            feed_items = fetch_feed_articles(feed_url, max_items, timeout_sec, user_agent)

            for item in feed_items:
                if item["link"] in seen_links:
                    continue
                seen_links.add(item["link"])
                item["source"] = feed_name
                cat_articles.append(item)

        total_articles += len(cat_articles)
        categories_data.append({
            "name": cat_name,
            "description": cat_desc,
            "color": cat_color,
            "subtopics": cat_subtopics,
            "articles": cat_articles,
        })

    return {
        "title": settings.get("newsletter_title", "Daily CyberSec Digest"),
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%A, %B %d, %Y | %H:%M UTC"),
        "total_articles": total_articles,
        "categories": categories_data,
    }


def render_html_newsletter(data, is_dry_run: bool = False) -> str:
    """Generate high-quality responsive HTML email content."""
    title = html.escape(data["title"])
    generated_at = html.escape(data["generated_at"])
    total_articles = data["total_articles"]
    categories = data["categories"]

    dry_run_badge = ""
    if is_dry_run:
        dry_run_badge = """
        <div style="background-color: #fef08a; border: 1px solid #ca8a04; color: #854d0e; padding: 10px 16px; border-radius: 6px; margin-bottom: 20px; font-size: 13px; font-weight: 600;">
          [TEST PREVIEW] This newsletter was generated in --dry-run mode and was not emailed.
        </div>
        """

    # Category quick links / stat pill
    stat_pills = f"""
    <div style="display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px;">
      <span style="background-color: #1e293b; color: #38bdf8; padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: 600;">
        {total_articles} Articles Curated
      </span>
      <span style="background-color: #1e293b; color: #94a3b8; padding: 4px 10px; border-radius: 12px; font-size: 12px;">
        {len(categories)} Threat Sectors
      </span>
    </div>
    """

    categories_html = []
    for cat in categories:
        cat_name = html.escape(cat["name"])
        cat_desc = html.escape(cat["description"])
        cat_color = cat["color"]
        articles = cat["articles"]

        # Subtopics badges
        subtopics_html = "".join(
            f'<span style="display:inline-block; background-color:#f1f5f9; color:#475569; font-size:11px; font-weight:500; padding:2px 8px; border-radius:4px; margin-right:4px; margin-top:4px;">#{html.escape(sub)}</span>'
            for sub in cat["subtopics"]
        )

        articles_html = []
        if not articles:
            articles_html.append("""
            <div style="padding: 16px; background-color: #f8fafc; border-radius: 6px; color: #64748b; font-size: 13px; font-style: italic;">
              No new advisories or articles detected in this cycle.
            </div>
            """)
        else:
            for item in articles:
                item_title = html.escape(item["title"])
                item_link = html.escape(item["link"])
                item_source = html.escape(item["source"])
                item_summary = html.escape(item["summary"])
                item_date = html.escape(item["date"])

                articles_html.append(f"""
                <div style="margin-bottom: 18px; padding-bottom: 16px; border-bottom: 1px solid #e2e8f0;">
                  <div style="margin-bottom: 6px;">
                    <span style="background-color: #e0f2fe; color: #0284c7; font-size: 11px; font-weight: 700; padding: 2px 7px; border-radius: 4px; text-transform: uppercase;">{item_source}</span>
                    <span style="color: #94a3b8; font-size: 12px; margin-left: 8px;">{item_date}</span>
                  </div>
                  <h3 style="margin: 0 0 6px 0; font-size: 16px; line-height: 1.4;">
                    <a href="{item_link}" target="_blank" style="color: #0f172a; text-decoration: none; font-weight: 600;">
                      {item_title}
                    </a>
                  </h3>
                  <p style="margin: 0; color: #475569; font-size: 13.5px; line-height: 1.55;">
                    {item_summary}
                  </p>
                  <div style="margin-top: 6px;">
                    <a href="{item_link}" target="_blank" style="color: #0284c7; font-size: 12px; text-decoration: none; font-weight: 600;">
                      Read advisory &rarr;
                    </a>
                  </div>
                </div>
                """)

        cat_block = f"""
        <div style="background-color: #ffffff; border-radius: 8px; border: 1px solid #e2e8f0; margin-bottom: 26px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
          <div style="border-left: 5px solid {cat_color}; padding: 16px 20px; background-color: #fafafa; border-bottom: 1px solid #f1f5f9;">
            <h2 style="margin: 0; font-size: 18px; color: #0f172a; font-weight: 700;">{cat_name}</h2>
            <p style="margin: 4px 0 6px 0; font-size: 12.5px; color: #64748b;">{cat_desc}</p>
            <div>{subtopics_html}</div>
          </div>
          <div style="padding: 20px;">
            {''.join(articles_html)}
          </div>
        </div>
        """
        categories_html.append(cat_block)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #334155;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color: #f1f5f9; padding: 24px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" style="max-width: 680px; text-align: left;" cellspacing="0" cellpadding="0">
          
          <!-- HEADER -->
          <tr>
            <td>
              <div style="background: linear-gradient(135deg, #0b1120 0%, #0f172a 100%); color: #ffffff; padding: 28px 24px; border-radius: 10px 10px 0 0; border-bottom: 3px solid #38bdf8;">
                <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 1.5px; color: #38bdf8; font-weight: 700; margin-bottom: 6px;">
                  AUTOMATED THREAT INTELLIGENCE
                </div>
                <h1 style="margin: 0; font-size: 24px; font-weight: 800; letter-spacing: -0.5px; line-height: 1.25;">
                  {title}
                </h1>
                <div style="font-size: 12.5px; color: #94a3b8; margin-top: 8px;">
                  {generated_at}
                </div>
                {stat_pills}
              </div>
            </td>
          </tr>

          <!-- MAIN BODY -->
          <tr>
            <td style="background-color: #f8fafc; padding: 24px; border-radius: 0 0 10px 10px; border: 1px solid #e2e8f0; border-top: none;">
              {dry_run_badge}
              {''.join(categories_html)}
              
              <!-- FOOTER -->
              <div style="text-align: center; margin-top: 24px; padding-top: 16px; border-top: 1px solid #e2e8f0; font-size: 11.5px; color: #94a3b8; line-height: 1.6;">
                <p style="margin: 0;">This security digest was automatically compiled by the CyberSec Newsletter engine.</p>
                <p style="margin: 4px 0 0 0;">Configured via <code style="background-color: #e2e8f0; padding: 2px 5px; border-radius: 3px; font-size: 11px;">topics.json</code> &bull; Path: {BASE_DIR}</p>
              </div>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def render_plaintext_newsletter(data) -> str:
    """Generate plaintext alternative for multipart email."""
    lines = [
        f"=== {data['title']} ===",
        f"Generated: {data['generated_at']}",
        f"Total Items: {data['total_articles']}",
        "=" * 50,
        "",
    ]

    for cat in data["categories"]:
        lines.append(f"\n[SECTION: {cat['name'].upper()}]")
        lines.append(f"Description: {cat['description']}")
        lines.append("-" * 40)

        if not cat["articles"]:
            lines.append("No new items.\n")
            continue

        for item in cat["articles"]:
            lines.append(f"* {item['title']}")
            lines.append(f"  Source: {item['source']} | Date: {item['date']}")
            lines.append(f"  URL: {item['link']}")
            if item["summary"]:
                lines.append(f"  Summary: {item['summary']}")
            lines.append("")

    lines.append("\n--\nAutomated CyberSec Newsletter Engine")
    return "\n".join(lines)


def send_email_digest(html_content: str, text_content: str, subject: str):
    """
    Dispatch email using SMTP credentials found in .env.
    Protects sensitive credentials from logging and terminal output.
    """
    host = os.getenv("SMTP_HOST")
    port_str = os.getenv("SMTP_PORT")
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASS")
    recipient = os.getenv("RECIPIENT_EMAIL")

    if not all([host, port_str, user, password, recipient]):
        logger.error(
            "Missing one or more required SMTP environment variables (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, RECIPIENT_EMAIL). "
            "Please check your .env file."
        )
        sys.exit(1)

    try:
        port = int(port_str.strip())
    except ValueError:
        logger.error("SMTP_PORT must be an integer.")
        sys.exit(1)

    # Construct email message
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["To"] = recipient

    msg.attach(MIMEText(text_content, "plain", "utf-8"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    logger.info("Attempting SMTP connection to host on port %d...", port)

    try:
        # Standard SSL on port 465
        if port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
                server.login(user, password)
                server.sendmail(user, [recipient], msg.as_string())
        else:
            # STARTTLS on port 587 or standard SMTP
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.ehlo()
                try:
                    context = ssl.create_default_context()
                    server.starttls(context=context)
                    server.ehlo()
                except Exception as tls_err:
                    logger.warning("STARTTLS not supported or skipped: %s", tls_err)
                server.login(user, password)
                server.sendmail(user, [recipient], msg.as_string())

        logger.info("Newsletter successfully dispatched via SMTP.")
    except Exception as exc:
        logger.error("Failed to deliver email via SMTP: %s", exc)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Automated Cybersecurity Newsletter Pipeline",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch feeds and save rendered HTML to newsletter_preview.html without dispatching email.",
    )
    args = parser.parse_args()

    logger.info("Starting CyberSec Newsletter generation...")
    logger.info("Script directory resolved to: %s", BASE_DIR)

    # 1. Load configuration and environment
    load_environment()
    config = load_topics_config()

    # 2. Collect and aggregate RSS articles
    data = collect_newsletter_data(config)
    logger.info("Aggregated %d total articles across all categories.", data["total_articles"])

    # 3. Render HTML and text templates
    html_body = render_html_newsletter(data, is_dry_run=args.dry_run)
    text_body = render_plaintext_newsletter(data)

    # 4. Handle --dry-run or SMTP dispatch
    if args.dry_run:
        try:
            with open(PREVIEW_PATH, "w", encoding="utf-8") as f:
                f.write(html_body)
            logger.info("[SUCCESS] --dry-run active: Preview saved to %s", PREVIEW_PATH)
        except Exception as exc:
            logger.error("Failed to save preview file: %s", exc)
            sys.exit(1)
        return

    # 5. Dispatch email
    date_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    subject = f"[{data['title']}] - Security Briefing {date_str}"
    send_email_digest(html_body, text_body, subject)


if __name__ == "__main__":
    main()
