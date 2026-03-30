import os 
from google import genai
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configure the Gemini API key with secret key
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise ValueError("No API key found. Please check your .env file.")

# Initialize the Gemini API client
client = genai.Client(api_key=api_key)

def classify_email(sender, subject, body_snippet):
    """
    Passes email data to the LLM and forces a binary classification.
    """
    prompt= f"""
    You are an elite executive assistant filtering emails. 
    Review the following email and classify it as either 'IMPORTANT' or 'IGNORE'.
    
    Rules:
    - Respond ONLY with the word 'IMPORTANT' or 'IGNORE'. Do not add any other text, punctuation, or explanation.
    - Newsletters, marketing, promotions, and automated reports should be 'IGNORE'.
    - Direct messages from real humans, urgent account alerts, or calendar invites should be 'IMPORTANT'.

    Email Data:
    Sender: {sender}
    Subject: {subject}
    Body: {body_snippet}
    """

    try:
        # Send the prompt to the model
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt
        )
        # Strip removes any accidental invisible spaces or newlines the AI might add
        classification = response.text.strip().upper()
        return classification
    except Exception as e:
        print(f"Error during classification: {e}")
        return "ERROR"
    
if __name__ == "__main__":
    # A quick local test to make sure our API key and prompt are working
    test_sender = "boss@company.com"
    test_subject = "Urgent: Project Deadline Moved Up"
    test_body = "Priyansh, we need to talk about the delivery timeline. Call me ASAP."
    
    print("Sending test email to Gemini for classification...")
    result = classify_email(test_sender, test_subject, test_body)
    print(f"LLM Decision: {result}")