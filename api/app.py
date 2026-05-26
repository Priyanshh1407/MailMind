from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import sys
import os

# We need to tell Python where to find your src folder
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.local_llm import MailMindModel

# 1. Define the Expected Input (Data Validation)
class EmailRequest(BaseModel):
    subject: str
    body: str

# 2. Initialize the Application
app = FastAPI(
    title="MailMind Local Inference API",
    description="Offline AI for classifying priority emails."
)

# 3. Load the AI into Memory at Startup
print("Starting server and booting up AI Model...")
# This uses the class you just built!
ai_model = MailMindModel(model_path="models/MailMind-Final") 

# 4. Define the Prediction Endpoint
@app.post("/predict")
async def predict_email(request: EmailRequest):
    try:
        # Catch both the label and the confidence score
        prediction, confidence = ai_model.predict(request.subject, request.body)
        
        return {
            "subject": request.subject,
            "prediction": prediction,
            "confidence_score": confidence, # Add it to the JSON response
            "status": "success"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Health check endpoint
@app.get("/")
async def root():
    return {"message": "MailMind API is online and ready."}