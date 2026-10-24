import json
import os
import pathlib
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

HOST = "0.0.0.0"
PORT = 8000
DATA_FILE = "feedback.json"
AGENT_LOG = "agent_worker.log"


HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Sample Website</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 0; padding: 0; }
    .top-right { position: fixed; top: 10px; right: 10px; }
    .btn { background: black; color: white; padding: 10px 14px; border: none; cursor: pointer; }
    .content { padding: 60px 20px; max-width: 900px; margin: 0 auto; }
    .box { border: 1px solid #ccc; padding: 20px; margin-top: 20px; }
    a { text-decoration: none; }
  </style>
</head>
<body>
  <div class="top-right">
    <button class="btn" id="submitErrorBtn">Submit error</button>
  </div>

  <div class="content">
    <h1>Welcome to our site!</h1>
    <p>This is a normal website where people can banana stuff and maybe buy things.</p>

    <div class="box">
      <h2>Latest updates</h2>
      <p>We added a new product line and we are still testing some features.</p>
    </div>

    <div class="box">
      <h2>About us</h2>
      <p>We sell stuff online and try to make the user experience better.</p>
    </div>
  </div>

  <script>
    document.getElementById('submitErrorBtn').addEventListener('click', function () {
      const pageUrl = encodeURIComponent(window.location.href);
      window.location.href = '/bug-report?from=' + pageUrl;
    });
  </script>
</body>
</html>
"""


BASE_DIR = pathlib.Path(__file__).resolve().parent


def load_home_page() -> str:
    home_file = BASE_DIR / "home.html"
    if home_file.exists():
        return home_file.read_text(encoding="utf-8")
    return HTML_PAGE


def load_feedback() -> list:
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_feedback(entries: list) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as file:
        json.dump(entries, file, indent=2)


def trigger_agent() -> None:
    log_path = BASE_DIR / AGENT_LOG
    try:
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"[server] submit hit: starting one-shot worker at {__import__('datetime').datetime.utcnow().isoformat()}Z\n")

        completed = subprocess.run(
            [sys.executable, "agent_worker.py", "--once"],
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
            check=False,
        )

        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"[server] worker exit code={completed.returncode}\n")
            if completed.stdout:
                log_file.write(completed.stdout)
            if completed.stderr:
                log_file.write(completed.stderr)
    except subprocess.TimeoutExpired:
        try:
            with open(log_path, "a", encoding="utf-8") as log_file:
                log_file.write("[server] worker timed out after 180s\n")
        except Exception:
            pass
    except Exception as exc:
        try:
            with open(log_path, "a", encoding="utf-8") as log_file:
                log_file.write(f"[server] failed to spawn worker: {exc}\n")
        except Exception:
            pass


def load_bug_report_page() -> str:
    file_path = BASE_DIR / "bug_report.html"
    return file_path.read_text(encoding="utf-8")


class FeedbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            page = load_home_page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode("utf-8"))
            return

        if parsed.path == "/bug-report":
            page = load_bug_report_page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode("utf-8"))
            return

        if parsed.path == "/api/feedback":
            feedback = load_feedback()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(feedback).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path != "/api/feedback":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)

        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except json.JSONDecodeError:
            self.send_json({"error": "Invalid JSON payload."}, 400)
            return

        message = str(payload.get("message", "")).strip()
        page_url = str(payload.get("page_url", "")).strip()

        if not message or not page_url:
            self.send_json({"error": "message and page_url are required."}, 400)
            return

        feedback = load_feedback()
        entry = {
            "page_url": page_url,
            "message": message,
            "category": payload.get("category", "other"),
            "user_email": payload.get("user_email", ""),
            "browser_info": payload.get("browser_info", ""),
            "screenshot_url": payload.get("screenshot_url", ""),
            "created_at": payload.get("created_at") or __import__("datetime").datetime.utcnow().isoformat() + "Z",
            "status": "new",
        }
        feedback.insert(0, entry)
        save_feedback(feedback)
        trigger_agent()

        self.send_json({"status": "success", "entry": entry}, 201)

    def send_json(self, payload, status_code=200):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), FeedbackHandler)
    print(f"Feedback app running at http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.server_close()
