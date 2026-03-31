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
    # This is Prompt Engineering. We are giving the AI a very strict persona and rules.
    prompt = f"""
    You are an elite executive assistant filtering emails for a software engineer. 
    Review the following email and classify it as either 'IMPORTANT' or 'IGNORE'.
    
    Rules:
    - Respond ONLY with the word 'IMPORTANT' or 'IGNORE'. Do not add any other text, punctuation, or explanation.
    
    Classify as 'IMPORTANT' if the email matches ANY of these criteria:
    1. AI/ML Opportunities: Any mention of an internship, interview, assessment, recruiter outreach, or job application status related to Artificial Intelligence (AI), Machine Learning (ML), or Deep Learning.
    2. StageVerse Project: Any correspondence mentioning "StageVerse", specifically feedback, inquiries, beta testing, or discussions from Lighting Designers (LDs) or concert production crew.
    3. Critical Personal Communications: Direct, personalized messages from real humans, university faculty, urgent account alerts, or calendar invites.

    Classify as 'IGNORE' if the email is:
    - General newsletters or digests (even if they discuss AI, ML, or concert production).
    - Marketing, promotional offers, or automated social media updates.
    - Cold sales pitches or generic software vendor emails.

    Email Data:
    Sender: {sender}
    Subject: {subject}
    Body: {body_snippet}
    """

    try:
        # Attempt 1: The Primary Model
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        return response.text.strip().upper()
        
    except Exception as e:
        error_msg = str(e).lower()
        # Check if the error is a Rate Limit / Quota issue
        if "429" in error_msg or "exhausted" in error_msg or "quota" in error_msg:
            print("[API Warning] Primary model quota exhausted. Falling back to flash-lite...")
            
            try:
                # Attempt 2: The Backup Model
                fallback_response = client.models.generate_content(
                    model="gemini-2.5-flash-lite",
                    contents=prompt
                )
                return fallback_response.text.strip().upper()
                
            except Exception as fallback_e:
                print(f"[API Error] Fallback model also failed: {fallback_e}")
                return "ERROR"
        else:
            # If it's a different error (like no internet), just fail safely
            print(f"[API Error] Classification failed: {e}")
            return "ERROR"
    
if __name__ == "__main__":
    # A quick local test to make sure our API key and prompt are working
    test_sender = "boss@company.com"
    test_subject = "Urgent: Project Deadline Moved Up"
    test_body = "Priyansh, we need to talk about the delivery timeline. Call me ASAP."
    
    print("Sending test email to Gemini for classification...")
    result = classify_email(test_sender, test_subject, test_body)
    print(f"LLM Decision: {result}")