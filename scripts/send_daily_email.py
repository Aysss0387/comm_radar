"""Send the daily three-paper deep-reading feed through Resend as an HTML email."""

import argparse
import html
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import requests


RESEND_API_URL = "https://api.resend.com/emails"
SLOT_COLORS = {"relevance": "#197c73", "theory": "#8a5a9e", "method": "#d8735b"}


def load_records(feed_path: Path, day: str) -> list:
    if not feed_path.exists():
        return []
    records = []
    for line in feed_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("date") == day:
            records.append(record)
    return records


def esc(value) -> str:
    return html.escape(str(value or ""))


def render_paper(record: dict) -> str:
    color = SLOT_COLORS.get(record.get("slot", ""), "#197c73")
    meta = record.get("venue_meta") or {}
    badge_bits = [esc(record.get("venue") or "未知期刊")]
    if meta.get("if_2025"):
        badge_bits.append(f"IF {esc(meta['if_2025'])}")
    if meta.get("rank"):
        badge_bits.append(esc(meta["rank"]))
    link = record.get("doi_url") or record.get("publisher_url") or record.get("openalex_url") or ""
    analysis = record.get("analysis")
    stars = "⭐" * int(analysis["priority"]) if analysis else "⭐⭐⭐"
    rows = []
    if analysis:
        inferred = '<p style="margin:6px 0;color:#a05b2c;font-size:13px;">以下解读主要基于标题与期刊推断，请以原文为准。</p>' if analysis.get("inferred") else ""
        sections = [
            ("为什么值得读", analysis.get("why_read")),
            ("RQ / 理论", analysis.get("rq_theory")),
            ("数据与方法", analysis.get("data_method")),
            ("核心发现", analysis.get("findings")),
            ("最值得精读", analysis.get("focus_section")),
            ("可直接借鉴", analysis.get("takeaways")),
        ]
        rows.append(inferred)
        for label, value in sections:
            if value:
                rows.append(f'<p style="margin:6px 0;font-size:14px;line-height:1.6;"><strong>{label}</strong>：{esc(value)}</p>')
    else:
        hits = "、".join(record.get("slot_hits", [])[:5])
        rows.append(f'<p style="margin:6px 0;font-size:14px;">入选理由：命中「{esc(record.get("slot_label"))}」信号词（{esc(hits) or "综合评分靠前"}）。</p>')
    backfill_note = '<p style="margin:6px 0;color:#8a6d3b;font-size:12px;">回溯推荐：近两周该槽位无合适新文。</p>' if record.get("backfilled_window") else ""
    return f"""
    <div style="border:1px solid #dfe7df;border-radius:12px;padding:18px 20px;margin:0 0 16px;background:#fffdf8;">
      <span style="display:inline-block;background:{color};color:#fff;font-size:12px;padding:3px 10px;border-radius:999px;">{esc(record.get('slot_label'))}</span>
      <span style="font-size:13px;color:#6b7d7b;margin-left:8px;">{' · '.join(badge_bits)}</span>
      <h2 style="margin:10px 0 4px;font-size:17px;line-height:1.45;color:#192b2c;">{esc(record.get('title'))}</h2>
      <p style="margin:2px 0 8px;font-size:13px;color:#6b7d7b;">{esc('; '.join(record.get('authors', [])[:6]))} · {esc(record.get('publication_date') or '')}</p>
      <p style="margin:2px 0 10px;font-size:14px;">精读优先级：{stars}{f' · <a href="{esc(link)}" style="color:#197c73;">打开论文</a>' if link else ''}</p>
      {''.join(rows)}
      {backfill_note}
    </div>"""


def render_email(day: str, records: list) -> str:
    body = "".join(render_paper(record) for record in records)
    return f"""<!doctype html><html><body style="margin:0;padding:24px;background:#eef4ef;font-family:-apple-system,'Segoe UI','PingFang SC',sans-serif;">
    <div style="max-width:680px;margin:0 auto;">
      <p style="font-size:12px;letter-spacing:.15em;color:#197c73;font-weight:700;">PERSONAL COMMUNICATION RESEARCH FEED</p>
      <h1 style="font-size:22px;color:#192b2c;margin:4px 0 18px;">每日精读推荐 · {esc(day)}</h1>
      {body}
      <p style="font-size:12px;color:#6b7d7b;margin-top:18px;">三槽位：① 当前研究最相关 ② 理论值得学 ③ 方法/前沿。仅使用公开元数据与摘要。</p>
    </div></body></html>"""


def send_email(subject: str, html_content: str) -> str:
    api_key = os.environ.get("RESEND_API_KEY")
    recipient = os.environ.get("CONTACT_EMAIL")
    sender = os.environ.get("RESEND_FROM_EMAIL", "Paper Radar <onboarding@resend.dev>")
    missing = [name for name, value in (("RESEND_API_KEY", api_key), ("CONTACT_EMAIL", recipient)) if not value]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
    response = requests.post(
        RESEND_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"from": sender, "to": [recipient], "subject": subject, "html": html_content},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["id"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Send daily deep-reading feed through Resend")
    parser.add_argument("--date", default=None, help="Feed date YYYY-MM-DD; defaults to today, then yesterday as fallback")
    parser.add_argument("--feed", default="data/daily_feed.jsonl", help="Path to daily feed JSONL")
    args = parser.parse_args()

    feed_path = Path(args.feed)
    day = args.date or date.today().isoformat()
    records = load_records(feed_path, day)
    if not records and not args.date:
        day = (date.today() - timedelta(days=1)).isoformat()
        records = load_records(feed_path, day)
    if not records:
        print(f"No daily feed records found for {day}; skip sending.")
        sys.exit(0)
    email_id = send_email(f"每日精读推荐 · {day}", render_email(day, records))
    print(f"Daily email sent successfully: {email_id}")


if __name__ == "__main__":
    main()
