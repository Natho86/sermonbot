#!/bin/bash

echo "🎵 Triggering sermonbot job..."
gcloud scheduler jobs run sermonbot-job --location=europe-west2

echo "✅ Job triggered! Check logs at:"
echo "https://console.cloud.google.com/run/detail/europe-west2/sermonbot/logs" 