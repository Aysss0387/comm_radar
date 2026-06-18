import argparse
import os
from pathlib import Path

import requests


RESEND_API_URL = "https://api.resend.com/emails"


def latest_report(reports_dir: Path) -> Path:
    reports = sorted(reports_dir.glob("*.md"), key=lambda path: path.stat().st_mtime)
    if not reports:
        raise FileNotFoundError(f"No weekly report found in {reports_dir}")
    return reports[-1]


def send_email(subject: str, content: str) -> str:
    api_key = os.environ.get("RESEND_API_KEY")
    recipient = os.environ.get("CONTACT_EMAIL")
    sender = os.environ.get("RESEND_FROM_EMAIL", "Paper Radar <onboarding@resend.dev>")

    missing = [
        name
        for name, value in (
            ("RESEND_API_KEY", api_key),
            ("CONTACT_EMAIL", recipient),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

    response = requests.post(
        RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": sender,
            "to": [recipient],
            "subject": subject,
            "text": content,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["id"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Send Paper Radar email through Resend")
    parser.add_argument("--test", action="store_true", help="Send a short test email")
    parser.add_argument("--reports-dir", default="reports", help="Weekly report directory")
    args = parser.parse_args()

    if args.test:
        subject = "Weekly Paper Radar Test"
        content = "Resend 配置成功。Weekly Paper Radar 已经可以发送邮件。"
    else:
        report_path = latest_report(Path(args.reports_dir))
        subject = f"Weekly Paper Radar - {report_path.stem}"
        content = report_path.read_text(encoding="utf-8")

    email_id = send_email(subject, content)
    print(f"Email sent successfully: {email_id}")


if __name__ == "__main__":
    main()
