# 1. Base Image: A lightweight version of Python 3.10
FROM python:3.10-slim

# 2. Set the working directory inside the container
WORKDIR /app

# 3. Copy the dependencies file first
COPY requirements.txt .

# 4. Install the dependencies
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy the actual application code and your trained model
COPY src/ ./src/
COPY api/ ./api/
COPY models/ ./models/

# 6. Expose the port FastAPI runs on
EXPOSE 8000

# 7. The command to boot up the API when the container starts
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]