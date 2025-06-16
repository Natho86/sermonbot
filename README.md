# Sermon Processing Automation

This project automates the processing of sermon audio files by:
1. Converting WAV files to M4A format using FFmpeg
2. Uploading the converted files to WordPress with metadata
3. Managing file organization in Google Drive
4. Using GCS bucket mounting for efficient large file processing

## 🎯 Current Status: WORKING VERSION

This version successfully processes large sermon files (tested with 979MB files) with:
- ✅ WordPress upload integration with proper authentication
- ✅ GCSFuse filesystem mounting for efficient file handling
- ✅ Robust error handling for filesystem sync issues
- ✅ SSL fallback for WordPress connections
- ✅ Comprehensive logging and progress tracking
- ✅ Custom metadata fields for WordPress media
- ✅ 60-minute timeout for large file processing (successfully tested with multiple files)
- ✅ Duplicate detection to avoid reprocessing archived files

---

## Prerequisites

- **Google Cloud Project** with the following APIs enabled:
  - Cloud Run API
  - Secret Manager API
  - Cloud Storage API
  - Drive API
- **Google Workspace account** with appropriate permissions
- **WordPress site** with REST API access and application password
- **Google Secret Manager**: You must create the following secrets and store their values (not the names) in Secret Manager **before deploying**:
  - `sermonbot-gcp-sa-account`: JSON key file for the service account
  - `sermonbot-wp-app-password`: WordPress application password
  - `raw-folder-id`: Google Drive ID for the RAW folder
  - `processed-folder-id`: Google Drive ID for the PROCESSED folder
  - `archive-folder-id`: Google Drive ID for the ARCHIVE folder
  - `sermonbot-api-key`: A secure random string to use as the API key
    ```bash
    # Generate a secure random API key (32 bytes, base64 encoded)
    API_KEY=$(openssl rand -base64 32)

    # Create the secret
    gcloud secrets create sermonbot-api-key --replication-policy="automatic"

    # Add the API key to the secret
    echo -n "$API_KEY" | gcloud secrets versions add sermonbot-api-key --data-file=-

    # Save the API key somewhere safe - you'll need it for the Cloud Scheduler job
    echo "Your API key is: $API_KEY"
    ```

---

## WordPress Setup

### Application Password Creation
1. Go to your WordPress admin dashboard
2. Navigate to Users → Profile
3. Scroll down to "Application Passwords"
4. Create a new application password named "SermonBot"
5. Copy the generated password and store it in Secret Manager

### WordPress User Requirements
- Username: `sermonbot` (or update `WORDPRESS_USERNAME` environment variable)
- User must have `upload_files` capability (Author role or higher)
- Application password must be properly configured

---

## Environment Variables File (`.env-vars.yaml`)

**Important:** The environment variables below are the **names of the secrets** in Google Secret Manager, **not the secret values themselves**.

Example `.env-vars.yaml`:

```yaml
TEMP_BUCKET_NAME: "sermonbot-temp"
SERVICE_ACCOUNT_SECRET: "sermonbot-gcp-sa-account"
WORDPRESS_APP_PASSWORD_SECRET: "sermonbot-wp-app-password"
WORDPRESS_USERNAME: "sermonbot"
RAW_FOLDER_ID_SECRET: "raw-folder-id"
PROCESSED_FOLDER_ID_SECRET: "processed-folder-id"
ARCHIVE_FOLDER_ID_SECRET: "archive-folder-id"
API_KEY_SECRET: "sermonbot-api-key"
WORDPRESS_API_URL: "https://llec.org.uk/wp-json/wp/v2/media"
IMPERSONATE_EMAIL: "sermon.bot@llec.org.uk"
```

- **Do not commit this file to version control.**
- Add `.env-vars.yaml` to your `.gitignore`.

---

## Cloud Storage Bucket for Temp Files

**Large files require a Cloud Storage bucket for temporary storage with GCSFuse mounting.**

### Create the bucket with proper configuration:

```bash
# Set your bucket name (must be globally unique)
BUCKET_NAME="sermonbot-temp"

# Create the bucket in europe-west2
gsutil mb -l europe-west2 gs://$BUCKET_NAME

# Set a retention policy of 7 days (604800 seconds)
gsutil retention set 604800s gs://$BUCKET_NAME

# Enable object lifecycle management to delete files after 7 days
cat > lifecycle.json <<EOF
{
  "rule": [
    {
      "action": {"type": "Delete"},
      "condition": {"age": 7}
    }
  ]
}
EOF

gsutil lifecycle set lifecycle.json gs://$BUCKET_NAME
rm lifecycle.json
```

**Grant your Cloud Run service account access to the bucket:**
```bash
# Replace with your Cloud Run service account email
SERVICE_ACCOUNT=your-service-account@your-project.iam.gserviceaccount.com

gsutil iam ch serviceAccount:$SERVICE_ACCOUNT:objectAdmin gs://$BUCKET_NAME
```

---

## Deployment Configuration

### Cloud Run Configuration Template

The `cloudrun.yaml.template` file contains placeholders for project-specific values. Generate the actual deployment file:

```bash
# Make the script executable
chmod +x generate-cloudrun-config.sh

# Generate cloudrun.yaml from template
./generate-cloudrun-config.sh
```

This creates `cloudrun.yaml` with your project ID and bucket name.

### Build and Deploy

```bash
# Set your project ID
export PROJECT_ID=$(gcloud config get-value project)

# Build the container image
gcloud builds submit --tag gcr.io/$PROJECT_ID/sermonbot

# Deploy to Cloud Run with GCSFuse support
gcloud run services replace cloudrun.yaml --region europe-west2
```

### Alternative Manual Deployment

```bash
PROJECT_ID=$(gcloud config get-value project)
GCP_SVC_ACCOUNT="your-service-account@$PROJECT_ID.iam.gserviceaccount.com"

gcloud run deploy sermonbot \
  --image gcr.io/$PROJECT_ID/sermonbot \
  --region europe-west2 \
  --platform managed \
  --env-vars-file .env-vars.yaml \
  --memory 2Gi \
  --cpu 1 \
  --timeout 3600 \
  --min-instances 0 \
  --max-instances 10 \
  --service-account=$GCP_SVC_ACCOUNT \
  --execution-environment gen2 \
  --no-cpu-throttling

# Grant the service account permission to invoke the Cloud Run service
gcloud run services add-iam-policy-binding sermonbot \
  --member=serviceAccount:$GCP_SVC_ACCOUNT \
  --role=roles/run.invoker --region=europe-west2
```

**Note**: GCSFuse mounting is handled internally by the Python application, not through Cloud Run CSI volumes.

---

## Key Technical Improvements

### 1. GCSFuse Integration
- Uses Python `gcsfuse` library to mount Cloud Storage bucket as filesystem at `/mnt/gcs`
- Enables efficient processing of large files without local disk limitations
- Automatic cleanup and lifecycle management
- Mounting handled internally by the application, not through Cloud Run CSI volumes

### 2. WordPress Upload with Metadata
- Proper Basic authentication with base64 encoding
- Custom metadata fields:
  - `sermon_bot_processed`: 'true'
  - `original_format`: 'wav'
  - `converted_format`: 'm4a'
  - `file_size_mb`: File size in MB
  - `processing_date`: Processing timestamp
  - `original_filename`: Original WAV filename
- Automatic title and description extraction from filename

### 3. Robust Error Handling
- SSL fallback for WordPress connections
- Retry logic for GCSFuse filesystem sync issues
- File verification with multiple attempts
- Comprehensive logging for debugging

### 4. FFmpeg Optimization
- Reduced log verbosity (`-loglevel warning`)
- Removed progress output to reduce log noise
- Optimized AAC encoding settings

### 5. File Processing Workflow
1. Download WAV from Google Drive with progress tracking
2. Convert to M4A using FFmpeg
3. Wait for filesystem sync (2 seconds)
4. Verify file creation with retry logic (up to 5 attempts)
5. Upload to WordPress with metadata
6. Upload M4A to Google Drive PROCESSED folder
7. Move original WAV to ARCHIVE folder
8. Cleanup temporary files

---

## Scheduling

To run the job every Sunday at 15:00, create a Cloud Scheduler job:

```bash
# Get the API key from Secret Manager
API_KEY=$(gcloud secrets versions access latest --secret=sermonbot-api-key)

# Get the Cloud Run service URL
SERVICE_URL=$(gcloud run services describe sermonbot --region=europe-west2 --format='value(status.url)')

gcloud scheduler jobs create http sermonbot-job \
  --schedule "0 15 * * 0" \
  --uri "$SERVICE_URL/process" \
  --http-method POST \
  --headers "X-API-Key=$API_KEY" \
  --oidc-service-account-email=$GCP_SVC_ACCOUNT \
  --location=europe-west2 \
  --time-zone="Europe/London"
```

---

## Security Features

The service implements the following security measures:

1. **API Key Authentication**: All requests must include a valid API key in the `X-API-Key` header.
2. **Secret Management**: All sensitive credentials are stored in Secret Manager.
3. **Service Account Impersonation**: Uses dedicated service account with minimal required permissions.
4. **No Public Access**: The service is not publicly accessible without authentication.
5. **SSL/TLS**: WordPress connections use HTTPS with SSL fallback handling.

---

## Usage

1. Upload WAV files to the RAW folder in Google Drive
2. The automation will run automatically every Sunday at 15:00, or you can trigger it manually with:
   ```bash
   curl -X POST https://[CLOUD_RUN_URL]/process \
     -H "X-API-Key: your-api-key"
   ```
3. The automation will:
   - Find WAV files in the RAW Google Drive folder
   - Download to GCS temporary storage with progress logging
   - Convert WAV to M4A using FFmpeg
   - Wait for filesystem sync and verify file integrity
   - Upload M4A to WordPress with comprehensive metadata
   - Upload M4A to Google Drive PROCESSED folder
   - Move original WAV to ARCHIVE folder
   - Clean up temporary files
   - Provide processing summary with success/failure counts

---

## Troubleshooting

### Common Issues and Solutions

#### 1. WordPress Upload Errors
- **401 Unauthorized**: Check WordPress username and application password
- **SSL Errors**: The service includes SSL fallback handling
- **File Size Limits**: WordPress.com supports up to 2GB files

#### 2. GCSFuse Filesystem Issues
- **"context deadline exceeded"**: Retry logic handles temporary filesystem sync issues
- **File not found after conversion**: Wait time and verification logic addresses this
- **Empty files**: Multiple verification attempts ensure file integrity

#### 3. FFmpeg Conversion Issues
- Check input file exists and has content
- Verify FFmpeg is properly installed in container
- Review conversion logs for specific errors

#### 4. Google Drive API Issues
- Ensure service account has proper Drive permissions
- Verify folder IDs are correct
- Check `supportsAllDrives=True` for shared drives

### Logging and Monitoring
- All operations are logged with INFO level
- Error details include full stack traces
- Processing summary shows success/failure counts
- File sizes and processing times are tracked

---

## File Structure

```
.
├── main.py                      # Main application code
├── requirements.txt             # Python dependencies
├── Dockerfile                   # Container configuration
├── cloudrun.yaml.template       # Cloud Run service template
├── generate-cloudrun-config.sh  # Script to generate deployment config
├── .env-vars.yaml              # Environment variables (not committed)
├── .gcloudignore               # Files to ignore during build
├── .gitignore                  # Git ignore rules
├── lifecycle.json              # GCS bucket lifecycle policy
└── README.md                   # This file
```

### Files Not Committed to Git
- `.env-vars.yaml` - Contains environment configuration
- `cloudrun.yaml` - Generated deployment file with project-specific values

---

## Error Handling

The script includes comprehensive error handling and logging:
- Failed files are logged but don't block processing of other files
- Retry logic for filesystem operations
- SSL fallback for WordPress connections
- Detailed error messages for debugging
- Processing continues even if individual files fail

---

## Performance Characteristics

- **Memory Usage**: 2GB Cloud Run instance
- **Timeout**: 60 minutes for large file processing
- **File Size Support**: Tested with 979MB files, supports up to WordPress limits
- **Processing Speed**: Approximately 5 minutes per GB for conversion
- **Concurrent Processing**: Sequential processing to avoid memory issues