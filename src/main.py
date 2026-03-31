import time
import schedule
from email_client import authenticate_gmail,get_unread_emails, mark_as_read
from llm_api import classify_email
from notifier import send_whatsapp_alert
from db_utils import log_email_to_db

def run_agent():
    print("Initializing MailMind Agent...")

    # 1. Boot up the email connection
    gmail_service = authenticate_gmail()
    if not gmail_service:
        print("Failed to connect to Gmail. Exiting.")
        return
    
    # 2. Fetch raw data
    print("Fetching unread emails...")
    # Fetch 5 emails for testing purposes
    emails = get_unread_emails(gmail_service,max_results=5)

    if not emails:
        print("Inbox is clean! No unread emails to process.")
        return
    
    print("--- Starting email classification ---")

    # 3. Core Agent Loop: Process each email through the LLM
    for email in emails:
        print(f"\nEvaluating Email ID: {email['id']}")
        print(f"From: {email['sender']}")
        print(f"Subject: {email['subject']}")

        # Call the gemini model
        decision = classify_email(
            sender=email['sender'], 
            subject=email['subject'], 
            body_snippet=email['body_snippet']
        )

        # THE CIRCUIT BREAKER
        if decision == "ERROR":
            print("\n🚨 [SYSTEM HALT] AI processing failed. Halting batch to protect email state.")
            print("The remaining emails will be kept as UNREAD. Trying again on next scheduled run.")
            break  # This completely exits the for-loop immediately!

        # Storing the result in the database for future reference and potential human review
        log_email_to_db(
            email_id=email['id'],
            sender=email['sender'],
            subject=email['subject'],
            body=email['body_snippet'],
            prediction=decision
        )

        # 4. Display the LLM's decision
        if decision == "IMPORTANT":
            # Using a simple ANSI escape code to print IMPORTANT in green for visibility
            print(f"Verdict: \033[92m{decision}\033[0m")
            print("Action: Triggering WhatsApp alert...")

            success = send_whatsapp_alert(
                sender=email['sender'], 
                subject=email['subject'], 
                summary=email['body_snippet'][:100] + "..."
            )

            if success:
                print("WhatsApp alert sent successfully!")
            else:
                print("Failed to send WhatsApp alert.")
        else:
            # Print IGNORE in a muted grey/standard color
            print(f"Verdict: {decision}")
            print("Action: Ignoring this email.")

        # STATE CHANGE: Mark the email as read regardless of the AI's decision
        if mark_as_read(gmail_service, email['id']):
            print("-> State Updated: Marked as Read in Gmail.")

        print("-"*40)

if __name__ == "__main__":
    print("Starting the MailMind Background Service...")

    run_agent()

    schedule.every(1).hour.do(run_agent)

    print("\nService is now active. Polling Gmail every 1 hour.")
    print("Press Ctrl+C to stop the process.\n")

    while True:
        schedule.run_pending()
        time.sleep(1)