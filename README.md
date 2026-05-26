# 🧠 MailMind AI: Autonomous Shadow Deployment Agent

An offline-first, locally hosted AI microservice that autonomously fetches, classifies, and routes high-priority emails using a fine-tuned DistilBERT model. 

MailMind features an enterprise-grade **Shadow Deployment (A/B Testing)** architecture, running local neural inferences in milliseconds alongside external cloud APIs, complete with real-time CLI telemetry and Telegram alerting.

## 🚀 System Architecture
- **Data Ingestion:** Securely polls Gmail API via OAuth2 for unread packets.
- **Microservice Brain (Dockerized):** A FastAPI backend hosting a Hugging Face NLP model, strictly isolated within a containerized Linux environment.
- **Shadow Telemetry:** Dual-engine classification. Every email is evaluated by both an External Cloud LLM and the Local MailMind Engine, comparing latency, confidence scoring, and model agreement dynamically.
- **Event-Driven Routing:** Priority communications instantly trigger a webhook to a Telegram Bot for real-time mobile push notifications.

## 🛠️ Tech Stack
* **AI & Machine Learning:** PyTorch, Hugging Face Transformers (`DistilBERT`)
* **Backend API:** FastAPI, Uvicorn, Python 3.10
* **DevOps & Deployment:** Docker, WSL2 (Linux Subsystem)
* **UI & Telemetry:** Rich (Terminal User Interface)
* **Integrations:** Gmail API, Telegram Bot API

## ⚡ Quick Start

### 1. Environment Setup
GEMINI_API_KEY

TELEGRAM_BOT_TOKEN 
TELEGRAM_CHAT_ID 