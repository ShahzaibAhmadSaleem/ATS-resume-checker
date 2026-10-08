# 📄 ATS Resume Checker

A Streamlit app that scores a resume for ATS (Applicant Tracking System) compatibility and gives specific, prioritized suggestions to improve it, powered by the Groq API.

## Features
- Upload a resume as **PDF, DOCX or TXT**
- Optional **job description** for keyword matching
- Overall ATS score = 70% AI evaluation + 30% rule-based checks (contact info, sections, length, bullets, metrics, keyword match)
- Score breakdown: formatting, keywords, content quality, readability
- Strengths, weaknesses, missing keywords, and prioritized improvements with example rewrites
- Download the full report as JSON

## Run locally
```bash
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## API key
Get a key from <https://console.groq.com/keys> (starts with `gsk_`). Provide it in one of these ways:

1. **Sidebar** — paste it into the app (easiest for local use)
2. **Environment variable** — `export GROQ_API_KEY="your_key"`
3. **Streamlit secrets** — create `.streamlit/secrets.toml`:
   ```toml
   GROQ_API_KEY = "your_key"
   ```

**Never commit your API key.** `.gitignore` already excludes `.streamlit/secrets.toml` and `.env`.

## Model
The default model is `llama-3.3-70b-versatile`. You can change it in the sidebar or set the `GROQ_MODEL` environment variable.

## Deploy on Streamlit Community Cloud
1. Push this repo to GitHub.
2. Go to <https://share.streamlit.io> and sign in with GitHub.
3. Click **Create app** → choose your repo, branch `main`, main file `app.py`.
4. Open **Advanced settings → Secrets** and add: `GROQ_API_KEY = "your_key"`
5. Click **Deploy**.

## Notes
- Scanned/image-only PDFs have no extractable text; export a text-based PDF instead.
- The ATS score is an estimate. Real ATS systems vary.
- Resume text is sent to the Groq API for analysis; this app does not store it.
