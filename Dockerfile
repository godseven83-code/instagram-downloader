# Use a lightweight Python image
FROM python:3.11-slim

# Install ffmpeg and system dependencies
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy all project files
COPY . /app

# Install Python dependencies
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Expose the Flask port
ENV PORT=5000
EXPOSE 5000

# Start your Flask app with gunicorn
CMD ["gunicorn", "main_web:app", "--bind", "0.0.0.0:5000"]
