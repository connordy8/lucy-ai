# Lucy — Beth's AI Companion

AI-powered phone companion for Beth. Makes daily check-in calls and class reminders using Vapi + OpenAI.

## Calls
- **Morning (10 AM PT)**: Check in, review calendar, ask about reminders
- **Evening (11:30 PM PT)**: Goodnight call, CPAP reminder, tomorrow's agenda
- **Class reminders (45 min before)**: Zumba and Aquacise only

## Architecture
- **Vapi**: Voice AI platform (telephony + LLM + TTS)
- **OpenAI GPT-4o-mini**: Conversation model
- **OpenAI TTS (Nova)**: Voice
- **Deepgram nova-3**: Speech-to-text
- **Google Calendar API**: Schedule data
- **Vercel**: Serverless API for live calendar lookups during calls
- **GitHub Actions**: Scheduled call triggers

## Secrets Required
- `VAPI_API_KEY`
- `OPENAI_API_KEY`
- `GOOGLE_SERVICE_ACCOUNT_KEY`
- `GOOGLE_CALENDAR_ID`
- `BETH_PHONE_NUMBER` (home: +19252781199)
- `BETH_CELL_NUMBER` (+14403211704)
