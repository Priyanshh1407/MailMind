from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sys
import os
import json

# We need to tell Python where to find your src folder
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.local_llm import MailMindModel
from src.db_utils import get_recent_emails, clear_all_emails, update_human_label
from src.email_client import authenticate_gmail
from src.main import run_agent
from src import vector_db

# 1. Define the Expected Input (Data Validation)
class EmailRequest(BaseModel):
    subject: str
    body: str

class FeedbackRequest(BaseModel):
    email_id: str
    subject: str
    body: str
    label: str  # IMPORTANT, UPDATES, or SPAM

# 2. Initialize the Application
app = FastAPI(
    title="MailMind Local Inference API",
    description="Offline AI for classifying priority emails."
)

# Enable CORS for the React Frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. Load the AI into Memory at Startup
print("Starting server and booting up AI Model...")
ai_model = MailMindModel(model_path="models/MailMind-Final") 

# 4. Define Endpoints
@app.post("/predict")
async def predict_email(request: EmailRequest):
    try:
        prediction, confidence = ai_model.predict(request.subject, request.body)
        return {
            "subject": request.subject,
            "prediction": prediction,
            "confidence_score": confidence, 
            "status": "success"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/emails")
async def get_emails(limit: int = 50):
    try:
        token_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'token.json'))
        local_token_path = 'token.json'
        is_logged_in = os.path.exists(token_path) or os.path.exists(local_token_path)
        
        if not is_logged_in:
            return {"emails": [], "status": "unauthorized"}
            
        emails = get_recent_emails(limit)
        return {"emails": emails, "status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/feedback")
async def add_feedback(request: FeedbackRequest):
    try:
        # Save this feedback to the Vector Database to self-heal
        vector_db.add_email_to_vector_db(
            email_id=request.email_id,
            subject=request.subject,
            body=request.body,
            label=request.label
        )
        # Update SQLite DB so the UI sees it
        update_human_label(request.email_id, request.label)
        return {"status": "success", "message": f"Added to vector DB as {request.label}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Health check endpoint
@app.get("/")
async def root():
    return {"message": "MailMind API is online and ready."}

@app.get("/status")
async def get_status():
    status_file = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data', 'polling_status.json'))
    if os.path.exists(status_file):
        try:
            with open(status_file, 'r') as f:
                return json.load(f)
        except:
            return {"is_polling": False}
    return {"is_polling": False}

@app.get("/user")
async def get_user():
    profile_file = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data', 'user_profile.json'))
    if os.path.exists(profile_file):
        try:
            with open(profile_file, 'r') as f:
                return json.load(f)
        except:
            return {"email": "Admin User"}
    return {"email": "Admin User"}

@app.post("/logout")
async def logout():
    try:
        token_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'token.json'))
        local_token_path = 'token.json'
        profile_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data', 'user_profile.json'))
        
        deleted = False
        for path in [token_path, local_token_path, profile_path]:
            if os.path.exists(path):
                os.remove(path)
                deleted = True
                
        # Wipe all DB records
        clear_all_emails()
                
        if deleted:
            return {"status": "success", "message": "Token deleted and DB cleared."}
        else:
            return {"status": "success", "message": "No token found, but DB cleared."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/authenticate")
async def trigger_auth():
    try:
        # Trigger the browser OAuth flow on the server side
        authenticate_gmail()
        # Instantly run a fetch cycle so the UI gets new emails
        run_agent()
        return {"status": "success", "message": "Authentication complete."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))