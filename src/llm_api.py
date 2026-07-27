import os 
from google import genai
from dotenv import load_dotenv
import sys

sys.path.append(os.path.dirname(__file__))
import vector_db

# Load environment variables from .env file
load_dotenv()

# Configure the Gemini API key with secret key
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise ValueError("No API key found. Please check your .env file.")

# Initialize the Gemini API client
client = genai.Client(api_key=api_key)

def classify_email(sender, subject, body_snippet):
    # 1. Fetch Dynamic Few-Shot Examples from Vector DB
    similar_emails = vector_db.search_similar_emails(subject, body_snippet, k=3)
    
    few_shot_context = ""
    if similar_emails:
        few_shot_context = "\n### User Precedents (Learn from these past classifications) ###\n"
        for i, em in enumerate(similar_emails):
            few_shot_context += f"Example {i+1}:\n"
            few_shot_context += f"{em['text']}\n"
            few_shot_context += f"User Classified As: {em['label']}\n\n"

    # 2. Build the Dynamic Prompt
    prompt = f"""
    You are an elite executive assistant filtering emails for a software engineer. 
    Review the following email and classify it strictly into one of three categories: 'IMPORTANT', 'UPDATES', or 'SPAM'.
    
    Rules:
    - Respond ONLY with the exact word 'IMPORTANT', 'UPDATES', or 'SPAM'. Do not add any other text.
    
    Category Definitions:
    1. IMPORTANT: Needs immediate attention. Direct messages, personal communications, urgent alerts, interview scheduling, or calendar invites.
    2. UPDATES: Useful but not urgent. Internship/job open positions, tool newsletters (like Supabase, AWS), tech updates.
    3. SPAM: Unwanted junk, general marketing, cold sales pitches, platforms like 'Unstop', promotional offers.
    {few_shot_context}
    
    ### Email to Classify ###
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
        prediction = response.text.strip().upper()
        
        # Sanitize output just in case
        if prediction not in ["IMPORTANT", "UPDATES", "SPAM"]:
            return "SPAM"
            
        return prediction
        
    except Exception as e:
        error_msg = str(e).lower()
        if "429" in error_msg or "exhausted" in error_msg or "quota" in error_msg:
            print("[API Warning] Primary model quota exhausted. Falling back to flash-lite...")
            try:
                fallback_response = client.models.generate_content(
                    model="gemini-2.5-flash-lite",
                    contents=prompt
                )
                prediction = fallback_response.text.strip().upper()
                if prediction not in ["IMPORTANT", "UPDATES", "SPAM"]:
                    return "SPAM"
                return prediction
            except Exception as fallback_e:
                print(f"[API Error] Fallback model also failed: {fallback_e}")
                
                # Ultimate Fallback: ChatGroq
                print("[API Warning] Both Gemini models failed. Triggering ultimate fallback (Groq/Llama3)...")
                try:
                    import requests
                    groq_api_key = os.getenv("GROQ_API_KEY")
                    if not groq_api_key:
                        raise ValueError("GROQ_API_KEY missing from .env")
                        
                    headers = {
                        "Authorization": f"Bearer {groq_api_key}",
                        "Content-Type": "application/json"
                    }
                    data = {
                        "model": "llama-3.1-8b-instant",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1
                    }
                    groq_resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=data, timeout=5)
                    groq_resp.raise_for_status()
                    
                    prediction = groq_resp.json()["choices"][0]["message"]["content"].strip().upper()
                    if prediction not in ["IMPORTANT", "UPDATES", "SPAM"]:
                        return "SPAM"
                    return prediction
                except Exception as groq_e:
                    print(f"[API Error] Ultimate Groq fallback also failed: {groq_e}")
                    return "ERROR"
        else:
            print(f"[API Error] Classification failed: {e}")
            return "ERROR"
    
if __name__ == "__main__":
    test_sender = "boss@company.com"
    test_subject = "Urgent: Project Deadline Moved Up"
    test_body = "Priyansh, we need to talk about the delivery timeline. Call me ASAP."
    print("Sending test email to Gemini for classification...")
    result = classify_email(test_sender, test_subject, test_body)
    print(f"LLM Decision: {result}")