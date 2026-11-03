"""AOZAL website + contact email API (Render Free + future AWS compatible).

Uses Resend HTTPS API by default, with optional SMTP for hosts that permit it.
All delivery keys must be configured as server-side environment variables.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import re
import smtplib
import urllib.error
import urllib.request
import ssl
import time
from collections import defaultdict, deque
from email.message import EmailMessage
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Lock
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")
MAX_BODY_BYTES = 16_384
RATE_LIMIT = 5
RATE_WINDOW_SECONDS = 15 * 60
GLOBAL_RATE_LIMIT = 100  # Coarse process-level abuse budget, supplement with edge protections.
MAX_TRACKED_IPS = 10_000
MAX_REQUEST_WORKERS = 16
ATTEMPTS: dict[str, deque[float]] = defaultdict(deque)
ATTEMPTS_LOCK = Lock()
GLOBAL_ATTEMPTS: deque[float] = deque()
STATIC_EXTENSIONS = {".html", ".css", ".js", ".jpg", ".jpeg", ".png", ".webp", ".svg", ".ico", ".json", ".woff", ".woff2"}
MIMETYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8"}


def load_local_env() -> None:
    """Read .env locally; real environment variables always take precedence."""
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def clean_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    # Remove control characters to protect email headers and plain text output.
    value = "".join(ch for ch in value if ch in "\n\t" or ord(ch) >= 32)
    return value.strip()[: limit + 1]


def validate_submission(payload: object) -> tuple[dict[str, str] | None, str | None]:
    if not isinstance(payload, dict):
        return None, "Invalid form submission."
    kind = clean_text(payload.get("type", "inquiry"), 24).lower()
    if kind not in {"inquiry", "support"}:
        return None, "Unknown request type."
    name = clean_text(payload.get("name"), 100)
    email = clean_text(payload.get("email"), 254)
    subject = clean_text(payload.get("subject", ""), 140)
    service = clean_text(payload.get("service", ""), 140)
    reference = clean_text(payload.get("reference", ""), 120)
    message = clean_text(payload.get("message") or payload.get("project"), 5000)
    website = clean_text(payload.get("website", ""), 500)
    if not 2 <= len(name) <= 100:
        return None, "Please provide your name (2–100 characters)."
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        return None, "Please enter a valid email address."
    if len(message) < 10 or len(message) > 5000:
        return None, "Please enter a message between 10 and 5,000 characters."
    if len(subject) > 140 or len(service) > 140 or len(reference) > 120:
        return None, "One of the fields is too long."
    if kind == "support" and len(subject) < 3:
        return None, "Please describe the subject of your support request."
    return {"type": kind, "name": name, "email": email, "subject": subject, "service": service,
            "reference": reference, "message": message, "website": website}, None


def smtp_configured() -> bool:
    return all(os.environ.get(key, "").strip() for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"))


def notification_details(data: dict[str, str]) -> tuple[str, str, str]:
    kind = data["type"]
    destination = os.environ.get("SUPPORT_EMAIL", "support@aozaltechnologies.com") if kind == "support" else os.environ.get("INFO_EMAIL", "info@aozaltechnologies.com")
    category = "Support request" if kind == "support" else "General / project inquiry"
    title = data["subject"] if kind == "support" else (data["service"] or "Website inquiry")
    title = " ".join(title.split())[:100]

    body = "\n".join((
        f"Request: {category}",
        f"Name: {data['name']}",
        f"Email: {data['email']}",
        f"Subject: {data['subject'] or '—'}",
        f"Service: {data['service'] or '—'}",
        f"Reference: {data['reference'] or '—'}",
        "", "Message:", data["message"], "", "Sent through aozaltechnologies.com"
    ))
    return destination, f"[AOZAL] {category}: {title}", body


def send_via_resend(data: dict[str, str]) -> None:
    """Send verified-domain transactional email over HTTPS (works on Render Free)."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender = os.environ.get("RESEND_FROM", "").strip()
    if not api_key or not sender:
        raise RuntimeError("Resend API key/sender not configured")
    destination, subject, body = notification_details(data)
    # The sender must belong to a domain verified in the owner's Resend account.
    # The client's email is Reply-To, never From, to prevent spoofed From headers.
    payload = json.dumps({
        "from": sender,
        "to": [destination],
        "reply_to": data["email"],
        "subject": subject,
        "text": body,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "AOZAL-website-contact/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            if response.status not in (200, 201):
                raise RuntimeError(f"Email API rejected submission with HTTP {response.status}")
            response_body = json.loads(response.read(8192))
            if not isinstance(response_body, dict) or not response_body.get("id"):
                raise RuntimeError("Email API did not confirm acceptance")
    except urllib.error.HTTPError as exc:
        # Never put API response data or the Bearer key into user-visible output.
        raise RuntimeError(f"Email API returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("Email API could not be reached or returned invalid data") from exc


def send_via_smtp(data: dict[str, str]) -> None:
    if not smtp_configured():
        raise RuntimeError("SMTP is not configured")
    destination, subject, body = notification_details(data)
    sender = os.environ.get("SMTP_FROM", os.environ["SMTP_USER"]).strip()
    email = EmailMessage()
    email["From"] = sender
    email["To"] = destination
    email["Reply-To"] = data["email"]
    email["Subject"] = subject
    email.set_content(body)
    host = os.environ["SMTP_HOST"].strip()
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"].strip()
    password = os.environ["SMTP_PASSWORD"]
    security = os.environ.get("SMTP_SECURITY", "ssl").lower().strip()
    context = ssl.create_default_context()
    if security == "ssl":
        with smtplib.SMTP_SSL(host, port, timeout=15, context=context) as connection:
            connection.login(user, password)
            connection.send_message(email)
    elif security == "starttls":
        with smtplib.SMTP(host, port, timeout=15) as connection:
            connection.ehlo()
            connection.starttls(context=context)
            connection.ehlo()
            connection.login(user, password)
            connection.send_message(email)
    else:
        raise ValueError("SMTP_SECURITY must be ssl or starttls")


def send_notification(data: dict[str, str]) -> None:
    provider = os.environ.get("EMAIL_PROVIDER", "resend").strip().lower()
    if provider == "resend":
        send_via_resend(data)
    elif provider == "smtp":
        send_via_smtp(data)
    else:
        raise RuntimeError("Unsupported EMAIL_PROVIDER")


def requesting_ip(headers, peer_ip: str) -> str:
    """Trust only a configured number of proxies, never untrusted XFF prefixes.

    Set TRUSTED_PROXY_HOPS only if the backend cannot be reached except through
    that reverse proxy. The rightmost entries are the most recently added hops.
    """
    try:
        hops = int(os.environ.get("TRUSTED_PROXY_HOPS", "0"))
    except ValueError:
        hops = 0
    if not 1 <= hops <= 5:
        return peer_ip
    xff = headers.get("X-Forwarded-For", "")
    values = [value.strip() for value in xff.split(",")]
    if len(values) < hops or len(xff) > 1024:
        return peer_ip
    try:
        return str(ipaddress.ip_address(values[-hops]))
    except ValueError:
        return peer_ip


def allowed_origin(headers) -> bool:
    """Block cross-origin browser forms. Bots may forge Origin; use edge checks too."""
    origin = headers.get("Origin")
    if not origin:
        return True  # Non-browser clients may omit Origin.
    host = headers.get("Host", "").strip().lower()
    scheme = headers.get("X-Forwarded-Proto", "http").split(",", 1)[0].strip().lower()
    if not host or scheme not in {"http", "https"}:
        return False
    try:
        parsed = urlsplit(origin)
        return (parsed.scheme.lower() == scheme and parsed.netloc.lower() == host
                and parsed.username is None and parsed.password is None
                and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def allowed_request(client_ip: str) -> bool:
    now = time.monotonic()
    with ATTEMPTS_LOCK:
        while GLOBAL_ATTEMPTS and GLOBAL_ATTEMPTS[0] <= now - RATE_WINDOW_SECONDS:
            GLOBAL_ATTEMPTS.popleft()
        if len(GLOBAL_ATTEMPTS) >= GLOBAL_RATE_LIMIT:
            return False
        # Limit memory usage even when clients rotate many source addresses.
        if client_ip not in ATTEMPTS and len(ATTEMPTS) >= MAX_TRACKED_IPS:
            for ip, events in list(ATTEMPTS.items()):
                if not events or events[-1] <= now - RATE_WINDOW_SECONDS:
                    del ATTEMPTS[ip]
            if len(ATTEMPTS) >= MAX_TRACKED_IPS:
                return False
        q = ATTEMPTS[client_ip]
        while q and q[0] <= now - RATE_WINDOW_SECONDS:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return False
        q.append(now)
        GLOBAL_ATTEMPTS.append(now)
        return True


def inline_script_hashes() -> list[str]:
    """Hash first-party inline JS to enforce CSP without unsafe-inline scripts."""
    hashes = []
    for sitefile in (ROOT / "index.html", ROOT / "services.html", ROOT / "about.html"):
        if not sitefile.exists():
            continue
        html = sitefile.read_text(encoding="utf-8")
        for match in re.finditer(r"<script(?:\s[^>]*)?>(.*?)</script\s*>", html, re.I | re.S):
            source = match.group(1)
            digest = base64.b64encode(hashlib.sha256(source.encode("utf-8")).digest()).decode("ascii")
            hashes.append("'sha256-" + digest + "'")
    return hashes


CSP_SCRIPT_HASHES = inline_script_hashes()
CSP = "; ".join((
    "default-src 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "script-src 'self' " + " ".join(CSP_SCRIPT_HASHES),
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self'",
))


class CappedHTTPServer(ThreadingHTTPServer):
    """Prevent unlimited email-sending connections/threads exhausting the host."""
    daemon_threads = True
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._capacity = BoundedSemaphore(MAX_REQUEST_WORKERS)

    def process_request(self, request, client_address):
        if not self._capacity.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._capacity.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._capacity.release()


class SiteHandler(BaseHTTPRequestHandler):
    server_version = "AozalSite"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def common_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        # Render/AWS reverse proxy terminates HTTPS; browsers ignore HSTS over HTTP.
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            self.send_header("Strict-Transport-Security", "max-age=31536000")

    def json_response(self, status: int, body: dict, headers: dict[str, str] | None = None) -> None:
        content = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.common_headers()
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if path == "/api/health":
            self.json_response(200, {"status": "ok"})
            return
        if path.startswith("/api/"):
            self.json_response(404, {"error": "Not found."})
            return
        if path == "/":
            path = "/index.html"
        # All publicly served files are explicitly located under the site root,
        # services/ or assets/. Deny private files and source code.
        parts = Path(path.removeprefix("/")).parts
        if (not parts or any(part in {".", ".."} or part.startswith(".") for part in parts)
                or len(parts) > 2
                or not ((len(parts) == 1 and parts[0] in {"index.html", "about.html", "services.html", "contact.html", "support.html", "services-catalog.json"})
                        or (len(parts) == 2 and parts[0] in {"assets", "services"}))):
            self.send_error(404)
            return
        target = (ROOT / Path(*parts)).resolve()
        if not target.is_file() or not target.is_relative_to(ROOT) or target.suffix.lower() not in STATIC_EXTENSIONS:
            self.send_error(404)
            return
        try:
            content = target.read_bytes()
        except OSError:
            self.send_error(404)
            return
        content_type = MIMETYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache" if target.suffix == ".html" else "public, max-age=3600")
        self.common_headers()
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/api/contact":
            self.json_response(404, {"error": "Not found."})
            return
        if not allowed_origin(self.headers):
            self.json_response(403, {"error": "Cross-origin submissions are not allowed."})
            return
        if not self.headers.get("Content-Type", "").lower().split(";", 1)[0].strip() == "application/json":
            self.json_response(415, {"error": "Send JSON data."})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        if size <= 0 or size > MAX_BODY_BYTES:
            self.json_response(413, {"error": "Request is too large or empty."})
            return
        try:
            data, error = validate_submission(json.loads(self.rfile.read(size)))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.json_response(400, {"error": "Invalid JSON data."})
            return
        if error:
            self.json_response(400, {"error": error})
            return
        assert data is not None
        # A honeypot field traps simple spam bots without delivering any email.
        if data["website"]:
            self.json_response(200, {"ok": True, "message": "Thanks, your message has been received."})
            return
        client_ip = requesting_ip(self.headers, self.client_address[0])
        if not allowed_request(client_ip):
            self.json_response(429, {"error": "Too many messages. Please try again later."}, {"Retry-After": "900"})
            return
        try:
            send_notification(data)
        except (OSError, RuntimeError, ValueError, smtplib.SMTPException):
            logging.exception("Contact email delivery failed")
            self.json_response(503, {"error": "We couldn't deliver your message right now. Please email us directly."})
            return
        self.json_response(200, {"ok": True, "message": "Your message has been sent successfully."})


if __name__ == "__main__":
    load_local_env()
    port = int(os.environ.get("PORT", "8000"))
    bind = os.environ.get("HOST", "127.0.0.1")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    server = CappedHTTPServer((bind, port), SiteHandler)
    print(f"AOZAL website listening on http://{bind}:{port}", flush=True)
    provider = os.environ.get("EMAIL_PROVIDER", "resend").strip().lower()
    if provider == "resend" and not (os.environ.get("RESEND_API_KEY") and os.environ.get("RESEND_FROM")):
        print("WARNING: Resend is not configured; contact forms will not send until environment variables are set.", flush=True)
    elif provider == "smtp" and not smtp_configured():
        print("WARNING: SMTP is not configured; contact forms will not send.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping AOZAL website server.")
    finally:
        server.server_close()
