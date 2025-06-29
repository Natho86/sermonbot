#!/bin/bash

echo "🎵 Triggering sermonbot job..."
gcloud scheduler jobs run sermonbot-job --location=europe-west2

echo "📊 Starting log stream in 3 seconds..."
echo "   (Press Ctrl+C to stop streaming)"
sleep 3

echo "=== STREAMING LOGS ==="
gcloud beta logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=sermonbot" \
  --format="value(timestamp,severity,textPayload)" 