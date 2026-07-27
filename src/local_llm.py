import torch
import torch.nn.functional as F
from transformers import AutoTokenizer, AutoModelForSequenceClassification
import sys
import os

sys.path.append(os.path.dirname(__file__))
import vector_db

class MailMindModel:
    def __init__(self, model_path="models/MailMind-Final"):
        print(f"Loading local model from {model_path}...")
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(model_path)
            self.model = AutoModelForSequenceClassification.from_pretrained(model_path)
            self.model.eval()
            self.id2label = {0: "SPAM", 1: "IMPORTANT"} # Mapping 0 to SPAM instead of IGNORE
            print("Model loaded and ready for inference!")
            self.model_loaded = True
        except Exception as e:
            print(f"Warning: Could not load local model from {model_path}. Using k-NN fallback only. Error: {e}")
            self.model_loaded = False

    def predict(self, subject, body):
        # 1. Try to get prediction from Self-Healing Vector DB (k-NN)
        knn_result = vector_db.get_knn_prediction(subject, body, k=5)
        
        if knn_result:
            label, confidence = knn_result
            print(f"[Local AI] k-NN Classification: {label} ({confidence}%)")
            return label, confidence
            
        # 2. If DB is empty, fallback to the original DistilBERT model if loaded
        if self.model_loaded:
            print("[Local AI] Vector DB empty. Falling back to base DistilBERT inference...")
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
            probabilities = F.softmax(logits, dim=-1)
            
            predicted_class_id = torch.argmax(probabilities, dim=-1).item()
            confidence_score = probabilities[0][predicted_class_id].item()
            
            label = self.id2label.get(predicted_class_id, "SPAM")
            return label, round(confidence_score * 100, 2)
            
        return "SPAM", 0.0

if __name__ == "__main__":
    ai = MailMindModel()
    label, score = ai.predict("Test Subject", "Test Body")
    print(f"Prediction: {label} (Confidence: {score}%)")