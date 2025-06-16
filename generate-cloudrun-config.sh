#!/bin/bash

# Generate cloudrun.yaml from template
# Usage: ./generate-cloudrun-config.sh

# Get project ID from gcloud config
PROJECT_ID=$(gcloud config get-value project)

if [ -z "$PROJECT_ID" ]; then
    echo "Error: No project ID found. Please run 'gcloud config set project YOUR_PROJECT_ID'"
    exit 1
fi

# Set bucket name (you can modify this if needed)
TEMP_BUCKET_NAME="sermonbot-temp"

echo "Generating cloudrun.yaml with:"
echo "  PROJECT_ID: $PROJECT_ID"
echo "  TEMP_BUCKET_NAME: $TEMP_BUCKET_NAME"

# Generate the actual cloudrun.yaml from template
sed -e "s/PROJECT_ID/$PROJECT_ID/g" \
    -e "s/TEMP_BUCKET_NAME/$TEMP_BUCKET_NAME/g" \
    cloudrun.yaml.template > cloudrun.yaml

echo "Generated cloudrun.yaml successfully!" 