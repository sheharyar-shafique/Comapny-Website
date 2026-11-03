# AOZAL Technologies — Render Free deployment

**Included:** complete premium website (Home, Services, About Us, Contact, Support and 29 service-detail pages); Founder/Director/team photos; Python email API; all contact forms. No external Python packages required.

## What is configured and what is not
- **Configured in code:** HTTPS email API (Resend), contact routing to `info@aozaltechnologies.com`, support routing to `support@aozaltechnologies.com`, Reply-To set to the visitor email, request validation, rate limits, basic anti-spam, browser security headers.
- **Still required from the domain owner:** Resend account/API key and verification of an outbound sending subdomain, Render deployment, DNS hookup, and live mail tests. No real emails were sent during automated tests.
- **Namecheap Private Email inboxes remain your actual receiving inboxes.** Resend is only the outbound *notification delivery* service. DO NOT change your root domain MX records or remove Namecheap Private Email SPF/DKIM records.

## Why HTTPS email API?
Render's **Free** tier blocks SMTP ports 25, 465 and 587. Thus the original Namecheap SMTP setup will not work there. Resend's `POST https://api.resend.com/emails` uses HTTPS port 443 instead. The free Resend tier currently includes 3,000 transactional emails/month with a maximum of 100/day; limits and provider policies can change.

## Step 1 — Set up Resend (free)
1. Sign up at https://resend.com and open **Domains > Add Domain**.
2. Enter **`notify.aozaltechnologies.com`**, to separate automated sending DNS from your existing Namecheap Private Email inboxes at the root domain.
3. Enable **sending only**, not receiving. Resend shows required DNS records (typically DKIM TXT/CNAME and SPF/return-path entries). **Copy the exact hostname/type/value shown in your own Resend account** into the DNS provider hosting your domain's active DNS zone (often Namecheap > Domain List > Manage > Advanced DNS). The actual DNS hostnames may vary. Do not create any root `@` MX or overwrite Namecheap Private Email DNS.
4. Wait for Resend domain status to show verified. Create a **sending-only API key** with permission to send from your verified domain. Save it privately; do not paste it into ChatGPT, website JavaScript, or a git repository.
5. Confirm that emails sent to `info@aozaltechnologies.com` and `support@aozaltechnologies.com` already arrive normally through Namecheap Private Email.

## Step 2 — Deploy on Render Free
1. Push the files in this folder into a **private** GitHub repository (or a repository of your choice). Keep the folder contents at repo root, so `backend/server.py` and `index.html` are directly present.
2. In Render, choose **New > Blueprint** and select the Git repository. `render.yaml` creates a Free Python web service automatically. Alternatively, **New > Web Service**, plan **Free**, build command `python -m compileall -q backend && python -m unittest discover -s tests -q`, start command `python backend/server.py`.
3. Set Render Environment variables: `HOST=0.0.0.0`, `EMAIL_PROVIDER=resend`, `RESEND_API_KEY=<your secret>` (mark as Secret), `RESEND_FROM=AOZAL Website <notifications@notify.aozaltechnologies.com>`, `INFO_EMAIL=info@aozaltechnologies.com`, `SUPPORT_EMAIL=support@aozaltechnologies.com`, `TRUSTED_PROXY_HOPS=0` (until proxy IP behavior is confirmed). `PORT` is supplied by Render.
4. Deploy, open `https://<render-service>.onrender.com/api/health` (should return `{ "status": "ok" }`) and browse `contact.html` / `support.html`. Submit a real inquiry and a real support request. Check both Namecheap inboxes (and spam). **The contact form cannot send until Resend is verified and configured.**
5. In Render **Settings > Custom Domains**, add `aozaltechnologies.com` (optionally `www.aozaltechnologies.com`), and use the exact A/CNAME/TXT DNS records Render displays for Namecheap. Leave email-related DNS alone. Verify HTTPS, then test both forms again using your actual domain.

**DNS caution:** Changing the nameservers or replacing root MX records can break Namecheap Private Email. Website A/CNAME records and email MX/SPF/DKIM/DMARC have different purposes. Preserve existing mail settings.

## Local preview
On Windows PowerShell: `Copy-Item .env.example .env`, then edit `.env` to use your Resend key, then `py backend/server.py`. On Linux/macOS: `cp .env.example .env && python3 backend/server.py`. Visit `http://127.0.0.1:8000`. Do not put secrets into source files or share them. Tests: `python -m unittest discover -s tests -v` (mock email only).

## AWS migration later
1. Deploy this same folder to EC2, ECS or another Python-capable AWS host, with HTTPS enabled via an Application Load Balancer or reverse proxy, and outbound HTTPS port 443 allowed.
2. Set the **same** `EMAIL_PROVIDER=resend`, `RESEND_API_KEY`, `RESEND_FROM`, `INFO_EMAIL`, `SUPPORT_EMAIL` as environment secrets (prefer AWS Secrets Manager/SSM). No change to contact forms or Resend is necessary.
3. Update only the **website** DNS A/CNAME/alias records from Render to AWS; **do not change Namecheap email MX records**. Monitor contact delivery on both sides before disabling Render.
4. If you later prefer Namecheap SMTP from AWS, set `EMAIL_PROVIDER=smtp` and the SMTP environment variables instead; verify AWS SMTP egress restrictions and server/provider settings before changing.

## Security and production limitations
Security has not been proven vulnerability-free. The server includes restrictive static-path allowlisting, CSP/hash-based inline JavaScript, `Reply-To` safety, basic input validation, request-size limit, in-memory per-IP and global rate limits, and same-origin checks. Add Cloudflare Turnstile (validated server-side), edge/WAF rate limiting, secret scanning, monitoring, and production penetration/security testing before relying on it for high-traffic public use. Render Free instances can spin down, leading to delayed first requests; free services have no uptime guarantee. Current rate limits are per-process, not distributed, and the `TRUSTED_PROXY_HOPS=0` default may treat proxied users as one IP; carefully validate your trusted proxy configuration before changing this setting.

### Form routing
- `POST /api/contact`, `type: inquiry` -> **info@aozaltechnologies.com**
- `POST /api/contact`, `type: support` -> **support@aozaltechnologies.com**
- Return success only after the mail provider accepts the message; final recipient delivery is not guaranteed until verified.
