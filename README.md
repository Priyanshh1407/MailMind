# 🧠 MailMind AI: Autonomous Shadow Deployment Agent

An offline-first, locally hosted AI microservice that autonomously fetches, classifies, and routes high-priority emails. 

MailMind features an enterprise-grade **Dual-LLM Architecture** and a premium **React + Tailwind CSS** frontend dashboard, running background inferences alongside external cloud APIs, complete with real-time UI telemetry and Telegram alerting.

## 🚀 System Architecture
- **Data Ingestion:** Securely polls Gmail API via OAuth2 for unread packets. Includes a monkey-patched WSGI server for seamless HTML redirection upon authentication.
- **Backend API (Python/FastAPI):** Hosts endpoints for fetching emails, syncing live telemetry statuses, user profile management, and Human-in-the-Loop feedback.
- **Dual-LLM Classification:** Every email is evaluated using an external Cloud LLM (Gemini 2.5 Flash / Llama 3.1 8B via Groq) alongside a local model. Emails are bucketed into `IMPORTANT`, `UPDATES`, or `SPAM`.
- **Feedback & Self-Healing:** Human corrections in the UI are saved to a local Vector Database (ChromaDB) to provide dynamic Few-Shot learning context to the AI, ensuring the system learns from mistakes.
- **Premium Frontend:** A beautiful, responsive, glassmorphism dashboard built with React and Vite, featuring dynamic Dicebear robot avatars and live polling badges.

## 🛠️ Tech Stack
* **AI & Machine Learning:** Google GenAI (Gemini), Groq API (Llama 3.1), ChromaDB (Vector Search)
* **Backend API:** FastAPI, Uvicorn, Python 3.10, SQLite
* **Frontend:** React, Vite, Tailwind CSS
* **Integrations:** Gmail API (OAuth2), Telegram Bot API

## ⚡ Quick Start

### 1. Environment Setup (.env)
Create a `.env` file in the root directory:
```
GEMINI_API_KEY=your_key
GROQ_API_KEY=your_key
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_id
```

### 2. Run the Full Stack
Use the provided batch script to launch the FastAPI server, the background AI polling engine, and the Vite React frontend simultaneously:
```bash
./start_all.bat
```