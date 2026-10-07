"""Outgoing email over SMTP (stdlib smtplib, run in a worker thread).

With SMTP_HOST empty (local dev) nothing is sent: the email is logged at INFO instead.
"""

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from urllib.parse import urlencode

from app.core.config import settings

logger = logging.getLogger(__name__)


def password_reset_link(token: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/reset-password?{urlencode({'token': token})}"


def _send_smtp(message: EmailMessage) -> None:
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as smtp:
        if settings.SMTP_TLS:
            smtp.starttls()
        if settings.SMTP_USER:
            smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        smtp.send_message(message)


async def send_email(to: str, subject: str, text: str, html: str | None = None) -> None:
    if not settings.SMTP_HOST:
        logger.info(
            "SMTP_HOST not set, email not sent. To: %s | Subject: %s\n%s", to, subject, text
        )
        return

    message = EmailMessage()
    message["From"] = settings.SMTP_FROM
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    if html is not None:
        message.add_alternative(html, subtype="html")

    await asyncio.to_thread(_send_smtp, message)


async def send_password_reset_email(to: str, token: str) -> None:
    """Runs as a background task: failures are logged, never raised to the client."""
    link = password_reset_link(token)
    minutes = settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES
    text = (
        "Xin chào,\n\n"
        "Chúng tôi nhận được yêu cầu đặt lại mật khẩu cho tài khoản của bạn.\n"
        "Mở liên kết sau để đặt mật khẩu mới "
        f"(hết hạn sau {minutes} phút, chỉ dùng được một lần):\n\n"
        f"{link}\n\n"
        "Nếu bạn không yêu cầu, hãy bỏ qua email này — mật khẩu của bạn sẽ không thay đổi.\n\n"
        f"{settings.PROJECT_NAME}"
    )
    html = (
        "<p>Xin chào,</p>"
        "<p>Chúng tôi nhận được yêu cầu đặt lại mật khẩu cho tài khoản của bạn.</p>"
        f'<p><a href="{link}">Đặt lại mật khẩu</a></p>'
        f"<p>Liên kết hết hạn sau {minutes} phút và chỉ dùng được một lần.</p>"
        "<p>Nếu bạn không yêu cầu, hãy bỏ qua email này — mật khẩu của bạn sẽ không thay đổi.</p>"
    )
    try:
        await send_email(to, "Đặt lại mật khẩu", text, html)
    except Exception:
        logger.exception("Failed to send password reset email to %s", to)
