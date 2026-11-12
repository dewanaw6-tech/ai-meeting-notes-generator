# AI Meeting Notes Generator

Paste a meeting transcript and get a summary, key decisions, an action items table
and a ready-to-send follow-up email. Built with Flask, vanilla JavaScript and the
OpenRouter API (`openai/gpt-4o-mini`).

## Project structure

```
ai-meeting-notes-generator/
├── app.py
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
├── templates/
│   └── index.html
└── static/
    ├── style.css
    └── script.js
```

## Setup

1. Install Python 3.9 or newer.
2. Create a virtual environment and install dependencies:

   ```bash
   python -m venv venv
   source venv/bin/activate        # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and add your key from https://openrouter.ai/keys:

   ```
   OPENROUTER_API_KEY=your_api_key_here
   OPENROUTER_MODEL=openai/gpt-4o-mini
   ```

4. Run the app and open http://127.0.0.1:5000

   ```bash
   python app.py
   ```

## Routes

| Route             | Method | Purpose                                              |
|-------------------|--------|------------------------------------------------------|
| `/`               | GET    | Dashboard                                            |
| `/generate`       | POST   | Transcript in, structured notes + email out (JSON)   |
| `/generate-email` | POST   | Regenerate the follow-up email in a chosen tone      |
| `/download-pdf`   | POST   | Export the notes as a PDF                            |

## Notes

- The API key stays in `.env` on the server; the browser never sees it.
- The AI is instructed to use only what is in the transcript. Missing owners or
  deadlines show as "Not specified".
- Transcripts up to 60,000 characters are accepted.
- Handled errors: empty input, missing or invalid API key, no internet, timeouts,
  rate limits, insufficient credits and malformed AI responses.


## Deploy on Render

1. Push this project folder to a GitHub repository.
2. In Render, create a new **Web Service** and connect that repository.
3. Set **Build Command** to:

   ```bash
   pip install -r requirements.txt
   ```

4. Set **Start Command** to:

   ```bash
   gunicorn app:app
   ```

5. Add these environment variables in Render:

   - `OPENROUTER_API_KEY` = your real OpenRouter API key
   - `OPENROUTER_MODEL` = `openai/gpt-4o-mini`

6. Deploy the service. Do not upload your real `.env` file or API key to GitHub.
