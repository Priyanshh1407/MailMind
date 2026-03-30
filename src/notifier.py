import os
from twilio.rest import Client
from dotenv import load_dotenv

# Load the hidden variables from the .env file
load_dotenv()

# Fetch credentials from .env
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
MY_WHATSAPP_NUMBER = os.getenv("MY_WHATSAPP_NUMBER")

def send_whatsapp_alert(sender, subject, summary):
    """
    Sends a formatted message to your personal WhatsApp via Twilio.
    """
    # Quick safety check to ensure all variables loaded correctly
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER, MY_WHATSAPP_NUMBER]):
        print("Missing Twilio credentials. Check your .env file.")
        return False

    # Initialize the Twilio client
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    # WhatsApp uses standard markdown for bolding instead of HTML tags
    message_text = (
        f"*IMPORTANT EMAIL ALERT*\n\n"
        f"*From:* {sender}\n"
        f"*Subject:* {subject}\n\n"
        f"*Snippet:* {summary}"
    )

    try:
        # Fire the message off to Twilio's servers
        message = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            body=message_text,
            to=MY_WHATSAPP_NUMBER
        )
        
        print(f"Successfully sent WhatsApp alert! Message SID: {message.sid}")
        return True
        
    except Exception as e:
        print(f"Error connecting to Twilio: {e}")
        return False

if __name__ == '__main__':
    # A quick local test to ensure the bot reaches your phone
    print("Testing WhatsApp Notifier...")
    send_whatsapp_alert(
        sender="ceo@stageverse.com", 
        subject="Investment Term Sheet Attached", 
        summary="Please review this before our call tomorrow. - CEO"
    )