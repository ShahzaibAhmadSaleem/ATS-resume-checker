"""ATS Resume Checker - Streamlit app powered by the Groq API."""

import io
import json
import os
import re

import streamlit as st
from docx import Document
from pypdf import PdfReader

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "llama-3.3-70b-versatile"
MAX_RESUME_CHARS = 15000
MIN_RESUME_CHARS = 100

SYSTEM_PROMPT = """You are an expert ATS (Applicant Tracking System) analyst and \
professional resume reviewer. Evaluate the resume honestly and strictly. \
Respond with a single valid JSON object and nothing else (no markdown, no commentary)."""

USER_PROMPT_TEMPLATE = """Analyze the resume below{jd_clause}.

Return JSON with exactly this structure:
{{
  "ats_score": <integer 0-100>,
  "summary": "<2-3 sentence overall assessment>",
  "section_scores": {{
    "formatting": <integer 0-100>,
    "keywords": <integer 0-100>,
    "content_quality": <integer 0-100>,
    "readability": <integer 0-100>
  }},
  "strengths": ["<short point>", "..."],
  "weaknesses": ["<short point>", "..."],
  "missing_keywords": ["<keyword>", "..."],
  "improvements": [
    {{
      "priority": "High" | "Medium" | "Low",
      "issue": "<what is wrong>",
      "suggestion": "<what to do>",
      "example": "<a concrete rewritten line or snippet, or empty string>"
    }}
  ]
}}

Rules:
- Be specific to THIS resume; quote or reference actual content.
- Give 5-8 improvements, ordered by priority.
- If no job description is given, base missing_keywords on common keywords for the \
candidate's apparent target role.
{jd_block}
RESUME:
\"\"\"
{resume}
\"\"\""""


# --------------------------------------------------------------------------- #
# File parsing
# --------------------------------------------------------------------------- #
def extract_text(filename: str, data: bytes) -> str:
    """Extract plain text from a PDF, DOCX or TXT file."""
    name = filename.lower()
    if name.endswith(".pdf"):
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                raise ValueError("This PDF is password protected.")
        pages = [(page.extract_text() or "") for page in reader.pages]
        return "\n".join(pages).strip()
    if name.endswith(".docx"):
        doc = Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(parts).strip()
    if name.endswith(".txt"):
        return data.decode("utf-8", errors="ignore").strip()
    raise ValueError("Unsupported file type. Please upload a PDF, DOCX or TXT file.")


# --------------------------------------------------------------------------- #
# Rule-based checks (fast, deterministic, no API needed)
# --------------------------------------------------------------------------- #
STOPWORDS = set(
    """a an the and or of to in for with on at by from as is are was were be been this
    that these those it its you your we our they their will can must should may have has
    had not but if than then so such into about over under per etc using use used work
    working experience years year ability strong skills skill team role job candidate
    responsibilities requirements preferred required plus including include""".split()
)


def _tokens(text: str) -> set:
    words = re.findall(r"[a-zA-Z][a-zA-Z+#.\-]{1,}", text.lower())
    return {w.strip(".-") for w in words if w not in STOPWORDS and len(w) > 2}


def rule_based_checks(text: str, job_description: str = "") -> dict:
    """Return a heuristic score (0-100) and a list of pass/fail checks."""
    lower = text.lower()
    word_count = len(text.split())
    checks = []  # (label, passed, weight)

    checks.append(("Email address found", bool(re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)), 10))
    checks.append(("Phone number found", bool(re.search(r"(\+?\d[\d\s().-]{8,}\d)", text)), 5))
    checks.append(("LinkedIn / GitHub / portfolio link", bool(re.search(r"linkedin\.com|github\.com|portfolio", lower)), 5))
    checks.append(("Experience section", bool(re.search(r"\b(experience|employment|work history|internship)\b", lower)), 15))
    checks.append(("Education section", bool(re.search(r"\b(education|academic|university|college|bachelor|master|degree)\b", lower)), 10))
    checks.append(("Skills section", bool(re.search(r"\b(skills|technologies|tech stack|competencies)\b", lower)), 15))
    checks.append(("Summary / objective or projects", bool(re.search(r"\b(summary|objective|profile|projects?)\b", lower)), 5))
    checks.append(("Good length (250-900 words)", 250 <= word_count <= 900, 10))
    bullets = len(re.findall(r"^\s*[•\-\*●▪◦–]\s+", text, flags=re.MULTILINE))
    checks.append(("Uses bullet points", bullets >= 5, 5))
    numbers = len(re.findall(r"\d+\s*%|\$\s*\d+|\b\d{2,}\b", text))
    checks.append(("Quantified achievements (numbers / %)", numbers >= 4, 10))

    keyword_match = None
    missing_from_jd = []
    if job_description.strip():
        jd_tokens = _tokens(job_description)
        resume_tokens = _tokens(text)
        if jd_tokens:
            matched = jd_tokens & resume_tokens
            keyword_match = round(100 * len(matched) / len(jd_tokens))
            missing_from_jd = sorted(jd_tokens - resume_tokens)[:25]
        checks.append(("Job description keyword match >= 40%", (keyword_match or 0) >= 40, 10))

    total_weight = sum(w for _, _, w in checks)
    earned = sum(w for _, ok, w in checks if ok)
    score = round(100 * earned / total_weight) if total_weight else 0
    return {
        "score": score,
        "checks": [(label, ok) for label, ok, _ in checks],
        "word_count": word_count,
        "keyword_match": keyword_match,
        "missing_from_jd": missing_from_jd,
    }


# --------------------------------------------------------------------------- #
# Grok API
# --------------------------------------------------------------------------- #
def build_messages(resume_text: str, job_description: str = "") -> list:
    jd = job_description.strip()
    jd_clause = " against the job description provided" if jd else ""
    jd_block = f'\nJOB DESCRIPTION:\n"""\n{jd[:6000]}\n"""\n' if jd else ""
    user_prompt = USER_PROMPT_TEMPLATE.format(
        jd_clause=jd_clause, jd_block=jd_block, resume=resume_text[:MAX_RESUME_CHARS]
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def parse_json_response(raw: str) -> dict:
    """Parse JSON from a model reply, tolerating code fences and extra prose."""
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            return json.loads(raw[start : end + 1])
        raise ValueError("The AI response was not valid JSON. Please try again.")


def _clamp(value, default=0) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return default


def _str_list(value) -> list:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def normalize_result(data: dict) -> dict:
    """Make sure the AI result has every field the UI expects."""
    if not isinstance(data, dict):
        raise ValueError("Unexpected AI response format.")
    sections = data.get("section_scores") or {}
    improvements = []
    for item in data.get("improvements") or []:
        if isinstance(item, dict):
            priority = str(item.get("priority", "Medium")).title()
            improvements.append(
                {
                    "priority": priority if priority in ("High", "Medium", "Low") else "Medium",
                    "issue": str(item.get("issue", "")).strip(),
                    "suggestion": str(item.get("suggestion", "")).strip(),
                    "example": str(item.get("example", "")).strip(),
                }
            )
        elif isinstance(item, str) and item.strip():
            improvements.append(
                {"priority": "Medium", "issue": "", "suggestion": item.strip(), "example": ""}
            )
    return {
        "ats_score": _clamp(data.get("ats_score")),
        "summary": str(data.get("summary", "")).strip(),
        "section_scores": {
            "Formatting": _clamp(sections.get("formatting")),
            "Keywords": _clamp(sections.get("keywords")),
            "Content quality": _clamp(sections.get("content_quality")),
            "Readability": _clamp(sections.get("readability")),
        },
        "strengths": _str_list(data.get("strengths")),
        "weaknesses": _str_list(data.get("weaknesses")),
        "missing_keywords": _str_list(data.get("missing_keywords")),
        "improvements": improvements,
    }


def analyze_resume(client, model: str, resume_text: str, job_description: str = "") -> dict:
    """Call Groq (OpenAI-compatible endpoint) and return a normalized result."""
    messages = build_messages(resume_text, job_description)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
    except Exception as first_error:
        # Some models reject response_format; retry once without it.
        if "response_format" in str(first_error).lower():
            response = client.chat.completions.create(
                model=model, messages=messages, temperature=0.2
            )
        else:
            raise
    content = response.choices[0].message.content
    return normalize_result(parse_json_response(content))


def get_client(api_key: str):
    from openai import OpenAI  # imported lazily so the UI loads even if missing

    return OpenAI(api_key=api_key, base_url=GROQ_BASE_URL, timeout=120.0)


def clean_key(key: str) -> str:
    """Remove spaces, newlines and accidental quotes around a pasted key."""
    return (key or "").strip().strip("\"'").strip()


def get_default_api_key() -> str:
    key = ""
    try:
        # GROQ_API_KEY preferred; XAI_API_KEY accepted so an existing secret still works
        key = st.secrets.get("GROQ_API_KEY", "") or st.secrets.get("XAI_API_KEY", "")
    except Exception:  # no secrets file present
        pass
    return clean_key(key or os.getenv("GROQ_API_KEY", "") or os.getenv("XAI_API_KEY", ""))


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def score_color(score: int) -> str:
    return "🟢" if score >= 75 else "🟡" if score >= 50 else "🔴"


def render_results(ai: dict, rules: dict) -> None:
    final = round(0.7 * ai["ats_score"] + 0.3 * rules["score"])

    st.subheader("Results")
    c1, c2, c3 = st.columns(3)
    c1.metric("Overall ATS score", f"{final}/100")
    c2.metric("AI evaluation", f"{ai['ats_score']}/100")
    c3.metric("Rule-based checks", f"{rules['score']}/100")
    st.progress(final / 100)
    st.caption(f"{score_color(final)} Overall = 70% AI evaluation + 30% rule-based checks.")

    if ai["summary"]:
        st.info(ai["summary"])

    st.markdown("#### Score breakdown")
    cols = st.columns(len(ai["section_scores"]))
    for col, (name, value) in zip(cols, ai["section_scores"].items()):
        col.metric(name, f"{value}/100")

    left, right = st.columns(2)
    with left:
        st.markdown("#### ✅ Strengths")
        for s in ai["strengths"] or ["No strengths reported."]:
            st.markdown(f"- {s}")
    with right:
        st.markdown("#### ⚠️ Weaknesses")
        for w in ai["weaknesses"] or ["No weaknesses reported."]:
            st.markdown(f"- {w}")

    st.markdown("#### 🛠️ Suggested improvements")
    icons = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}
    order = {"High": 0, "Medium": 1, "Low": 2}
    for item in sorted(ai["improvements"], key=lambda i: order[i["priority"]]):
        title = item["issue"] or item["suggestion"][:80]
        with st.expander(f"{icons[item['priority']]} {item['priority']} — {title}"):
            if item["suggestion"]:
                st.markdown(f"**What to do:** {item['suggestion']}")
            if item["example"]:
                st.markdown("**Example:**")
                st.code(item["example"], language=None)
    if not ai["improvements"]:
        st.write("No improvements reported.")

    if ai["missing_keywords"]:
        st.markdown("#### 🔑 Missing keywords (AI)")
        st.write(", ".join(f"`{k}`" for k in ai["missing_keywords"]))

    with st.expander("Rule-based checklist"):
        for label, ok in rules["checks"]:
            st.markdown(f"{'✅' if ok else '❌'} {label}")
        st.caption(f"Word count: {rules['word_count']}")
        if rules["keyword_match"] is not None:
            st.markdown(f"**Job description keyword match:** {rules['keyword_match']}%")
            if rules["missing_from_jd"]:
                st.write("Words from the job description not found in your resume: "
                         + ", ".join(f"`{k}`" for k in rules["missing_from_jd"]))

    report = {"overall_score": final, "ai": ai, "rule_based": rules}
    st.download_button(
        "⬇️ Download report (JSON)",
        data=json.dumps(report, indent=2, ensure_ascii=False),
        file_name="ats_report.json",
        mime="application/json",
    )


def main() -> None:
    st.set_page_config(page_title="ATS Resume Checker", page_icon="📄", layout="wide")
    st.title("📄 ATS Resume Checker")
    st.write("Upload your resume to get an ATS score and specific suggestions to improve it.")

    with st.sidebar:
        st.header("⚙️ Settings")
        default_key = get_default_api_key()
        api_key = default_key
        if default_key:
            st.success("API key loaded from secrets / environment.")
        else:
            api_key = st.text_input(
                "Groq API key",
                type="password",
                help="Get a key at https://console.groq.com/keys",
            )
        model = st.text_input(
            "Model",
            value=os.getenv("GROQ_MODEL", DEFAULT_MODEL),
            help="Any chat model available on your Groq account.",
        )
        st.caption("Your resume is sent to the Groq API for analysis and is not stored by this app.")

    uploaded = st.file_uploader("Upload resume", type=["pdf", "docx", "txt"])
    job_description = st.text_area(
        "Job description (optional, improves keyword matching)",
        height=150,
        placeholder="Paste the job description here...",
    )

    if st.button("Analyze resume", type="primary", disabled=uploaded is None):
        api_key = clean_key(api_key)
        if not api_key:
            st.error("Please enter your Groq API key in the sidebar.")
            return
        if not model.strip():
            st.error("Please enter a model name.")
            return

        try:
            text = extract_text(uploaded.name, uploaded.getvalue())
        except Exception as e:
            st.error(f"Could not read the file: {e}")
            return
        if len(text) < MIN_RESUME_CHARS:
            st.error(
                "Very little text could be extracted. If your resume is a scanned image, "
                "ATS systems cannot read it either — export a text-based PDF or DOCX."
            )
            return

        try:
            with st.spinner("Analyzing your resume..."):
                client = get_client(api_key)
                ai = analyze_resume(client, model.strip(), text, job_description)
        except Exception as e:
            msg = str(e)
            low = msg.lower()
            if "401" in msg or "api key" in low or "unauthorized" in low:
                st.error("Authentication failed. Check that the key is correct, active and "
                         "from console.groq.com (a Groq key starts with gsk_).")
            elif "403" in msg or "credit" in low or "permission" in low:
                st.error("Access denied. Your Groq account may lack "
                         "access to this model, or you hit a rate limit. Try again or change the model.")
            elif "404" in low or "model" in low:
                st.error("Model not found. Change the model name in the sidebar.")
            else:
                st.error("Analysis failed.")
            with st.expander("Technical details from the API"):
                st.code(msg, language=None)
            return

        rules = rule_based_checks(text, job_description)
        render_results(ai, rules)

        with st.expander("Extracted resume text (what an ATS would read)"):
            st.text(text[:5000] + ("..." if len(text) > 5000 else ""))


if __name__ == "__main__":
    main()
