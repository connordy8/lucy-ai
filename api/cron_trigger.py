"""Vercel Cron handler — triggers the Lucy GitHub Actions workflow on an exact schedule."""

import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Verify this is a legitimate Vercel Cron invocation
        cron_secret = os.environ.get("CRON_SECRET", "").strip()
        auth_header = self.headers.get("Authorization", "")
        if not cron_secret or auth_header != f"Bearer {cron_secret}":
            self.send_response(401)
            self.end_headers()
            msg = "Unauthorized"
            self.wfile.write(msg.encode())
            return

        gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not gh_token:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"GITHUB_TOKEN not configured")
            return

        # Determine which call type based on query param
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(self.path).query)
        command = query.get("command", ["call"])[0]
        allowed = {"call", "call-evening", "test", "post-process", "update"}
        if command not in allowed:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(f"Invalid command: {command}".encode())
            return

        # Trigger the GitHub Actions workflow_dispatch
        url = "https://api.github.com/repos/connordy8/lucy-ai/actions/workflows/lucy-call.yml/dispatches"
        payload = json.dumps({
            "ref": "main",
            "inputs": {"command": command}
        }).encode()

        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {gh_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            resp = urllib.request.urlopen(req)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(f"Triggered workflow with command={command} (status {resp.status})".encode())
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"Failed to trigger workflow: {e}".encode())
