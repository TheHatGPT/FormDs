# Form D & SEDAR+ Scout Discord Bot

A 24/7 owner-locked Discord DM bot that polls **SEC EDGAR (Form D)** and **SEDAR+ (Form 45-106F1)**, filters for small operating companies meeting your criteria, verifies websites and executive profiles using **Exa.ai** + **TypeSafe.ai (`noul` QC)** + **OpenRouter (`google/gemini-2.5-flash`)**, enriches and validates executive emails via **Apify** (`vulnv/linkedin-email-finder` + `michael.g/email-verifier-validator`), and provides full step-by-step token and USD cost auditing.

## Quick Setup

1. Copy `.env.example` to `.env` and fill in your keys (`DISCORD_BOT_TOKEN`, `OWNER_DISCORD_ID`, `SEC_USER_AGENT`, `OPENROUTER_API_KEY`, `EXA_API_KEY`, `TYPESAFE_API_KEY`, `APIFY_API_TOKEN`).
2. Create a virtual environment and install dependencies:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   playwright install chromium
   ```
3. Run the bot:
   ```powershell
   python bot.py
   ```
