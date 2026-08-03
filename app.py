from __future__ import annotations

import base64
import json
import re
import sqlite3
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "safety.db"
TEXT_MODEL = "openai/gpt-oss-20b"
VISION_MODEL = "qwen/qwen3.6-27b"
FORCE_DEMO_MODE = False
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
AUDIO_MODEL = "whisper-large-v3-turbo"

app = FastAPI(title="OmniGuard Safety Platform", version="0.24.0")

@app.middleware("http")
async def disable_browser_cache(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                contact TEXT,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                status TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_type TEXT NOT NULL,
                details TEXT,
                actions_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


init_db()


class CheckinRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    contact: str = Field(default="", max_length=160)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    status: Literal["safe", "arrived", "need_help"] = "arrived"
    note: str = Field(default="", max_length=500)


class TextAnalysisRequest(BaseModel):
    text: str = Field(min_length=2, max_length=20_000)
    channel: str = Field(default="unknown", max_length=40)


class LinkRequest(BaseModel):
    url: str = Field(min_length=3, max_length=2_000)


class DocumentTextRequest(BaseModel):
    text: str = Field(min_length=2, max_length=60_000)
    filename: str = Field(default="document", max_length=180)


class IncidentRequest(BaseModel):
    incident_type: str = Field(min_length=2, max_length=80)
    details: str = Field(default="", max_length=4_000)


class MemoryProfileRequest(BaseModel):
    transcript: str = Field(min_length=1, max_length=8_000)
    speaker_label: str = Field(default="Visible visitor", max_length=120)
    sync_score: int | None = Field(default=None, ge=0, le=100)
    existing_name: str = Field(default="", max_length=80)
    existing_relation: str = Field(default="", max_length=80)
    existing_where_met: str = Field(default="", max_length=120)
    existing_note: str = Field(default="", max_length=240)
    existing_facts: list[str] = Field(default_factory=list, max_length=12)




NAME_STOPWORDS = {
    "a", "an", "and", "are", "asking", "buying", "calling", "can", "could", "doing",
    "from", "going", "here", "looking", "need", "please", "selling", "speaking", "the",
    "there", "they", "this", "to", "today", "trying", "using", "want", "we", "would",
    "your", "you", "gold", "money", "payment", "gift", "card", "crypto", "bitcoin",
}

NAME_BOUNDARY_WORDS = {
    "and", "but", "because", "from", "here", "speaking", "today", "who", "that", "which",
    "your", "you", "i", "we", "can", "could", "would", "want", "need", "please", "am", "is",
}

def _title_name(value: str) -> str:
    return " ".join(part[:1].upper() + part[1:].lower() for part in value.split())

def _clean_explicit_name(candidate: str) -> str:
    words = re.findall(r"[A-Za-z][A-Za-z'\-]{1,30}", candidate)
    kept: list[str] = []
    for word in words[:4]:
        lower = word.lower()
        if lower in NAME_BOUNDARY_WORDS:
            break
        kept.append(word)
        if len(kept) >= 3:
            break
    if not kept:
        return ""
    if kept[0].lower() in NAME_STOPWORDS:
        return ""
    if any(word.lower() in NAME_STOPWORDS for word in kept):
        return ""
    result = _title_name(" ".join(kept))
    if len(result) < 2 or len(result) > 60:
        return ""
    return result

def explicit_name_from_transcript(text: str) -> str:
    normalized = re.sub(r"[’]", "'", text or "")
    patterns = [
        r"\bmy name(?:'s| is)\s+([^,.;!?]{2,70})",
        r"\bi(?:'m| am)\s+([^,.;!?]{2,70})",
        r"\bthis is\s+([^,.;!?]{2,70})",
        r"\b(?:you can )?call me\s+([^,.;!?]{2,70})",
        r"\bi introduced myself as\s+([^,.;!?]{2,70})",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            candidate = _clean_explicit_name(match.group(1))
            if candidate:
                return candidate
    return ""

def suspicious_identity_name(name: str) -> bool:
    words = re.findall(r"[A-Za-z][A-Za-z'\-]*", name or "")
    if not words or len(words) > 3:
        return True
    lowered = [word.lower() for word in words]
    return lowered[0] in NAME_STOPWORDS or any(word in NAME_STOPWORDS for word in lowered)

def enforce_identity_guardrail(result: dict[str, Any], payload: MemoryProfileRequest) -> dict[str, Any]:
    verified_name = explicit_name_from_transcript(payload.transcript)
    existing_name = (payload.existing_name or "").strip()
    existing_is_placeholder = (not existing_name or existing_name.lower().startswith("familiar visitor")
                               or suspicious_identity_name(existing_name))

    if verified_name:
        if existing_name and not existing_is_placeholder and verified_name.casefold() != existing_name.casefold():
            # A single later utterance must not rename an already verified face.
            result["explicit_identity"] = False
            result["name"] = existing_name
            result["reason"] = "A different spoken name was detected, so the existing verified identity was preserved for caregiver review."
        else:
            result["explicit_identity"] = True
            result["name"] = verified_name
    else:
        # Model output alone is never enough to rename a person.
        result["explicit_identity"] = False
        result["name"] = existing_name
        if result.get("reason"):
            result["reason"] = str(result["reason"]) + " No valid self-introduction phrase was detected."
        else:
            result["reason"] = "No valid self-introduction phrase was detected."

    if not result.get("name"):
        result["name"] = ""
    return result


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def strict_schema(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": schema},
    }


def groq_chat(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    api_key = api_key.strip()
    if not api_key:
        raise RuntimeError("A Groq API key was not supplied")
    with httpx.Client(timeout=45.0) as http:
        response = http.post(
            GROQ_CHAT_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    response.raise_for_status()
    return response.json()


def groq_json(api_key: str, system: str, user: str, name: str, schema: dict[str, Any]) -> dict[str, Any]:
    response = groq_chat({
        "model": TEXT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "max_completion_tokens": 1_200,
        "response_format": strict_schema(name, schema),
    }, api_key)
    return json.loads(response["choices"][0]["message"].get("content") or "{}")


def demo_scam(text: str) -> dict[str, Any]:
    lower = text.lower()
    flags: list[dict[str, str]] = []
    patterns = [
        ("Urgency", ["immediately", "right now", "urgent", "act now", "today only", "last chance", "within 10 minutes", "account will be closed", "final notice"]),
        ("Credential request", ["verification code", "password", "one-time code", "otp", "login code"]),
        ("Unusual payment", ["gift card", "crypto", "bitcoin", "wire transfer", "zelle", "venmo", "cash app"]),
        ("Threat or fear", ["arrest", "suspended", "lawsuit", "police", "penalty"]),
        ("Impersonation", ["bank", "irs", "social security", "medicare", "police", "government", "tech support", "amazon", "microsoft", "your son", "your daughter", "your grandson", "your granddaughter"]),
        ("Secrecy", ["do not tell anyone", "don't tell anyone", "do not tell your caregiver", "don't tell your caregiver", "keep this secret", "between us"]),
        ("Remote access", ["anydesk", "teamviewer", "remote access", "screen sharing"]),
    ]
    for label, needles in patterns:
        match = next((n for n in needles if n in lower), None)
        if match:
            flags.append({"type": label, "evidence": match, "explanation": f"The message uses a common {label.lower()} tactic."})
    money_match = re.search(
        r"(?:\b(?:pay|send|wire|transfer|buy|purchase|lend|loan|donate|invest|deposit)\b.{0,60}(?:\$\s?\d[\d,]*|\b\d[\d,]*\s*(?:dollars?|usd)\b)|(?:\$\s?\d[\d,]*|\b\d[\d,]*\s*(?:dollars?|usd)\b).{0,60}\b(?:pay|send|wire|transfer|buy|purchase|lend|loan|donate|invest|deposit)\b)",
        text,
        flags=re.IGNORECASE,
    )
    direct_financial_request = re.search(
        r"\b(?:can you|could you|please|need you to|want you to|help me)\b.{0,50}\b(?:pay|send|wire|transfer|buy|purchase|lend|loan|donate|invest|deposit|cover|fund)\b",
        text,
        flags=re.IGNORECASE,
    )
    if money_match:
        flags.append({
            "type": "Financial request",
            "evidence": money_match.group(0)[:140],
            "explanation": "The speaker appears to be asking for money, a purchase, or a financial transfer that should be independently verified.",
        })
    if direct_financial_request and not money_match:
        flags.append({
            "type": "Financial request",
            "evidence": direct_financial_request.group(0)[:140],
            "explanation": "The speaker is asking the listener to take a financial action. For a vulnerable user, this should be independently verified before continuing.",
        })
    score = min(98, 18 + len(flags) * 18)
    if money_match or direct_financial_request:
        financial_text = money_match.group(0) if money_match else direct_financial_request.group(0)
        amount_match = re.search(r"\$\s?([\d,]+)", financial_text)
        amount = int(amount_match.group(1).replace(",", "")) if amount_match else 0
        score = max(score, 72 if amount >= 500 else 48)
    flag_types = {item["type"] for item in flags}
    severe = bool(flag_types & {"Credential request", "Unusual payment", "Remote access", "Financial request"})
    if severe:
        score = max(score, 58)
    if len(flags) >= 2:
        score = max(score, 68)
    if severe and bool(flag_types & {"Urgency", "Secrecy", "Threat or fear"}):
        score = max(score, 88)
    return {
        "risk_level": "critical" if score >= 80 else "high" if score >= 55 else "medium" if score >= 30 else "low",
        "risk_score": score,
        "summary": "This message contains several scam indicators." if flags else "No strong scam pattern was found, but identity should still be independently verified.",
        "signals": flags,
        "safe_next_steps": [
            "Do not share passwords, one-time codes, or payment information.",
            "End the conversation and contact the organization through an independently verified number or website.",
            "Preserve screenshots, caller information, and payment instructions as evidence.",
        ],
        "verification_question": "Can I independently call the organization using the number printed on my card or its official website?",
    }


def demo_mental(text: str) -> dict[str, Any]:
    lower = text.lower()
    urgent_terms = ["kill yourself", "i will hurt you", "going to hurt you", "suicide", "i want to die"]
    urgent = any(term in lower for term in urgent_terms)
    patterns: list[str] = []
    for label, terms in [
        ("Repeated insults", ["stupid", "loser", "worthless", "ugly"]),
        ("Threats", ["hurt you", "ruin your life", "you'll regret"]),
        ("Exclusion or humiliation", ["nobody likes you", "everyone hates you", "post this everywhere"]),
        ("Coercion", ["don't tell anyone", "if you cared", "send me"]),
    ]:
        if any(term in lower for term in terms):
            patterns.append(label)
    return {
        "urgency": "immediate_human_help" if urgent else "support_recommended",
        "summary": "The content may include bullying, coercion, or threats." if patterns else "The message may be upsetting; the app can still help the user pause and respond safely.",
        "patterns": patterns,
        "grounding_steps": [
            "Put the phone down for 30 seconds and take five slow breaths.",
            "Move away from the conversation and avoid replying while overwhelmed.",
            "Name three things you can see, two you can hear, and one you can feel.",
        ],
        "safety_steps": [
            "Save screenshots and timestamps before blocking or reporting.",
            "Tell a trusted adult, counselor, teacher, parent, or teammate.",
            "Block or mute the sender when it is safe to do so.",
        ],
        "trusted_contact_message": "I received messages that made me feel unsafe or overwhelmed. Could you look at them with me and help me decide what to do next?",
        "disclaimer": "This tool identifies communication risks; it does not diagnose a person or replace professional support.",
    }


SCAM_SCHEMA = {
    "type": "object",
    "properties": {
        "risk_level": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "risk_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "summary": {"type": "string"},
        "signals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "evidence": {"type": "string"},
                    "explanation": {"type": "string"},
                },
                "required": ["type", "evidence", "explanation"],
                "additionalProperties": False,
            },
        },
        "safe_next_steps": {"type": "array", "items": {"type": "string"}},
        "verification_question": {"type": "string"},
    },
    "required": ["risk_level", "risk_score", "summary", "signals", "safe_next_steps", "verification_question"],
    "additionalProperties": False,
}

DOCUMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "risk_level": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "risk_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "document_type": {"type": "string"},
        "summary": {"type": "string"},
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                    "category": {"type": "string"},
                    "excerpt": {"type": "string"},
                    "explanation": {"type": "string"},
                },
                "required": ["severity", "category", "excerpt", "explanation"],
                "additionalProperties": False,
            },
        },
        "verification_checks": {"type": "array", "items": {"type": "string"}},
        "safe_next_steps": {"type": "array", "items": {"type": "string"}},
        "extracted_text": {"type": "string"},
        "disclaimer": {"type": "string"},
    },
    "required": ["risk_level", "risk_score", "document_type", "summary", "highlights", "verification_checks", "safe_next_steps", "extracted_text", "disclaimer"],
    "additionalProperties": False,
}


def demo_document(text: str, filename: str = "document") -> dict[str, Any]:
    lower = text.lower()
    patterns = [
        ("Urgent deadline", "high", ["immediately", "within 24 hours", "final notice", "act now", "today only"]),
        ("Unusual payment method", "critical", ["gift card", "cryptocurrency", "bitcoin", "wire transfer", "western union"]),
        ("Changed payment details", "critical", ["new bank account", "updated bank details", "change of banking", "different account number"]),
        ("Credential or identity request", "high", ["verification code", "password", "social security number", "one-time code", "login credentials"]),
        ("Secrecy or bypass", "high", ["do not contact", "keep this confidential", "do not tell", "outside the normal process"]),
        ("Threat or penalty", "high", ["arrest", "legal action", "account suspended", "penalty", "collection action"]),
        ("Advance fee", "high", ["processing fee", "release fee", "tax before receiving", "upfront fee"]),
    ]
    highlights: list[dict[str, str]] = []
    severity_weight = {"low": 6, "medium": 12, "high": 20, "critical": 30}
    score = 8
    for category, severity, needles in patterns:
        match = next((needle for needle in needles if needle in lower), None)
        if not match:
            continue
        start = max(0, lower.find(match) - 55)
        end = min(len(text), lower.find(match) + len(match) + 95)
        excerpt = " ".join(text[start:end].split())
        highlights.append({
            "severity": severity,
            "category": category,
            "excerpt": excerpt or match,
            "explanation": f"The document contains language associated with {category.lower()}. Verify it through an independent channel.",
        })
        score += severity_weight[severity]
    score = min(98, score)
    return {
        "risk_level": "critical" if score >= 80 else "high" if score >= 55 else "medium" if score >= 28 else "low",
        "risk_score": score,
        "document_type": Path(filename).suffix.lstrip(".").upper() or "Document",
        "summary": "Potential fraud indicators were found in the document." if highlights else "No strong scam phrase was found, but the sender, payment details, and claims still require independent verification.",
        "highlights": highlights,
        "verification_checks": [
            "Confirm the sender using a known phone number or official website, not contact details inside the document.",
            "Compare payment instructions with a previously verified invoice or contract.",
            "Check names, domains, dates, totals, and account numbers for small changes.",
        ],
        "safe_next_steps": [
            "Do not pay, sign, or disclose credentials until the document is independently verified.",
            "Preserve the original file, email headers, and delivery context.",
            "Ask a trusted person or the real organization to review the suspicious sections.",
        ],
        "extracted_text": text[:18_000],
        "disclaimer": "This scan identifies warning signs; it does not prove that a document is genuine or fraudulent.",
    }


def analyze_document_text_with_key(text: str, filename: str, api_key: str) -> dict[str, Any]:
    if FORCE_DEMO_MODE or not api_key.strip():
        result = demo_document(text, filename)
        result["mode"] = "demo"
        return result
    try:
        result = groq_json(
            api_key,
            """You are a document fraud and scam safety analyst. Inspect the supplied document text for payment diversion, impersonation, fake invoices, advance fees, credential requests, threats, urgency, secrecy, inconsistent identities, suspicious contact instructions, and clauses designed to bypass normal verification. Do not declare a document definitively fraudulent or legally valid. Quote short exact excerpts for every highlight. Keep extracted_text to at most 18000 characters. Return only the requested JSON.""",
            f"Filename: {filename}\n\nDocument text:\n{text[:45_000]}",
            "document_scam_analysis",
            DOCUMENT_SCHEMA,
        )
        result["mode"] = "groq"
        return result
    except Exception as exc:
        fallback = demo_document(text, filename)
        fallback.update({"mode": "fallback", "warning": f"Groq request failed: {type(exc).__name__}"})
        return fallback


MEMORY_PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "explicit_identity": {"type": "boolean"},
        "should_remember": {"type": "boolean"},
        "name": {"type": "string"},
        "relationship": {"type": "string"},
        "where_met": {"type": "string"},
        "memory_note": {"type": "string"},
        "new_facts": {"type": "array", "items": {"type": "string"}},
        "short_overview": {"type": "string"},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason": {"type": "string"},
    },
    "required": ["explicit_identity", "should_remember", "name", "relationship", "where_met", "memory_note", "new_facts", "short_overview", "confidence", "reason"],
    "additionalProperties": False,
}


def demo_memory_profile(payload: MemoryProfileRequest) -> dict[str, Any]:
    text = " ".join(payload.transcript.split())
    lower = text.lower()
    verified_name = explicit_name_from_transcript(text)
    name = verified_name or payload.existing_name
    relation = payload.existing_relation
    where_met = payload.existing_where_met
    note = payload.existing_note
    explicit = bool(verified_name)

    relation_terms = [
        "daughter", "son", "granddaughter", "grandson", "wife", "husband", "sister", "brother",
        "friend", "neighbor", "neighbour", "caregiver", "nurse", "doctor", "niece", "nephew", "cousin",
    ]
    for term in relation_terms:
        if re.search(rf"\b(?:your|i am your|i'm your)\s+{re.escape(term)}\b", lower):
            relation = term
            break

    facts: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        clean = sentence.strip()
        if len(clean) >= 12 and any(token in clean.lower() for token in ["visit", "live", "work", "remember", "usually", "every", "brought", "help", "meet"]):
            facts.append(clean[:180])
    facts = facts[:4]
    if not note and facts:
        note = facts[0]
    should = bool(text and (explicit or payload.existing_name or len(text.split()) >= 3))
    overview_parts = []
    if name:
        overview_parts.append(name + (f" is your {relation}" if relation else " is someone familiar"))
    else:
        overview_parts.append("This is a familiar visitor whose name has not been stated clearly yet")
    if note:
        overview_parts.append(note.rstrip(". "))
    overview = ". ".join(overview_parts)[:240].rstrip(". ") + "."
    result = {
        "explicit_identity": explicit,
        "should_remember": should,
        "name": name,
        "relationship": relation,
        "where_met": where_met,
        "memory_note": note,
        "new_facts": facts,
        "short_overview": overview,
        "confidence": 82 if explicit else 52 if should else 20,
        "reason": "Identity was verified from a clear self-introduction phrase." if explicit else "Speech was captured, but no valid self-introduction phrase was found.",
        "mode": "demo",
    }
    return enforce_identity_guardrail(result, payload)


MENTAL_SCHEMA = {
    "type": "object",
    "properties": {
        "urgency": {"type": "string", "enum": ["routine", "support_recommended", "immediate_human_help"]},
        "summary": {"type": "string"},
        "patterns": {"type": "array", "items": {"type": "string"}},
        "grounding_steps": {"type": "array", "items": {"type": "string"}},
        "safety_steps": {"type": "array", "items": {"type": "string"}},
        "trusted_contact_message": {"type": "string"},
        "disclaimer": {"type": "string"},
    },
    "required": ["urgency", "summary", "patterns", "grounding_steps", "safety_steps", "trusted_contact_message", "disclaimer"],
    "additionalProperties": False,
}


@app.get("/")
def home() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "force_demo_mode": FORCE_DEMO_MODE,
        "browser_key_supported": True,
        "text_model": TEXT_MODEL,
        "vision_model": VISION_MODEL,
        "audio_model": AUDIO_MODEL,
    }


@app.post("/api/groq/test")
def test_groq_key(
    x_groq_api_key: str = Header(default="", alias="X-Groq-API-Key"),
) -> dict[str, Any]:
    if not x_groq_api_key.strip():
        raise HTTPException(status_code=400, detail="Enter a Groq API key on the website first.")
    try:
        with httpx.Client(timeout=20.0) as http:
            response = http.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {x_groq_api_key.strip()}"},
            )
        if response.status_code in {401, 403}:
            raise HTTPException(status_code=401, detail="Groq rejected this API key.")
        response.raise_for_status()
        return {"ok": True, "message": "Groq key connected for this browser tab."}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Groq: {type(exc).__name__}") from exc


@app.post("/api/checkin")
def create_checkin(payload: CheckinRequest) -> dict[str, Any]:
    timestamp = utc_now()
    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO checkins(name, contact, latitude, longitude, status, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (payload.name, payload.contact, payload.latitude, payload.longitude, payload.status, payload.note, timestamp),
        )
        checkin_id = cursor.lastrowid
    return {
        "id": checkin_id,
        "created_at": timestamp,
        "map_url": f"https://www.openstreetmap.org/?mlat={payload.latitude}&mlon={payload.longitude}#map=16/{payload.latitude}/{payload.longitude}",
        "notification": f"{payload.name} checked in as {payload.status.replace('_', ' ')}.",
        "contact_delivery": "demo_log" if payload.contact else "not_configured",
    }


@app.get("/api/checkins")
def list_checkins() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM checkins ORDER BY id DESC LIMIT 25").fetchall()
    return [dict(row) for row in rows]


@app.post("/api/analyze/scam-audio")
async def analyze_scam_audio(
    audio: UploadFile = File(...),
    context: str = Form(default=""),
    x_groq_api_key: str = Header(default="", alias="X-Groq-API-Key"),
) -> dict[str, Any]:
    if not x_groq_api_key.strip():
        raise HTTPException(status_code=400, detail="Enter and test a Groq API key before starting live call listening.")
    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="The microphone segment was empty.")
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio segment is too large.")
    mime = (audio.content_type or "audio/webm").split(";")[0]
    filename = audio.filename or ("call-segment.webm" if "webm" in mime else "call-segment.wav")
    try:
        async with httpx.AsyncClient(timeout=60.0) as http:
            response = await http.post(
                GROQ_TRANSCRIBE_URL,
                headers={"Authorization": f"Bearer {x_groq_api_key.strip()}"},
                data={"model": AUDIO_MODEL, "response_format": "json"},
                files={"file": (filename, data, mime)},
            )
        if response.status_code in {401, 403}:
            raise HTTPException(status_code=401, detail="Groq rejected the API key while transcribing audio.")
        response.raise_for_status()
        transcript = (response.json().get("text") or "").strip()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Audio transcription failed: {type(exc).__name__}") from exc

    combined = (context.strip() + "\n" + transcript).strip()[-12_000:]
    if not combined:
        return {
            "transcript": "",
            "analysis": demo_scam(""),
            "mode": "groq_audio",
            "message": "No clear speech was detected in this segment.",
        }
    try:
        analysis = groq_json(
            x_groq_api_key,
            """You are a real-time scam-call safety agent, including for older adults and people with dementia who may be vulnerable to financial exploitation. Analyze only the rolling transcript. Use a conservative safety threshold for an older adult or person with dementia: any request to pay, buy, transfer, lend, donate, invest, disclose personal or financial information, reveal a code, click a link, scan a QR code, install software, or keep a secret should be at least medium risk until independently verified. Identify urgency, threats, impersonation, credentials, one-time codes, unusual purchases, loans, donations, investment pitches, payment diversion, secrecy, remote access, gift cards, cryptocurrency, prizes, refunds, family-emergency stories, and instructions not to verify independently. A request to buy an ordinary item for an unusually large amount should be treated as a financial-exploitation warning even without explicit threats. Never claim certainty about caller identity. Keep evidence excerpts brief and exact. The user may still be in the conversation, so prioritize immediate safe actions such as pausing, withholding money or information, asking a trusted caregiver, ending the call, and independently verifying the request. Return only the requested JSON.""",
            f"Rolling call transcript:\n{combined}",
            "live_scam_call",
            SCAM_SCHEMA,
        )
    except Exception as exc:
        analysis = demo_scam(combined)
        analysis["warning"] = f"Groq risk analysis failed: {type(exc).__name__}"
    return {"transcript": transcript, "analysis": analysis, "mode": "groq_audio"}


@app.post("/api/memory/profile-from-speech")
def memory_profile_from_speech(
    payload: MemoryProfileRequest,
    x_groq_api_key: str = Header(default="", alias="X-Groq-API-Key"),
) -> dict[str, Any]:
    if payload.sync_score is None or payload.sync_score < 28:
        return {
            "explicit_identity": False,
            "should_remember": False,
            "name": payload.existing_name,
            "relationship": payload.existing_relation,
            "where_met": payload.existing_where_met,
            "memory_note": payload.existing_note,
            "new_facts": [],
            "short_overview": "Speech-to-face timing was too uncertain to update this person's memory profile.",
            "confidence": 10,
            "reason": "The mouth-motion and microphone timing did not align strongly enough.",
            "mode": "local_guardrail",
        }
    if FORCE_DEMO_MODE or not x_groq_api_key.strip():
        return demo_memory_profile(payload)
    existing = {
        "name": payload.existing_name,
        "relationship": payload.existing_relation,
        "where_met": payload.existing_where_met,
        "memory_note": payload.existing_note,
        "facts": payload.existing_facts[:12],
    }
    try:
        result = groq_json(
            x_groq_api_key,
            """You extract a cautious memory profile from speech for a dementia-support application. Use only facts explicitly spoken in the transcript or already present in the existing profile. Never infer identity, age, gender, ethnicity, health status, relationship, or trustworthiness from appearance. Do not invent facts. A name or relationship counts as explicit only when the speaker clearly states it, for example 'I'm Meera' or 'I'm your granddaughter'. Preserve existing verified fields unless the transcript explicitly updates them. Keep the overview gentle, neutral, and under 35 words. If identity is incomplete, say that the visitor is familiar but unnamed. Return only the requested JSON.""",
            f"Visible-speaker label: {payload.speaker_label}\nLip/audio sync score: {payload.sync_score}\nExisting profile: {json.dumps(existing, ensure_ascii=False)}\nTranscript: {payload.transcript}",
            "dementia_memory_profile",
            MEMORY_PROFILE_SCHEMA,
        )
        result = enforce_identity_guardrail(result, payload)
        result["mode"] = "groq"
        return result
    except Exception as exc:
        fallback = demo_memory_profile(payload)
        fallback.update({"mode": "fallback", "warning": f"Groq profile extraction failed: {type(exc).__name__}"})
        return fallback


@app.post("/api/analyze/scam-document-text")
def analyze_scam_document_text(
    payload: DocumentTextRequest,
    x_groq_api_key: str = Header(default="", alias="X-Groq-API-Key"),
) -> dict[str, Any]:
    return analyze_document_text_with_key(payload.text, payload.filename, x_groq_api_key)


@app.post("/api/analyze/scam-document")
async def analyze_scam_document(
    document: UploadFile = File(...),
    x_groq_api_key: str = Header(default="", alias="X-Groq-API-Key"),
) -> dict[str, Any]:
    filename = document.filename or "document"
    suffix = Path(filename).suffix.lower()
    data = await document.read()
    if not data:
        raise HTTPException(status_code=400, detail="The document is empty.")
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Use a document under 15 MB.")

    if suffix == ".pdf" or document.content_type == "application/pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(data))
            pages = []
            for page in reader.pages[:25]:
                pages.append(page.extract_text() or "")
            text = "\n\n".join(pages).strip()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Could not read this PDF: {type(exc).__name__}") from exc
        if len(text) < 20:
            raise HTTPException(status_code=400, detail="This PDF appears to be image-only. Upload a screenshot or photo so the local OCR scanner can read it.")
        return analyze_document_text_with_key(text, filename, x_groq_api_key)

    if suffix in {".txt", ".md", ".csv", ".log"} or (document.content_type or "").startswith("text/"):
        text = data.decode("utf-8", errors="replace").strip()
        if len(text) < 2:
            raise HTTPException(status_code=400, detail="No readable text was found.")
        return analyze_document_text_with_key(text, filename, x_groq_api_key)

    raise HTTPException(status_code=415, detail="Upload a PDF, text file, or an image. Images are read locally in the browser before analysis.")


@app.post("/api/analyze/scam")
def analyze_scam(
    payload: TextAnalysisRequest,
    x_groq_api_key: str = Header(default="", alias="X-Groq-API-Key"),
) -> dict[str, Any]:
    if FORCE_DEMO_MODE or not x_groq_api_key.strip():
        result = demo_scam(payload.text)
        result["mode"] = "demo"
        return result
    try:
        result = groq_json(
            x_groq_api_key,
            """You are the scam-analysis engine for a safety application. Analyze manipulation and fraud indicators, not the victim. Do not claim certainty about caller identity. Explain evidence, recommend independent verification, and never tell the user to continue a risky conversation. Return only the requested JSON.""",
            f"Channel: {payload.channel}\nContent:\n{payload.text}",
            "scam_analysis",
            SCAM_SCHEMA,
        )
        result["mode"] = "groq"
        return result
    except Exception as exc:
        fallback = demo_scam(payload.text)
        fallback.update({"mode": "fallback", "warning": f"Groq request failed: {type(exc).__name__}"})
        return fallback


@app.post("/api/analyze/mental")
def analyze_mental(
    payload: TextAnalysisRequest,
    x_groq_api_key: str = Header(default="", alias="X-Groq-API-Key"),
) -> dict[str, Any]:
    if FORCE_DEMO_MODE or not x_groq_api_key.strip():
        result = demo_mental(payload.text)
        result["mode"] = "demo"
        return result
    try:
        result = groq_json(
            x_groq_api_key,
            """You are a mental-safety support classifier for online bullying and harassment. Do not diagnose mental illness or label people as abusers. Identify observable communication patterns, give brief grounding steps, evidence-preservation steps, and a message to a trusted human. If the text mentions immediate self-harm or credible violence, set urgency to immediate_human_help. Do not give therapy or medical advice. Return only the requested JSON.""",
            payload.text,
            "mental_safety",
            MENTAL_SCHEMA,
        )
        result["mode"] = "groq"
        return result
    except Exception as exc:
        fallback = demo_mental(payload.text)
        fallback.update({"mode": "fallback", "warning": f"Groq request failed: {type(exc).__name__}"})
        return fallback


def link_heuristics(raw_url: str) -> tuple[int, list[str], str]:
    candidate = raw_url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", candidate):
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    score = 0
    findings: list[str] = []
    if parsed.scheme != "https":
        score += 20
        findings.append("The link does not use HTTPS.")
    if not host or "." not in host:
        score += 25
        findings.append("The destination does not look like a normal public domain.")
    if "@" in raw_url:
        score += 25
        findings.append("The URL contains @, which can hide the true destination.")
    if host.startswith("xn--") or ".xn--" in host:
        score += 25
        findings.append("The domain uses internationalized punycode and may imitate another name.")
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        score += 25
        findings.append("The link uses a raw IP address instead of a recognizable domain.")
    shorteners = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "is.gd", "ow.ly", "buff.ly"}
    if host in shorteners:
        score += 15
        findings.append("The destination is hidden behind a URL-shortening service.")
    if host.count("-") >= 3 or host.count(".") >= 4:
        score += 12
        findings.append("The domain is unusually complex.")
    risky_terms = ["login", "verify", "wallet", "giftcard", "secure-account", "update-payment", "free-prize"]
    if any(term in candidate.lower() for term in risky_terms):
        score += 12
        findings.append("The URL contains words commonly used in credential or payment lures.")
    score = min(score, 100)
    return score, findings, candidate


@app.post("/api/analyze/link")
def analyze_link(payload: LinkRequest) -> dict[str, Any]:
    score, findings, normalized = link_heuristics(payload.url)
    level = "critical" if score >= 75 else "high" if score >= 50 else "medium" if score >= 25 else "low"
    return {
        "normalized_url": normalized,
        "risk_score": score,
        "risk_level": level,
        "findings": findings or ["No obvious URL-format warning was detected. This does not prove the destination is safe."],
        "next_steps": [
            "Do not enter credentials after opening an unexpected link.",
            "Navigate to the organization by typing its known official address instead.",
            "Verify the sender through a separate communication channel.",
        ],
        "limitation": "This MVP performs local URL inspection. Production deployment should add DNS, certificate, reputation, redirect-chain, and sandbox checks.",
    }


@app.post("/api/analyze/wildfire")
async def analyze_wildfire(
    activity: str = Form(...),
    wind_mph: float = Form(0),
    temperature_f: float = Form(70),
    humidity_percent: float = Form(40),
    image: UploadFile | None = File(default=None),
    x_groq_api_key: str = Header(default="", alias="X-Groq-API-Key"),
) -> dict[str, Any]:
    if not activity.strip():
        raise HTTPException(status_code=400, detail="Activity is required")

    weather_context = (
        f"Planned activity: {activity}. Wind: {wind_mph} mph. "
        f"Temperature: {temperature_f} F. Relative humidity: {humidity_percent}%."
    )

    if FORCE_DEMO_MODE or not x_groq_api_key.strip() or image is None:
        dryness = max(0, 50 - humidity_percent) + max(0, temperature_f - 80) * 0.5 + wind_mph * 1.5
        high_spark = any(word in activity.lower() for word in ["mow", "grind", "weld", "campfire", "burn", "chainsaw", "generator"])
        score = min(100, int(dryness + (30 if high_spark else 10)))
        return {
            "risk_level": "critical" if score >= 80 else "high" if score >= 55 else "medium" if score >= 30 else "low",
            "risk_score": score,
            "proceed": score < 55,
            "observed_hazards": ["Image inspection unavailable in demo mode."] if image else ["No image supplied."],
            "weather_factors": [weather_context],
            "required_precautions": [
                "Clear dry vegetation away from heat or spark sources.",
                "Keep water and a suitable extinguisher immediately available.",
                "Avoid spark-producing activity during strong wind or extreme dryness.",
                "Follow local restrictions and official fire-weather guidance.",
            ],
            "limitations": "This is a screening tool, not official permission to perform the activity.",
            "mode": "demo",
        }

    data = await image.read()
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image is too large; use an image under 12 MB")
    mime = image.content_type or "image/jpeg"
    image_url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    prompt = f"""You are analyzing a scene for wildfire ignition prevention. {weather_context}
Inspect only visible conditions: dry vegetation, combustible debris, clearances, possible spark/heat sources, nearby structures, access, and available suppression equipment. Do not claim the activity is legally authorized. Return a JSON object with exactly these keys: risk_level (low|medium|high|critical), risk_score (0-100), proceed (boolean), observed_hazards (array of strings), weather_factors (array of strings), required_precautions (array of strings), limitations (string). Be conservative when visibility is poor."""
    try:
        response = groq_chat({
            "model": VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            "temperature": 0.1,
            "max_completion_tokens": 1_000,
            "response_format": {"type": "json_object"},
        }, x_groq_api_key)
        result = json.loads(response["choices"][0]["message"].get("content") or "{}")
        result["mode"] = "groq_vision"
        return result
    except Exception as exc:
        return {
            "risk_level": "unknown",
            "risk_score": 0,
            "proceed": False,
            "observed_hazards": ["The image could not be analyzed."],
            "weather_factors": [weather_context],
            "required_precautions": ["Do not rely on this result; use official local guidance and a qualified human inspection."],
            "limitations": f"Groq vision request failed: {type(exc).__name__}",
            "mode": "fallback",
        }


@app.post("/api/incident/contain")
def contain_incident(payload: IncidentRequest) -> dict[str, Any]:
    actions = [
        {"action": "Preserve evidence", "status": "completed", "detail": "Incident details stored in the local demo database."},
        {"action": "End this app session", "status": "ready", "detail": "The frontend can clear its local session and tokens."},
        {"action": "Revoke external provider tokens", "status": "integration_required", "detail": "Requires OAuth/API integration with each provider."},
        {"action": "Log out other devices", "status": "integration_required", "detail": "Requires the account provider's session-management API."},
        {"action": "Notify trusted contact", "status": "integration_required", "detail": "Connect Twilio, email, or push notification service."},
    ]
    created = utc_now()
    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO incidents(incident_type, details, actions_json, created_at) VALUES (?, ?, ?, ?)",
            (payload.incident_type, payload.details, json.dumps(actions), created),
        )
        incident_id = cursor.lastrowid
    return {"incident_id": incident_id, "created_at": created, "actions": actions}
