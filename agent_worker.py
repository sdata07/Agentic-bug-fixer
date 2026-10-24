import argparse
import json
import re
import subprocess
import time
from pathlib import Path
from urllib import parse, request

WORKSPACE = Path(__file__).resolve().parent
FEEDBACK_FILE = WORKSPACE / "feedback.json"
HOME_FILE = WORKSPACE / "home.html"
AGENT_LOG_FILE = WORKSPACE / "agent_changes.log"
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "qwen2.5-coder:7b"

parser = argparse.ArgumentParser()
parser.add_argument("--once", action="store_true")
args = parser.parse_args()


def ensure_ollama_running() -> bool:
    try:
        with request.urlopen("http://localhost:11434/api/tags", timeout=2):
            return True
    except Exception:
        pass

    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(30):
            try:
                with request.urlopen("http://localhost:11434/api/tags", timeout=2):
                    return True
            except Exception:
                time.sleep(0.5)
    except Exception:
        return False

    return False


def load_feedback() -> list:
    if not FEEDBACK_FILE.exists():
        return []
    try:
        data = json.loads(FEEDBACK_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_feedback(entries: list) -> None:
    FEEDBACK_FILE.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def call_ollama(prompt: str) -> str:
    if not ensure_ollama_running():
        return "ERROR: Local Ollama server is not available"

    payload = {
        "model": MODEL_NAME,
        "stream": False,
        "prompt": prompt,
        "options": {"temperature": 0.0, "num_predict": 250},
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(OLLAMA_URL, data=data, headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as exc:
        return f"ERROR: {exc}"

    try:
        parsed = json.loads(raw)
        return parsed.get("response", "").strip()
    except Exception:
        return raw.strip()


def discover_candidate_files() -> list[Path]:
    candidates: list[Path] = []
    for path in WORKSPACE.iterdir():
        if path.name in {".git", ".venv", "__pycache__", ".pytest_cache"}:
            continue
        if path.is_file() and path.suffix.lower() in {".html", ".py", ".js", ".css", ".json", ".md", ".txt"}:
            candidates.append(path)
    return sorted(candidates)


def build_prompt(feedback_entry: dict) -> str:
    message = feedback_entry.get("message", "")
    category = feedback_entry.get("category", "bug")
    page_url = feedback_entry.get("page_url", "")

    candidate_files = discover_candidate_files()
    file_contexts: list[str] = []
    for path in candidate_files:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        snippet = text[:1200].replace("```", "")
        file_contexts.append(f"FILE: {path.name}\n```\n{snippet}\n```\n")

    repo_text = "\n\n".join(file_contexts) if file_contexts else "No project files found."

    return (
        "You are a senior frontend engineer working on a small local project.\n\n"
        "Browse only the relevant project files below and choose the exact file that needs to change.\n\n"
        f"Relevant project files:\n{repo_text}\n\n"
        "User feedback:\n"
        f"- page_url: {page_url}\n"
        f"- category: {category}\n"
        f"- message: {message}\n\n"
        "Goal:\n"
        "- choose the correct file to edit\n"
        "- identify the exact text that should be replaced\n"
        "- return only valid JSON in this exact shape:\n"
        '{"file": "relative/path/to/file", "replacement": "old text|new text", "summary": "what changed"}\n\n'
        "Rules:\n"
        "- Use the file contents above as the source of truth.\n"
        "- Only choose a file that actually exists in this project.\n"
        "- Do not guess a file name.\n"
        "- Do not return markdown fences.\n"
        "- If the issue is unclear, return {\"file\": null, \"summary\": \"Need more context\"}\n"
    )


def extract_json_object(raw_text: str):
    text = raw_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text, flags=re.I)

    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None

    candidate = match.group(0)
    try:
        return json.loads(candidate)
    except Exception:
        return None


def normalize_replacement(replacement: str) -> tuple[str, str] | None:
    if not isinstance(replacement, str):
        return None
    cleaned = replacement.strip()
    if "|" not in cleaned:
        return None
    left, right = cleaned.split("|", 1)
    left = left.strip()
    right = right.strip()
    if not left or not right or left == right:
        return None
    return left, right


def choose_safe_pair(file_text: str, left: str, right: str) -> tuple[str, str] | None:
    left_in_file = left in file_text
    right_in_file = right in file_text

    if left_in_file and not right_in_file and left != right:
        return left, right
    if right_in_file and not left_in_file and left != right:
        return right, left
    if left_in_file and right_in_file and left != right:
        return left, right
    return None


def apply_text_fix(file_path: Path, old: str, new: str) -> bool:
    text = file_path.read_text(encoding="utf-8")
    if old not in text or old == new:
        return False
    file_path.write_text(text.replace(old, new), encoding="utf-8")
    return True


def get_model_fix(entry: dict) -> str:
    primary = build_prompt(entry)
    first = call_ollama(primary)
    if first and not first.startswith("ERROR:"):
        return first

    fallback = (
        "You are fixing a typo in a local web app.\n"
        "Return only JSON in the form {\"file\": \"home.html\", \"replacement\": \"old|new\", \"summary\": \"what changed\"}.\n"
        f"User report: {entry.get('message', '')}\n"
        "Use only text that is actually present in the project files above.\n"
    )
    return call_ollama(fallback)


def extract_typos_from_message(message: str) -> list[tuple[str, str]]:
    text = message.strip()
    if not text:
        return []

    patterns = [
        r"(?i)\b([A-Za-z][A-Za-z'-]*)\s+(?:instead of|should be|not|to)\s+([A-Za-z][A-Za-z'-]*)\b",
        r"(?i)\b([A-Za-z][A-Za-z'-]*)\s+should\s+be\s+([A-Za-z][A-Za-z'-]*)\b",
        r"(?i)\b([A-Za-z][A-Za-z'-]*)\s+instead\s+of\s+([A-Za-z][A-Za-z'-]*)\b",
    ]

    pairs: list[tuple[str, str]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            left, right = match.groups()
            if left and right and left.lower() != right.lower():
                pairs.append((left, right))
    return pairs


def choose_message_fix(file_text: str, message: str) -> tuple[str, str] | None:
    if not message:
        return None

    candidates = extract_typos_from_message(message)
    if not candidates:
        return None

    for left, right in candidates:
        left_lower = left.lower()
        right_lower = right.lower()
        if left_lower in file_text.lower() and right_lower not in file_text.lower():
            return left, right
        if right_lower in file_text.lower() and left_lower not in file_text.lower():
            return right, left
        if left in file_text and right not in file_text:
            return left, right
        if right in file_text and left not in file_text:
            return right, left
    return None


def log_change(entry: dict, old_text: str, new_text: str) -> None:
    timestamp = __import__("datetime").datetime.utcnow().isoformat() + "Z"
    line = f"[{timestamp}] {entry.get('message', '')} -> replaced '{old_text}' with '{new_text}'\n"
    with AGENT_LOG_FILE.open("a", encoding="utf-8") as log_file:
        log_file.write(line)


def process_feedback_item(entry: dict) -> dict:
    if entry.get("status") == "resolved":
        return entry

    file_text = HOME_FILE.read_text(encoding="utf-8") if HOME_FILE.exists() else ""
    message_fix = choose_message_fix(file_text, entry.get("message", ""))
    if message_fix:
        old_text, new_text = message_fix
        if apply_text_fix(HOME_FILE, old_text, new_text):
            log_change(entry, old_text, new_text)
            entry["status"] = "resolved"
            entry["resolution"] = f"Fixed typo from {old_text} to {new_text}"
            return entry

    raw_response = get_model_fix(entry)
    entry["agent_note"] = raw_response
    entry["status"] = "processing"

    if raw_response.startswith("ERROR:"):
        entry["status"] = "failed"
        entry["resolution"] = "Local Ollama unavailable; model could not process the issue"
        return entry

    parsed = extract_json_object(raw_response)
    if not isinstance(parsed, dict):
        entry["status"] = "failed"
        entry["resolution"] = "Agent response was not valid JSON"
        return entry

    file_name = parsed.get("file")
    replacement = parsed.get("replacement")
    if file_name and replacement:
        pair = normalize_replacement(replacement)
        if pair:
            old_text, new_text = pair
            target = WORKSPACE / str(file_name)
            if target.exists():
                file_text = target.read_text(encoding="utf-8")
                safe_pair = choose_safe_pair(file_text, old_text, new_text)
                if safe_pair and apply_text_fix(target, safe_pair[0], safe_pair[1]):
                    log_change(entry, safe_pair[0], safe_pair[1])
                    entry["status"] = "resolved"
                    entry["resolution"] = parsed.get("summary", "Automatic fix applied by local agent")
                    return entry

    entry["status"] = "failed"
    entry["resolution"] = "Agent could not find a safe fix automatically"
    return entry


def run_agent_once() -> None:
    entries = load_feedback()
    if not entries:
        print("No new feedback to process.")
        return

    newest = entries[0]
    processed = process_feedback_item(newest)
    remaining = [entry for entry in entries[1:] if entry.get("status") != "resolved"]

    if processed.get("status") == "resolved":
        save_feedback(remaining)
    else:
        save_feedback([processed, *remaining])

    print(f"Processed feedback item: {processed.get('status', 'unknown')}")


if __name__ == "__main__":
    if args.once:
        run_agent_once()
    else:
        run_agent_once()
