import smtplib
from email.message import EmailMessage

from .security import Problem


def send(cfg, recipient, subject, content):
    if not cfg.smtp_host:
        if cfg.production:
            raise Problem("MAIL_UNAVAILABLE", status=503)
        return False
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = cfg.smtp_from, recipient, subject
    message.set_content(content)
    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20) as server:
            if cfg.smtp_starttls:
                server.starttls()
            if cfg.smtp_user:
                server.login(cfg.smtp_user, cfg.smtp_password)
            server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise Problem("MAIL_UNAVAILABLE", "邮件暂时无法投递，请稍后重试", 503) from exc
    return True
