import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

from .config import Config


def send_email_notification(
    config: Config, subject: str, body: str, notification_type: str = "info"
) -> bool:
    """Send email notification about token status or other events."""
    # Check if email notifications are enabled
    email_enabled = os.environ.get("EMAIL_NOTIFICATIONS", "false").lower() == "true"
    if not email_enabled:
        return False

    # Get email configuration from environment
    smtp_server = os.environ.get("SMTP_SERVER", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_username = os.environ.get("SMTP_USERNAME", "")
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    sender_email = os.environ.get("SENDER_EMAIL", "")
    recipient_email = os.environ.get("RECIPIENT_EMAIL", "")

    if not all(
        [smtp_server, smtp_username, smtp_password, sender_email, recipient_email]
    ):
        print("Email configuration incomplete, skipping notification")
        return False

    # Create message
    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = recipient_email
    msg["Subject"] = f"[Repo Scanner] {subject}"

    # Add timestamp and type to body
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_body = f"""
Notification Type: {notification_type.upper()}
Timestamp: {timestamp}
Repository: {config.repo}

{body}

---
This is an automated notification from Repo Scanner.
"""

    msg.attach(MIMEText(full_body, "plain"))

    try:
        # Connect to SMTP server
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_username, smtp_password)

        # Send email
        server.sendmail(sender_email, recipient_email, msg.as_string())
        server.quit()

        print(f"Email notification sent: {subject}")
        return True
    except Exception as e:
        print(f"Failed to send email notification: {e}")
        return False


def notify_token_expired(config: Config, error_message: str) -> bool:
    """Send notification when LLM token has expired."""
    subject = "LLM Token Expired"
    body = f"""
The LLM API token has expired or become invalid.

Error Details:
{error_message}

Action Required:
1. Check your API key at platform.xiaomimimo.com
2. Generate a new API key if needed
3. Update the MIMO_API_KEY or LLM_API_KEY secret in your repository

The scanner will not be able to analyze code until the token is renewed.
"""
    return send_email_notification(config, subject, body, "token_expired")


def notify_token_exhausted(config: Config, usage_details: str = "") -> bool:
    """Send notification when LLM token quota is exhausted."""
    subject = "LLM Token Quota Exhausted"
    body = f"""
The LLM API token quota has been exhausted.

Usage Details:
{usage_details}

Action Required:
1. Check your usage at platform.xiaomimimo.com
2. Wait for quota reset or upgrade your plan
3. Consider reducing MAX_FILES or increasing IGNORE_PATTERNS to reduce usage

The scanner will resume when quota becomes available.
"""
    return send_email_notification(config, subject, body, "token_exhausted")


def notify_scan_completed(
    config: Config, issues_found: int, issues_created: int
) -> bool:
    """Send notification when scan completes successfully."""
    subject = f"Scan Completed - {issues_found} issues found"
    body = f"""
Repository scan completed successfully.

Results:
- Total issues found: {issues_found}
- GitHub issues created: {issues_created}
- Repository: {config.repo}
- Severity threshold: {config.severity_threshold}

Check the GitHub repository for details.
"""
    return send_email_notification(config, subject, body, "scan_completed")


def notify_fix_completed(
    config: Config, fixes_attempted: int, fixes_succeeded: int
) -> bool:
    """Send notification when fix operation completes."""
    subject = f"Fix Completed - {fixes_succeeded}/{fixes_attempted} successful"
    body = f"""
Auto-fix operation completed.

Results:
- Issues attempted: {fixes_attempted}
- Successful fixes: {fixes_succeeded}
- Failed fixes: {fixes_attempted - fixes_succeeded}
- Repository: {config.repo}

Check the GitHub repository for the created pull requests.
"""
    return send_email_notification(config, subject, body, "fix_completed")
