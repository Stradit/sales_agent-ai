"""Minimal transactional email sender for auth flows (welcome, password
reset). Deliberately independent of apps/content_pipeline/mailer.py: that
sender needs an interactive Microsoft device-code login and its own
config.py — reusing it from this process hits a real `config` module-name
collision (this app's own top-level config.py already occupies that name in
sys.modules), not just a style mismatch. Plain SMTP is the portable option
that needs no Azure app registration.

Falls back to logging the email instead of raising when SMTP isn't
configured, so auth flows (account creation, password reset) keep working
end-to-end during development without real email infra — see
AUTH_JWT_IMPLEMENTATION_PLAN.md §7.5 for the tradeoffs of this vs. a proper
sender, which is worth revisiting before relying on this for real users.
"""
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from xml.sax.saxutils import escape

from dotenv import load_dotenv

# Loaded independently of import order — same reasoning as auth.py.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
# Accept either name — SMTP_USERNAME is what actually ended up in .env.
SMTP_USER = os.getenv("SMTP_USERNAME", "") or os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "") or SMTP_USER

# Brand palette, mirrored from frontend/css/tokens.css (email clients can't
# read CSS variables): orange for the action, navy for the header and ink.
BRAND = "#E46521"        # --brand
BRAND_DARK = "#C9541A"   # --brand-hover: links, which need more contrast on white
NAVY = "#001233"         # --navy / --topbar-bg
INK = "#001233"          # --text-primary
TEXT = "#4C5671"         # --text-secondary
MUTED = "#8D9097"        # --text-muted
BORDER = "#E7EAF0"       # --panel-border
PAGE_BG = "#F5F6F9"      # --content-bg
FONT = "Roboto,'Segoe UI',Helvetica,Arial,sans-serif"


def is_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def render_html(heading: str, lines: list, cta_label: str = None, cta_url: str = None,
                 footnote: str = None) -> str:
    """A single shared branded template — inline CSS only, since email
    clients ignore <style> blocks and stylesheets alike. `lines` are plain
    paragraphs (escaped); pass pre-built HTML only via `footnote` if needed.
    """
    body_html = "".join(
        f'<p style="margin:0 0 14px;font-size:14px;line-height:1.6;color:{TEXT};">{escape(line)}</p>'
        for line in lines
    )
    button_html = ""
    if cta_label and cta_url:
        button_html = f'''
        <table role="presentation" cellpadding="0" cellspacing="0" style="margin:22px 0;">
          <tr><td style="background:{BRAND};">
            <a href="{escape(cta_url)}" target="_blank"
               style="display:inline-block;padding:12px 26px;font-size:14px;font-weight:700;
                      color:#ffffff;text-decoration:none;font-family:{FONT};">
              {escape(cta_label)}
            </a>
          </td></tr>
        </table>
        <p style="margin:0 0 14px;font-size:12px;line-height:1.5;color:{MUTED};word-break:break-all;">
          Or copy this link: <a href="{escape(cta_url)}" style="color:{BRAND_DARK};">{escape(cta_url)}</a>
        </p>'''

    footnote_html = (
        f'<p style="margin:18px 0 0;font-size:12px;line-height:1.5;color:{MUTED};">{escape(footnote)}</p>'
        if footnote else ""
    )

    return f'''<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:{PAGE_BG};font-family:{FONT};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE_BG};padding:32px 16px;">
    <tr><td align="center">
      <table role="presentation" width="480" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border:1px solid {BORDER};max-width:480px;width:100%;">
        <tr><td style="background:{NAVY};padding:20px 28px;border-bottom:3px solid {BRAND};">
          <span style="font-size:16px;font-weight:800;color:#ffffff;letter-spacing:-.01em;">
            <span style="color:{BRAND};">&#9679;</span> Doshin
          </span>
        </td></tr>
        <tr><td style="padding:28px 28px 24px;">
          <h1 style="margin:0 0 16px;font-size:19px;font-weight:800;color:{INK};letter-spacing:-.01em;">{escape(heading)}</h1>
          {body_html}
          {button_html}
          {footnote_html}
        </td></tr>
        <tr><td style="padding:16px 28px;border-top:1px solid {BORDER};">
          <p style="margin:0;font-size:11px;color:{MUTED};">Doshin &mdash; turn signals into strategy. Automated account notification.</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>'''


def send_email(to: str, subject: str, body: str, html_body: str = None) -> bool:
    """Returns True if actually sent over SMTP, False if only logged.

    `html_body`, if given, is sent alongside `body` as a multipart/
    alternative message (plain text stays the fallback for clients that
    don't render HTML). Never raises on a missing/bad config — an auth flow
    (account creation, password reset) must not fail just because email
    infra isn't set up; the token/account-level effect already happened by
    the time this runs.
    """
    if not is_configured():
        print(f"[email] SMTP not configured (see .env SMTP_* vars) — logging instead of sending.\n"
              f"To: {to}\nSubject: {subject}\n{body}\n")
        return False

    if html_body:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
    else:
        msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to], msg.as_string())
        return True
    except Exception as e:
        print(f"[email] Send failed ({e}) — logging instead.\nTo: {to}\nSubject: {subject}\n{body}\n")
        return False
