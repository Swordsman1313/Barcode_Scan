FROM python:3.11-slim

# Install required system dependencies for image processing and C++ builds
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Create photos and data directory
RUN mkdir -p photos

# Run the Telegram bot
CMD ["python", "bot.py"]
