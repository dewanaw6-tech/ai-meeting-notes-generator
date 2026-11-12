"""AI Meeting Notes Generator - Flask backend.

Routes
    /                 Dashboard (HTML)
    /generate         POST  transcript -> structured meeting notes + follow-up email
    /generate-email   POST  transcript/notes -> new follow-up email
    /download-pdf     POST  notes -> PDF file
"""

import io
import json
import os
import re

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)
from xml.sax.saxutils import escape

load_dotenv()

app = Flask(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_TRANSCRIPT_CHARS = 60000
NOT_SPECIFIED = "Not specified"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class AIError(Exception):
    """Error with a message that is safe to show to the user."""

    def __init__(self, message, status=500):
        super().__init__(message)
        self.message = message
        self.status = status


@app.errorhandler(AIError)
def handle_ai_error(err):
    return jsonify({"success": False, "error": err.message}), err.status


@app.errorhandler(404)
def handle_404(_err):
    return jsonify({"success": False, "error": "Page not found."}), 404


@app.errorhandler(500)
def handle_500(_err):
    return jsonify({"success": False, "error": "Something went wrong on the server."}), 500


# --------------------------------------------------------------------------
# OpenRouter helper
# --------------------------------------------------------------------------
def call_openrouter(system_prompt, user_prompt, json_mode=True, temperature=0.2):
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip() or "openai/gpt-4o-mini"

    if not api_key or api_key == "your_api_key_here":
        raise AIError(
            "OpenRouter API key is missing. Add OPENROUTER_API_KEY to your .env file and restart the app.",
            500,
        )

    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:5000",
        "X-Title": "AI Meeting Notes Generator",
    }

    try:
        resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=90)
    except requests.exceptions.Timeout:
        raise AIError("The AI service took too long to respond. Please try again.", 504)
    except requests.exceptions.ConnectionError:
        raise AIError("Could not reach the AI service. Check your internet connection and try again.", 503)
    except requests.exceptions.RequestException:
        raise AIError("Network error while contacting the AI service.", 502)

    if resp.status_code in (401, 403):
        raise AIError("Authentication failed. Check that your OpenRouter API key is correct.", 401)
    if resp.status_code == 402:
        raise AIError("Your OpenRouter account has insufficient credits.", 402)
    if resp.status_code == 429:
        raise AIError("Rate limit reached. Wait a moment and try again.", 429)
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except ValueError:
            pass
        raise AIError(f"The AI service returned an error ({resp.status_code}). {detail}".strip(), 502)

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise AIError("The AI service returned an unexpected response. Please try again.", 502)

    if not content or not content.strip():
        raise AIError("The AI returned an empty response. Please try again.", 502)
    return content


def parse_json(text):
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    raise AIError("The AI response could not be read. Please try again.", 502)


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------
NOTES_SYSTEM = """You are an expert meeting secretary. You turn raw meeting transcripts into accurate, professional meeting notes written in simple, clear English.

STRICT RULES
- Use ONLY information found in the transcript. Never invent participants, decisions, tasks, owners or deadlines.
- A decision is something the group clearly agreed or decided. Do not treat suggestions or open questions as decisions. If there are none, return an empty list.
- An action item is a task someone must do. Set "responsible_person" only if a person is explicitly named for it, and "deadline" only if one is explicitly stated. Otherwise use "Not specified".
- "status" is always "Pending" unless the transcript says the task is already done (then "Completed").
- Keep each list item short and specific.

Return ONLY a JSON object with exactly this shape:
{
  "title": "short meeting title based on the content",
  "summary": "2-4 sentence professional summary",
  "discussion_points": ["main topic or key point", "..."],
  "decisions": ["decision", "..."],
  "action_items": [
    {"task": "...", "responsible_person": "name or Not specified", "deadline": "date/time or Not specified", "status": "Pending"}
  ],
  "responsible_persons": ["each distinct person explicitly given a task"],
  "deadlines": ["each distinct deadline explicitly stated"],
  "follow_up_email": {"subject": "...", "body": "..."}
}

The follow-up email must be professional, start with a greeting, include the meeting highlights, the decisions and the pending tasks (with owners and deadlines when known), end with a polite closing, and use a placeholder like [Your Name] as the signature. Use plain text with line breaks (\\n) in the body."""

EMAIL_SYSTEM = """You write professional follow-up emails after meetings.

STRICT RULES
- Use ONLY the information provided. Never invent participants, decisions, tasks or deadlines.
- Include a suitable subject line, a greeting, meeting highlights, key decisions, pending tasks (with owner and deadline when known), a polite closing and a signature placeholder like [Your Name].
- If a section has nothing to report, leave it out instead of making something up.
- Use plain text with line breaks (\\n). Simple, professional English.

Return ONLY a JSON object: {"subject": "...", "body": "..."}"""


def build_notes_prompt(transcript):
    return f"Meeting transcript:\n\n{transcript}"


def build_email_prompt(transcript, notes, tone):
    parts = [f"Tone: {tone}."]
    if notes:
        parts.append("Meeting notes (JSON):\n" + json.dumps(notes, ensure_ascii=False))
    if transcript:
        parts.append("Original transcript:\n" + transcript)
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# Normalising AI output
# --------------------------------------------------------------------------
def clean_str(value, default=""):
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def clean_list(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = clean_str(item)
        if text:
            out.append(text)
    return out


def normalize_unspecified(value):
    text = clean_str(value)
    if not text or text.lower() in {"none", "n/a", "na", "unknown", "not mentioned",
                                    "not specified", "unspecified", "null", "-"}:
        return NOT_SPECIFIED
    return text


def normalize_email(raw):
    if not isinstance(raw, dict):
        raw = {}
    return {
        "subject": clean_str(raw.get("subject"), "Meeting follow-up"),
        "body": clean_str(raw.get("body")),
    }


def normalize_notes(raw):
    if not isinstance(raw, dict):
        raise AIError("The AI response was not in the expected format. Please try again.", 502)

    items = []
    for item in raw.get("action_items") or []:
        if isinstance(item, str):
            item = {"task": item}
        if not isinstance(item, dict):
            continue
        task = clean_str(item.get("task"))
        if not task:
            continue
        status = clean_str(item.get("status"), "Pending")
        items.append({
            "task": task,
            "responsible_person": normalize_unspecified(item.get("responsible_person")),
            "deadline": normalize_unspecified(item.get("deadline")),
            "status": "Completed" if status.lower().startswith("complet") else "Pending",
        })

    persons = [p for p in clean_list(raw.get("responsible_persons")) if p.lower() != NOT_SPECIFIED.lower()]
    deadlines = [d for d in clean_list(raw.get("deadlines")) if d.lower() != NOT_SPECIFIED.lower()]
    # Keep the derived lists consistent with the action items table.
    for it in items:
        if it["responsible_person"] != NOT_SPECIFIED and it["responsible_person"] not in persons:
            persons.append(it["responsible_person"])
        if it["deadline"] != NOT_SPECIFIED and it["deadline"] not in deadlines:
            deadlines.append(it["deadline"])

    return {
        "title": clean_str(raw.get("title"), "Meeting Notes"),
        "summary": clean_str(raw.get("summary"), "No summary could be generated."),
        "discussion_points": clean_list(raw.get("discussion_points")),
        "decisions": clean_list(raw.get("decisions")),
        "action_items": items,
        "responsible_persons": persons,
        "deadlines": deadlines,
        "follow_up_email": normalize_email(raw.get("follow_up_email")),
    }


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    body = request.get_json(silent=True) or {}
    transcript = clean_str(body.get("transcript"))

    if not transcript:
        raise AIError("Please paste a meeting transcript before generating notes.", 400)
    if len(transcript) < 30:
        raise AIError("The transcript is too short to produce useful notes. Add more of the conversation.", 400)
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        raise AIError(
            f"The transcript is too long ({len(transcript):,} characters). The limit is {MAX_TRANSCRIPT_CHARS:,}.",
            413,
        )

    content = call_openrouter(NOTES_SYSTEM, build_notes_prompt(transcript))
    notes = normalize_notes(parse_json(content))
    return jsonify({"success": True, "notes": notes})


@app.route("/generate-email", methods=["POST"])
def generate_email():
    body = request.get_json(silent=True) or {}
    transcript = clean_str(body.get("transcript"))
    notes = body.get("notes") if isinstance(body.get("notes"), dict) else None
    tone = clean_str(body.get("tone"), "Professional")
    if tone not in {"Professional", "Friendly", "Formal", "Concise"}:
        tone = "Professional"

    if not transcript and not notes:
        raise AIError("Generate meeting notes first, then create the follow-up email.", 400)
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        raise AIError("The transcript is too long to process.", 413)

    # The email is written from the structured notes when available; they are shorter
    # and keep the email consistent with what is shown on screen.
    slim_notes = None
    if notes:
        slim_notes = {k: notes.get(k) for k in
                      ("title", "summary", "discussion_points", "decisions", "action_items")}
        transcript = ""

    content = call_openrouter(EMAIL_SYSTEM, build_email_prompt(transcript, slim_notes, tone), temperature=0.4)
    email = normalize_email(parse_json(content))
    if not email["body"]:
        raise AIError("The AI returned an empty email. Please try again.", 502)
    return jsonify({"success": True, "email": email})


@app.route("/download-pdf", methods=["POST"])
def download_pdf():
    body = request.get_json(silent=True) or {}
    notes = body.get("notes")
    if not isinstance(notes, dict) or not notes.get("summary"):
        raise AIError("There are no meeting notes to export yet.", 400)

    pdf = build_pdf(normalize_notes(notes))
    return send_file(pdf, mimetype="application/pdf", as_attachment=True,
                     download_name="meeting-notes.pdf")


# --------------------------------------------------------------------------
# PDF export
# --------------------------------------------------------------------------
def build_pdf(notes):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm, title=notes["title"])
    base = getSampleStyleSheet()
    blue = colors.HexColor("#1d4ed8")
    title = ParagraphStyle("T", parent=base["Title"], textColor=blue, alignment=0, fontSize=20, spaceAfter=4)
    h = ParagraphStyle("H", parent=base["Heading2"], textColor=blue, fontSize=13, spaceBefore=14, spaceAfter=6)
    p = ParagraphStyle("P", parent=base["BodyText"], fontSize=10.5, leading=15)
    cell = ParagraphStyle("C", parent=p, fontSize=9.5, leading=12)
    cell_head = ParagraphStyle("CH", parent=cell, textColor=colors.white, fontName="Helvetica-Bold")

    def bullets(items):
        return [Paragraph("&bull;&nbsp; " + escape(i), p) for i in items] or [Paragraph("None recorded.", p)]

    story = [Paragraph(escape(notes["title"]), title), Paragraph("Meeting Notes", p)]

    story += [Paragraph("Meeting Summary", h), Paragraph(escape(notes["summary"]), p)]
    story += [Paragraph("Main Discussion Points", h)] + bullets(notes["discussion_points"])
    story += [Paragraph("Key Decisions", h)] + bullets(notes["decisions"])

    story.append(Paragraph("Action Items", h))
    if notes["action_items"]:
        rows = [[Paragraph(x, cell_head) for x in ("Task", "Responsible Person", "Deadline", "Status")]]
        for it in notes["action_items"]:
            rows.append([Paragraph(escape(it[k]), cell) for k in
                         ("task", "responsible_person", "deadline", "status")])
        table = Table(rows, colWidths=[68 * mm, 38 * mm, 36 * mm, 28 * mm], repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), blue),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(table)
    else:
        story.append(Paragraph("No action items were identified.", p))

    story += [Paragraph("Responsible Persons", h)] + bullets(notes["responsible_persons"])
    story += [Paragraph("Deadlines", h)] + bullets(notes["deadlines"])

    email = notes["follow_up_email"]
    story.append(Paragraph("Follow-up Email", h))
    story.append(Paragraph("<b>Subject:</b> " + escape(email["subject"]), p))
    story.append(Spacer(1, 4))
    for line in email["body"].split("\n"):
        story.append(Paragraph(escape(line) if line.strip() else "&nbsp;", p))

    doc.build(story)
    buf.seek(0)
    return buf


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1", host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
