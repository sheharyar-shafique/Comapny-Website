"""Run with: python -m unittest discover -s tests -v"""
import json
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import server


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.ATTEMPTS.clear()
        server.GLOBAL_ATTEMPTS.clear()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.SiteHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path):
        try:
            with urlopen(f"http://127.0.0.1:{self.port}{path}") as res:
                return res.status, res.read()
        except HTTPError as ex:
            return ex.code, ex.read()

    def post(self, content, extra_headers=None):
        raw=json.dumps(content).encode()
        req=Request(f"http://127.0.0.1:{self.port}/api/contact",data=raw,headers={"Content-Type":"application/json", **(extra_headers or {})},method="POST")
        try:
            with urlopen(req) as res:
                return res.status,json.loads(res.read())
        except HTTPError as ex:
            return ex.code,json.loads(ex.read())

    def test_site_and_private_file(self):
        self.assertEqual(self.get('/')[0],200)
        self.assertEqual(self.get('/contact.html')[0],200)
        self.assertEqual(self.get('/support.html')[0],200)
        self.assertEqual(self.get('/services/mobile-app-development.html')[0],200)
        self.assertEqual(self.get('/assets/contact.js')[0],200)
        self.assertEqual(self.get('/.env')[0],404)
        self.assertEqual(self.get('/backend/server.py')[0],404)
        self.assertEqual(self.get('/api/health')[0],200)

    def test_clean_urls_and_legacy_redirects(self):
        from http.client import HTTPConnection

        for path in ("/", "/about", "/services", "/contact", "/support",
                     "/services/mobile-app-development",
                     "/services/tiktok-shop-account-management"):
            with self.subTest(clean_url=path):
                self.assertEqual(self.get(path)[0], 200)

        catalog = json.loads((server.ROOT / "services-catalog.json").read_text(encoding="utf-8"))
        for entry in catalog:
            with self.subTest(service=entry["slug"]):
                self.assertEqual(self.get("/services/" + entry["slug"])[0], 200)

        for old_path, expected in (
            ("/index.html", "/"),
            ("/about.html", "/about"),
            ("/services.html?campaign=summer", "/services?campaign=summer"),
            ("/contact.html?service=AI", "/contact?service=AI"),
            ("/support.html", "/support"),
            ("/services/mobile-app-development.html", "/services/mobile-app-development"),
            ("/services/unknown-service.html", None),
        ):
            with self.subTest(old_url=old_path):
                connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
                try:
                    connection.request("GET", old_path)
                    result = connection.getresponse()
                    if expected is None:
                        self.assertEqual(result.status, 404)
                    else:
                        self.assertEqual(result.status, 301)
                        self.assertEqual(result.getheader("Location"), expected)
                    result.read()
                finally:
                    connection.close()

        self.assertEqual(self.get("/services/../../backend/server.py")[0], 404)
        for file in [server.ROOT / "index.html", *sorted((server.ROOT / "services").glob("*.html")),
                     *(server.ROOT / name for name in ("about.html", "services.html", "contact.html", "support.html"))]:
            content = file.read_text(encoding="utf-8")
            with self.subTest(navigation_file=file.name):
                self.assertNotRegex(content, r"""href=["'][^"']*[.]html(?:[?#"'])""")

    def test_new_logo_assets_on_every_page(self):
        from pathlib import Path
        pages = [server.ROOT / "index.html", server.ROOT / "services.html",
                 server.ROOT / "about.html", server.ROOT / "contact.html",
                 server.ROOT / "support.html", *sorted((server.ROOT / "services").glob("*.html"))]
        self.assertEqual(len(pages), 34)
        for page in pages:
            with self.subTest(page=page.name):
                html = page.read_text(encoding="utf-8")
                self.assertIn('src="/assets/aozal-logo.svg"', html)
                self.assertIn('href="/assets/aozal-brand.css"', html)
                self.assertIn('href="/assets/aozal-icon.svg"', html)
        for path in ("/assets/aozal-logo.svg", "/assets/aozal-icon.svg", "/assets/aozal-brand.css"):
            with self.subTest(asset=path):
                self.assertEqual(self.get(path)[0], 200)

    def test_mern_team_profile_is_visible(self):
        about = (server.ROOT / "about.html").read_text(encoding="utf-8")
        self.assertIn('class="teammate mern-teammate"', about)
        self.assertIn('src="/assets/mern-stack-developer.jpg"', about)
        self.assertIn('MERN Stack Web Developer', about)
        self.assertIn('MongoDB', about)
        self.assertEqual(self.get("/assets/mern-stack-developer.jpg")[0], 200)
        self.assertEqual(self.get("/about")[0], 200)

    def test_reject_missing_fields(self):
        status,body=self.post({'type':'support','name':'A','email':'not-email','message':'ok'})
        self.assertEqual(status,400)
        self.assertIn('error',body)

    def test_delivery_inquiry_and_support(self):
        with patch.object(server,'send_notification') as send:
            msg={'name':'Example Client','email':'client@example.com','message':'I would like to discuss an AI project for my business.'}
            status,data=self.post(dict(msg,type='inquiry',service='AI Services'))
            self.assertEqual(status,200)
            self.assertTrue(data['ok'])
            status,data=self.post(dict(msg,type='support',subject='Issue with my app'))
            self.assertEqual(status,200)
            self.assertTrue(data['ok'])
            self.assertEqual(send.call_count,2)
            self.assertEqual(send.call_args.args[0]['type'],'support')

    def test_no_false_success_on_send_failure(self):
        server.ATTEMPTS.clear()
        server.GLOBAL_ATTEMPTS.clear()
        with patch.object(server,'send_notification',side_effect=RuntimeError('SMTP not configured')):
            status,data=self.post({'type':'inquiry','name':'Example Client','email':'client@example.com','message':'I need information about AOZAL Services.'})
            self.assertEqual(status,503)
            self.assertIn('error',data)

    def test_reject_cross_origin(self):
        server.ATTEMPTS.clear()
        server.GLOBAL_ATTEMPTS.clear()
        data={'type':'inquiry','name':'Example Client','email':'client@example.com','message':'Please quote my mobile app project.'}
        status, body = self.post(data, {'Origin':'https://malicious.example'})
        self.assertEqual(status, 403)
        self.assertIn('error', body)

    def test_security_headers_and_no_private_files(self):
        with urlopen(f"http://127.0.0.1:{self.port}/index.html") as res:
            csp = res.headers.get('Content-Security-Policy')
            self.assertIn("script-src 'self' 'sha256-", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertEqual(res.headers.get('X-Content-Type-Options'), 'nosniff')
        for path in ['/..%2F.env','/assets/..%2Fbackend/server.py','/assets/.env','/backend/server.py']:
            self.assertEqual(self.get(path)[0], 404)

    def test_spoofed_proxy_header_not_trusted_by_default(self):
        with patch.dict(os.environ, {"TRUSTED_PROXY_HOPS":"0"}):
            self.assertEqual(server.requesting_ip({"X-Forwarded-For":"9.9.9.9"},"127.0.0.1"), "127.0.0.1")
        with patch.dict(os.environ, {"TRUSTED_PROXY_HOPS":"1"}):
            self.assertEqual(server.requesting_ip({"X-Forwarded-For":"9.9.9.9, 8.8.8.8"},"127.0.0.1"), "8.8.8.8")
            self.assertEqual(server.requesting_ip({"X-Forwarded-For":"malformed"},"127.0.0.1"), "127.0.0.1")

    def test_resend_api_and_routing_without_network(self):
        from io import BytesIO
        msg={'type':'inquiry','name':'Example Client','email':'client@example.com','subject':'hello','service':'AI Services','reference':'','message':'Please send project information.'}
        class Response:
            status=200
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,size=-1): return b'{"id":"email_mock_123"}'
        config={
            'EMAIL_PROVIDER':'resend', 'RESEND_API_KEY':'re_test_secret',
            'RESEND_FROM':'AOZAL Website <notifications@notify.aozaltechnologies.com>',
            'INFO_EMAIL':'info@aozaltechnologies.com',
            'SUPPORT_EMAIL':'support@aozaltechnologies.com'
        }
        with patch.dict(os.environ,config), patch.object(server.urllib.request,'urlopen',return_value=Response()) as sender:
            server.send_notification(msg)
            request=sender.call_args.args[0]
            self.assertEqual(request.full_url,'https://api.resend.com/emails')
            self.assertEqual(request.get_header('Authorization'),'Bearer re_test_secret')
            payload=json.loads(request.data)
            self.assertEqual(payload['to'],['info@aozaltechnologies.com'])
            self.assertEqual(payload['reply_to'],'client@example.com')
            self.assertEqual(payload['from'],config['RESEND_FROM'])
            self.assertIn('AI Services',payload['subject'])
            server.send_notification(dict(msg,type='support',subject='Need Help'))
            self.assertEqual(json.loads(sender.call_args.args[0].data)['to'], ['support@aozaltechnologies.com'])

    def test_resend_rejects_unconfigured_or_remote_failure(self):
        from urllib.error import HTTPError
        data={'type':'inquiry','name':'Test Client','email':'client@example.com','subject':'Inquiry','service':'','reference':'','message':'Hello from tests.'}
        with patch.dict(os.environ,{'EMAIL_PROVIDER':'resend','RESEND_API_KEY':'','RESEND_FROM':''}):
            with self.assertRaises(RuntimeError): server.send_notification(data)
        with patch.dict(os.environ,{'EMAIL_PROVIDER':'resend','RESEND_API_KEY':'re_test','RESEND_FROM':'notify@notify.aozaltechnologies.com'}):
            with patch.object(server.urllib.request,'urlopen',side_effect=HTTPError('https://api.resend.com/emails',403,'not verified',{},None)):
                with self.assertRaisesRegex(RuntimeError,'HTTP 403'):server.send_notification(data)

    def test_email_routing(self):
        msg={'type':'inquiry','name':'Example Client','email':'client@example.com','subject':'hello','service':'AI Services','reference':'','message':'Please send project information.'}
        fake_env={'SMTP_HOST':'mail.example.com','SMTP_PORT':'465','SMTP_SECURITY':'ssl','SMTP_USER':'sender@example.com','SMTP_PASSWORD':'secret','SMTP_FROM':'sender@example.com','INFO_EMAIL':'info@aozaltechnologies.com','SUPPORT_EMAIL':'support@aozaltechnologies.com'}
        with patch.dict(os.environ,dict(fake_env,EMAIL_PROVIDER='smtp')),patch.object(server.smtplib,'SMTP_SSL') as smtp:
            server.send_notification(msg)
            sent=smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertEqual(sent['To'],'info@aozaltechnologies.com')
            self.assertEqual(sent['Reply-To'],'client@example.com')
            msg['type']='support';msg['subject']='Help'
            server.send_notification(msg)
            sent=smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertEqual(sent['To'],'support@aozaltechnologies.com')


if __name__=='__main__':unittest.main()
