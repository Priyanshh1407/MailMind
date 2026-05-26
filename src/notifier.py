import requests
import os

# Best practice: Store these in your .env file, but you can hardcode them here for testing
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_alert(sender, subject, summary):
    """Sends a priority alert to your Telegram account."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    # Formatting the message with Markdown
    message = (
        f"🚨 *MailMind Priority Alert* 🚨\n\n"
        f"👤 *From:* {sender}\n"
        f"📌 *Subject:* {subject}\n\n"
        f"📝 *Snippet:*\n{summary}"
    )
    
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code == 200:
            return True
        else:
            print(f"Telegram API Error: {response.text}")
            return False
    except Exception as e:
        print(f"Failed to connect to Telegram: {e}")
        return False