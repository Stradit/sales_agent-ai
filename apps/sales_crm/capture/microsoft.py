"""Microsoft 365 via Microsoft Graph (README §4.1). No SDK — plain OAuth 2.0 + REST over httpx.

Azure AD app registration (single tenant) → .env:
    MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT_ID
    MS_REDIRECT_URI   optional; default {APP_BASE_URL}/api/crm/capture/callback/microsoft
Delegated permissions: offline_access, User.Read, Mail.Read, Calendars.Read (admin consent recommended).

Reads are incremental with Graph *delta* queries: the first run takes the last 30 days of Inbox,
Sent Items and the calendar (−30 / +30 days); later runs only fetch changes via the stored deltaLink.
"""

import base64
import hashlib
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["offline_access", "User.Read", "Mail.Read", "Calendars.Read"]
INITIAL_DAYS = 30
MAIL_SELECT = "id,subject,from,toRecipients,ccRecipients,receivedDateTime,sentDateTime,bodyPreview,conversationId,internetMessageId,isDraft"
EVENT_SELECT = "id,subject,start,end,attendees,organizer,bodyPreview,isCancelled,isOnlineMeeting,iCalUId,type"


class AuthError(Exception):
    """Token refresh failed (revoked / expired consent) → the connection needs to be reconnected."""


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"Graph throttled; retry after {retry_after}s")
        self.retry_after = retry_after


def config() -> Dict[str, str]:
    base = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
    return {"client_id": os.getenv("MS_CLIENT_ID", "").strip(), "client_secret": os.getenv("MS_CLIENT_SECRET", "").strip(),
            "tenant": os.getenv("MS_TENANT_ID", "").strip() or "organizations",
            "redirect_uri": os.getenv("MS_REDIRECT_URI", "").strip() or f"{base}/api/crm/capture/callback/microsoft"}


def configured() -> bool:
    c = config()
    return bool(c["client_id"] and c["client_secret"] and os.getenv("MS_TENANT_ID", "").strip())


def _token_url() -> str:
    return f"https://login.microsoftonline.com/{config()['tenant']}/oauth2/v2.0/token"


# ── OAuth ─────────────────────────────────────────────────────────────────────


def new_pkce() -> Tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorize_url(state: str, code_challenge: str, login_hint: Optional[str] = None) -> str:
    c = config()
    q = {"client_id": c["client_id"], "response_type": "code", "redirect_uri": c["redirect_uri"], "response_mode": "query",
         "scope": " ".join(SCOPES), "state": state, "code_challenge": code_challenge, "code_challenge_method": "S256",
         "prompt": "select_account"}
    if login_hint:
        q["login_hint"] = login_hint
    return f"https://login.microsoftonline.com/{c['tenant']}/oauth2/v2.0/authorize?{urlencode(q)}"


def _token_request(data: Dict[str, str], client: Optional[httpx.Client] = None) -> Dict[str, Any]:
    c = config()
    body = {"client_id": c["client_id"], "client_secret": c["client_secret"], "scope": " ".join(SCOPES), **data}
    http = client or httpx.Client(timeout=30)
    try:
        r = http.post(_token_url(), data=body)
    finally:
        if client is None:
            http.close()
    js = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200:
        err = js.get("error", "")
        if err in ("invalid_grant", "interaction_required", "consent_required", "invalid_client", "unauthorized_client"):
            raise AuthError(js.get("error_description", err)[:300])
        raise RuntimeError(f"Token endpoint {r.status_code}: {js.get('error_description', r.text[:200])}")
    return {"access_token": js["access_token"], "refresh_token": js.get("refresh_token", data.get("refresh_token")),
            "expires_at": int(time.time()) + int(js.get("expires_in", 3600)) - 120, "scope": js.get("scope", "")}


def exchange_code(code: str, code_verifier: str, client: Optional[httpx.Client] = None) -> Dict[str, Any]:
    return _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": config()["redirect_uri"],
                           "code_verifier": code_verifier}, client)


def refresh(tokens: Dict[str, Any], client: Optional[httpx.Client] = None) -> Dict[str, Any]:
    if not tokens.get("refresh_token"):
        raise AuthError("No refresh token stored")
    return _token_request({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}, client)


# ── Graph reads ───────────────────────────────────────────────────────────────


class Graph:
    """Minimal Graph client. `transport` lets tests plug in httpx.MockTransport."""

    def __init__(self, tokens: Dict[str, Any], on_refresh=None, transport: Optional[httpx.BaseTransport] = None):
        self.tokens = tokens
        self.on_refresh = on_refresh
        self.http = httpx.Client(timeout=60, transport=transport)

    def close(self):
        self.http.close()

    def _auth(self) -> Dict[str, str]:
        if self.tokens.get("expires_at", 0) <= time.time():
            self.tokens = refresh(self.tokens, self.http)
            if self.on_refresh:
                self.on_refresh(self.tokens)
        return {"Authorization": f"Bearer {self.tokens['access_token']}"}

    def get(self, url: str, params: Optional[Dict[str, str]] = None, prefer: Optional[List[str]] = None) -> Dict[str, Any]:
        headers = {**self._auth(), "Prefer": ", ".join(['outlook.timezone="UTC"', 'odata.maxpagesize=50'] + (prefer or []))}
        r = self.http.get(url if url.startswith("http") else GRAPH + url, params=params, headers=headers)
        if r.status_code == 401:                       # access token rejected early — refresh once
            self.tokens["expires_at"] = 0
            headers.update(self._auth())
            r = self.http.get(url if url.startswith("http") else GRAPH + url, params=params, headers=headers)
        if r.status_code == 429 or r.status_code == 503:
            raise RateLimited(int(r.headers.get("Retry-After", "60")))
        if r.status_code == 410:                       # delta token expired → caller restarts the stream
            raise LookupError("delta token expired")
        if r.status_code >= 400:
            raise RuntimeError(f"Graph {r.status_code}: {r.text[:300]}")
        return r.json()

    def me(self) -> Dict[str, Any]:
        return self.get("/me", {"$select": "id,displayName,mail,userPrincipalName"})

    def delta(self, stream: str, cursor: Optional[str], bodies: bool = False, max_pages: int = 40) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """→ (items, new deltaLink). Stream: 'inbox' | 'sent' | 'calendar'."""
        now = datetime.now(timezone.utc)
        prefer = ['outlook.body-content-type="text"'] if bodies else None
        if cursor:
            url, params = cursor, None
        elif stream == "calendar":
            url = "/me/calendarView/delta"
            params = {"startDateTime": (now - timedelta(days=INITIAL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "endDateTime": (now + timedelta(days=INITIAL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        else:
            folder = "inbox" if stream == "inbox" else "sentitems"
            url = f"/me/mailFolders/{folder}/messages/delta"
            params = {"$select": MAIL_SELECT + (",body" if bodies else ""),
                      "$filter": f"receivedDateTime ge {(now - timedelta(days=INITIAL_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')}"}
        items: List[Dict[str, Any]] = []
        for _ in range(max_pages):
            page = self.get(url, params, prefer)
            items.extend(page.get("value", []))
            if page.get("@odata.nextLink"):
                url, params = page["@odata.nextLink"], None
                continue
            return items, page.get("@odata.deltaLink")
        return items, url          # page budget used up: resume from the next page next time


# ── Normalisation ─────────────────────────────────────────────────────────────


def _addr(x: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    ea = (x or {}).get("emailAddress") or {}
    if not ea.get("address"):
        return None
    return {"email": ea["address"].strip().lower(), "name": (ea.get("name") or "").strip()}


def _dt(v: Optional[str]) -> Optional[datetime]:
    if not v:
        return None
    v = v.rstrip("Z")
    if "." in v:
        head, frac = v.split(".", 1)
        v = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(v).replace(tzinfo=timezone.utc)


def normalize_message(m: Dict[str, Any], mailbox: str, stream: str) -> Optional[Dict[str, Any]]:
    if m.get("@removed"):
        return {"removed": True, "external_id": m["id"], "kind": "email"}
    if m.get("isDraft"):
        return None
    sender = _addr(m.get("from"))
    to = [a for a in (_addr(x) for x in m.get("toRecipients") or []) if a]
    cc = [a for a in (_addr(x) for x in m.get("ccRecipients") or []) if a]
    outbound = stream == "sent" or (sender and sender["email"] == mailbox.lower())
    when = _dt(m.get("sentDateTime") if outbound else m.get("receivedDateTime")) or datetime.now(timezone.utc)
    body = ((m.get("body") or {}).get("content") or "").strip() or None
    return {"kind": "email", "external_id": m.get("internetMessageId") or m["id"], "graph_id": m["id"],
            "thread_id": m.get("conversationId"),
            "subject": (m.get("subject") or "(no subject)")[:300], "snippet": (m.get("bodyPreview") or "").strip()[:500] or None,
            "body": body, "occurred_at": when, "duration_min": None, "direction": "outbound" if outbound else "inbound",
            "participants": ([sender] if sender else []) + to + cc}


def normalize_event(e: Dict[str, Any], mailbox: str) -> Optional[Dict[str, Any]]:
    if e.get("@removed") or e.get("isCancelled"):
        return {"removed": True, "external_id": e.get("iCalUId") or e["id"], "graph_id": e["id"], "kind": "meeting"}
    start, end = _dt((e.get("start") or {}).get("dateTime")), _dt((e.get("end") or {}).get("dateTime"))
    if not start:
        return None
    people = [a for a in (_addr(x) for x in e.get("attendees") or []) if a]
    org = _addr(e.get("organizer"))
    if org and all(p["email"] != org["email"] for p in people):
        people.insert(0, org)
    ext = e.get("iCalUId") or e["id"]
    if e.get("type") == "occurrence":               # recurring series: one activity per occurrence
        ext = f"{ext}:{start:%Y%m%d}"
    return {"kind": "meeting", "external_id": ext, "graph_id": e["id"], "thread_id": None,
            "subject": (e.get("subject") or "(no subject)")[:300], "snippet": (e.get("bodyPreview") or "").strip()[:500] or None,
            "body": None, "occurred_at": start, "duration_min": int((end - start).total_seconds() // 60) if end else None,
            "direction": "outbound" if org and org["email"] == mailbox.lower() else "inbound", "participants": people,
            "online": bool(e.get("isOnlineMeeting"))}
