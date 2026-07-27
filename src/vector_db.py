import os
import chromadb
from chromadb.config import Settings
import uuid

# Define where ChromaDB should store its data locally
DB_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'chroma_db')

# Initialize the ChromaDB client
client = chromadb.PersistentClient(path=DB_DIR, settings=Settings(allow_reset=True))

# Get or create a collection for our emails
# We'll use Chroma's default embedding function (all-MiniLM-L6-v2) for simplicity, 
# or we can pass custom embeddings later. 
# For now, default sentence-transformers embedding is fast and robust for text.
collection = client.get_or_create_collection(
    name="mailmind_emails",
    metadata={"hnsw:space": "cosine"} # Use cosine similarity for better semantic search
)

def add_email_to_vector_db(email_id, subject, body, label):
    """
    Adds an email and its user-defined label to the Vector DB.
    """
    text_content = f"Subject: {subject}\nBody: {body}"
    
    # We use the email_id as the unique ID in ChromaDB
    # If the email_id is already there, we update it.
    collection.upsert(
        documents=[text_content],
        metadatas=[{"label": label, "subject": subject}],
        ids=[str(email_id)]
    )
    print(f"Added email {email_id} to Vector DB as {label}")

def search_similar_emails(subject, body, k=3):
    """
    Searches the Vector DB for the k most similar emails.
    Returns a list of dicts with their text and label.
    """
    text_content = f"Subject: {subject}\nBody: {body}"
    
    results = collection.query(
        query_texts=[text_content],
        n_results=k
    )
    
    similar_emails = []
    if results and results['documents'] and len(results['documents']) > 0:
        for i in range(len(results['documents'][0])):
            doc_text = results['documents'][0][i]
            metadata = results['metadatas'][0][i]
            label = metadata.get("label", "UNKNOWN")
            distance = results['distances'][0][i] if 'distances' in results else 0.0
            
            similar_emails.append({
                "text": doc_text,
                "label": label,
                "distance": distance
            })
            
    return similar_emails

def get_knn_prediction(subject, body, k=5):
    """
    Performs k-NN classification based on Vector DB similarity.
    If the DB is empty, returns None.
    """
    similar = search_similar_emails(subject, body, k=k)
    
    if not similar:
        return None
        
    # Count the votes
    votes = {}
    for item in similar:
        label = item["label"]
        votes[label] = votes.get(label, 0) + 1
        
    # Get the majority vote
    best_label = max(votes, key=votes.get)
    
    # Calculate confidence based on majority count / total
    confidence = votes[best_label] / len(similar)
    
    return best_label, round(confidence * 100, 2)
