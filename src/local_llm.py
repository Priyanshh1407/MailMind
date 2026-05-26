import torch
import torch.nn.functional as F  # NEW: We need this for Softmax
from transformers import AutoTokenizer, AutoModelForSequenceClassification

class MailMindModel:
    def __init__(self, model_path="models/MailMind-Final"):
        print(f"Loading local model from {model_path}...")
        self.tokenizer = AutoTokenizer.from_pretrained(model_path)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_path)
        self.model.eval()
        self.id2label = {0: "IGNORE", 1: "IMPORTANT"}
        print("Model loaded and ready for inference!")

    def predict(self, subject, body):
        combined_text = f"Subject: {subject} | Body: {body}"
        
        inputs = self.tokenizer(
            combined_text, 
            return_tensors="pt", 
            truncation=True, 
            max_length=512
        )
        
        with torch.no_grad():
            outputs = self.model(**inputs)
            
        logits = outputs.logits
        
        # NEW: Calculate the confidence score using Softmax
        probabilities = F.softmax(logits, dim=-1)
        
        # Get the highest score and its corresponding ID
        predicted_class_id = torch.argmax(probabilities, dim=-1).item()
        confidence_score = probabilities[0][predicted_class_id].item()
        
        label = self.id2label[predicted_class_id]
        
        # Return both!
        return label, round(confidence_score * 100, 2) 

# Test block
if __name__ == "__main__":
    ai = MailMindModel()
    label, score = ai.predict("Test Subject", "Test Body")
    print(f"Prediction: {label} (Confidence: {score}%)")