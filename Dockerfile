FROM python:3.11-slim

# Install ffmpeg and gcsfuse
RUN apt-get update && apt-get install -y \
    ffmpeg \
    gnupg \
    curl \
    && echo "deb https://packages.cloud.google.com/apt gcsfuse-focal main" > /etc/apt/sources.list.d/gcsfuse.list \
    && curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | apt-key add - \
    && apt-get update \
    && apt-get install -y gcsfuse \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Create mount point
RUN mkdir -p /mnt/gcs

CMD ["python", "main.py"] 