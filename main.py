import os
import tempfile
import time
import base64
from pathlib import Path
from google.cloud import secretmanager
from google.cloud import storage
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
import requests
import json
import google.auth
import logging
import subprocess
import traceback
from flask import Flask, request, jsonify
from functools import wraps
import atexit

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Get the project ID from the environment or default credentials
PROJECT_ID = (
    os.environ.get("GOOGLE_CLOUD_PROJECT") or
    os.environ.get("GCLOUD_PROJECT") or
    google.auth.default()[1]
)

# Get the temp bucket name from environment
TEMP_BUCKET_NAME = os.environ["TEMP_BUCKET_NAME"]

# Security settings
API_KEY_SECRET = os.environ["API_KEY_SECRET"]

# Secret names from environment variables
SERVICE_ACCOUNT_SECRET = os.environ["SERVICE_ACCOUNT_SECRET"]
WORDPRESS_APP_PASSWORD_SECRET = os.environ["WORDPRESS_APP_PASSWORD_SECRET"]
RAW_FOLDER_ID_SECRET = os.environ["RAW_FOLDER_ID_SECRET"]
PROCESSED_FOLDER_ID_SECRET = os.environ["PROCESSED_FOLDER_ID_SECRET"]
ARCHIVE_FOLDER_ID_SECRET = os.environ["ARCHIVE_FOLDER_ID_SECRET"]

# WordPress API URL
WORDPRESS_API_URL = os.environ["WORDPRESS_API_URL"]

# Google Drive API scopes
SCOPES = ['https://www.googleapis.com/auth/drive']

def unmount_gcs_bucket():
    """Unmount the GCS bucket."""
    try:
        subprocess.run(['fusermount', '-u', '/mnt/gcs'], check=True)
        logger.info("Successfully unmounted GCS bucket")
    except Exception as e:
        logger.error(f"Failed to unmount GCS bucket: {str(e)}")

# Mount GCS bucket at startup
def mount_gcs_bucket():
    """Mount the GCS bucket for temporary file storage."""
    try:
        # Create mount point if it doesn't exist
        os.makedirs('/mnt/gcs', exist_ok=True)
        
        # Mount the bucket
        subprocess.run([
            'gcsfuse',
            '--implicit-dirs',
            '--file-mode=0777',
            '--dir-mode=0777',
            'llec-sermonbot-temp',
            '/mnt/gcs'
        ], check=True)
        logger.info("Successfully mounted GCS bucket")
    except Exception as e:
        logger.error(f"Failed to mount GCS bucket: {str(e)}")
        raise

# Mount the bucket before starting the application
mount_gcs_bucket()

# Register cleanup function
atexit.register(unmount_gcs_bucket)

def get_secret(secret_id):
    """Retrieve a secret from Secret Manager."""
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")

def initialize_drive_service():
    """Create an authenticated Google Drive service."""
    service_account_info = json.loads(get_secret(SERVICE_ACCOUNT_SECRET))
    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES,
        subject=os.environ.get('IMPERSONATE_EMAIL')
    )
    service = build('drive', 'v3', credentials=credentials)
    
    # Log the service account email being used
    logger.info(f"Using service account: {credentials.service_account_email}")
    if os.environ.get('IMPERSONATE_EMAIL'):
        logger.info(f"Impersonating user: {os.environ.get('IMPERSONATE_EMAIL')}")
    
    return service

def download_gcs_to_local(gcs_bucket, gcs_blob_name, local_path):
    """Download a file from GCS to local disk for processing (conversion)."""
    bucket = storage.Client().bucket(gcs_bucket)
    blob = bucket.blob(gcs_blob_name)
    blob.download_to_filename(local_path)

def upload_local_to_gcs(local_path, gcs_bucket, gcs_blob_name):
    """Upload a local file to GCS."""
    bucket = storage.Client().bucket(gcs_bucket)
    blob = bucket.blob(gcs_blob_name)
    blob.upload_from_filename(local_path)
    return f"gs://{gcs_bucket}/{gcs_blob_name}"

def delete_gcs_blob(gcs_bucket, gcs_blob_name):
    bucket = storage.Client().bucket(gcs_bucket)
    blob = bucket.blob(gcs_blob_name)
    blob.delete()

def convert_wav_to_m4a_gcs(gcs_bucket, wav_blob_name, m4a_blob_name):
    """Download WAV from GCS, convert to M4A, upload back to GCS."""
    with tempfile.TemporaryDirectory() as temp_dir:
        wav_local = os.path.join(temp_dir, 'input.wav')
        m4a_local = os.path.join(temp_dir, 'output.m4a')
        download_gcs_to_local(gcs_bucket, wav_blob_name, wav_local)
        audio = AudioSegment.from_wav(wav_local)
        audio.export(m4a_local, format="m4a")
        upload_local_to_gcs(m4a_local, gcs_bucket, m4a_blob_name)

def upload_to_wordpress(file_path, filename, original_wav_name=None):
    """Upload file to WordPress media library with metadata."""
    logger.info(f"Uploading {filename} to WordPress...")
    
    try:
        # Get WordPress credentials
        wp_username = os.environ.get('WORDPRESS_USERNAME', 'sermonbot')
        wp_password = get_secret(WORDPRESS_APP_PASSWORD_SECRET)
        
        # Create proper Basic auth header (base64 encode username:password)
        credentials = f"{wp_username}:{wp_password}"
        encoded_credentials = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')
        
        headers = {
            'Authorization': f'Basic {encoded_credentials}',
            'Content-Type': 'multipart/form-data'
        }
        
        logger.info(f"WordPress authentication: username={wp_username}, password_length={len(wp_password)}")
        logger.info(f"WordPress API URL: {WORDPRESS_API_URL}")
        
        # Validate the WordPress API URL format
        if not WORDPRESS_API_URL.startswith(('http://', 'https://')):
            logger.error(f"Invalid WordPress API URL format: {WORDPRESS_API_URL}")
            raise ValueError(f"WordPress API URL must start with http:// or https://")
        
        # Check if file exists and get size
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        logger.info(f"WordPress upload: {filename} ({file_size_mb:.1f}MB)")
        
        # WordPress.com has a 2GB limit, but let's warn for large files
        if file_size_mb > 100:
            logger.warning(f"Large file upload to WordPress: {file_size_mb:.1f}MB")
        
        # Extract date and title from filename (assuming format: YYYY-MM-DD_Title.m4a)
        base_name = os.path.splitext(filename)[0]
        parts = base_name.split('_', 1)
        
        if len(parts) == 2:
            date_part, title_part = parts
            title = title_part.replace('-', ' ').replace('_', ' ')
            description = f"Sermon recording from {date_part}"
        else:
            title = base_name.replace('-', ' ').replace('_', ' ')
            description = "Sermon recording"
        
        # Prepare metadata
        metadata = {
            'title': title,
            'caption': f"Audio sermon: {title}",
            'description': description,
            'alt_text': f"Sermon audio: {title}",
        }
        
        # Add custom metadata
        custom_meta = {
            'sermon_bot_processed': 'true',
            'original_format': 'wav',
            'converted_format': 'm4a',
            'file_size_mb': str(round(file_size_mb, 2)),
            'processing_date': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        
        if original_wav_name:
            custom_meta['original_filename'] = original_wav_name
        
        logger.info(f"WordPress metadata - Title: {title}, Description: {description}")
        
        with open(file_path, 'rb') as f:
            # Prepare the multipart form data
            files = {
                'file': (filename, f, 'audio/mp4')
            }
            
            # Add metadata as form data
            data = {}
            for key, value in metadata.items():
                data[key] = value
            
            # Add custom meta fields
            for key, value in custom_meta.items():
                data[f'meta[{key}]'] = value
            
            # Remove Content-Type header to let requests set it automatically for multipart
            headers_without_content_type = {k: v for k, v in headers.items() if k != 'Content-Type'}
            
            # Make the request with timeout for large files
            timeout = max(300, int(file_size_mb * 2))  # 2 seconds per MB, minimum 5 minutes
            logger.info(f"WordPress upload timeout set to {timeout} seconds")
            logger.info(f"WordPress API URL: {WORDPRESS_API_URL}")
            
            # Try with SSL verification first, then without if it fails
            try:
                response = requests.post(
                    WORDPRESS_API_URL, 
                    headers=headers_without_content_type, 
                    files=files,
                    data=data,
                    timeout=timeout,
                    verify=True  # Verify SSL certificates
                )
            except requests.exceptions.SSLError as ssl_error:
                logger.warning(f"SSL verification failed, trying without verification: {ssl_error}")
                response = requests.post(
                    WORDPRESS_API_URL, 
                    headers=headers_without_content_type, 
                    files=files,
                    data=data,
                    timeout=timeout,
                    verify=False  # Disable SSL verification as fallback
                )
            response.raise_for_status()
            
            result = response.json()
            logger.info(f"Successfully uploaded to WordPress - Media ID: {result.get('id', 'unknown')}")
            logger.info(f"WordPress URL: {result.get('source_url', 'unknown')}")
            logger.info(f"WordPress title: {result.get('title', {}).get('rendered', 'unknown')}")
            
            return result
            
    except requests.exceptions.Timeout:
        logger.error(f"WordPress upload timed out for {filename}")
        raise
    except requests.exceptions.RequestException as e:
        logger.error(f"WordPress upload failed for {filename}: {str(e)}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"WordPress response status: {e.response.status_code}")
            logger.error(f"WordPress response text: {e.response.text}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error uploading {filename} to WordPress: {str(e)}")
        raise

def process_sermon_files():
    """Process WAV files from the RAW folder."""
    try:
        # Initialize Drive service
        drive_service = initialize_drive_service()
        
        # Get folder IDs from Secret Manager
        raw_folder_id = get_secret(RAW_FOLDER_ID_SECRET)
        processed_folder_id = get_secret(PROCESSED_FOLDER_ID_SECRET)
        archive_folder_id = get_secret(ARCHIVE_FOLDER_ID_SECRET)
        
        logger.info(f"Using folder IDs - Raw: {raw_folder_id}, Processed: {processed_folder_id}, Archive: {archive_folder_id}")
        
        # Create temp directory if it doesn't exist
        os.makedirs('/mnt/gcs/temp', exist_ok=True)
        
        # Search for WAV files in the RAW folder (current versions only)
        logger.info("Searching for current WAV files in RAW folder...")
        try:
            wav_query = f"'{raw_folder_id}' in parents and (name contains '.wav' or name contains '.WAV') and trashed=false"
            logger.info(f"Using query: {wav_query}")
            
            results = drive_service.files().list(
                q=wav_query,
                fields="files(id, name, mimeType, size, createdTime)",
                orderBy="createdTime desc",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            ).execute()
            
            files = results.get('files', [])
            logger.info(f"Found {len(files)} WAV files:")
            for f in files:
                logger.info(f"  - {f['name']} (ID: {f['id']}, Type: {f.get('mimeType', 'unknown')}, Size: {f.get('size', 'unknown')})")
        except Exception as e:
            logger.error(f"Error listing WAV files: {str(e)}")
            logger.error(traceback.format_exc())
            raise
        
        if not files:
            logger.warning("No WAV files found in RAW folder")
            return
        
        # Initialize counters
        processed_count = 0
        failed_count = 0
        wordpress_success_count = 0
            
        for file in files:
            try:
                # Log file info
                file_size_gb = int(file.get('size', 0)) / (1024**3)
                logger.info(f"Processing file: {file['name']} ({file_size_gb:.1f}GB)")
                
                if file_size_gb > 1.0:
                    estimated_minutes = file_size_gb * 5  # Rough estimate: 5 min per GB
                    logger.info(f"Large file detected - estimated processing time: {estimated_minutes:.1f} minutes")
                
                # Download WAV file to temp storage
                wav_path = f"/mnt/gcs/temp/{file['name']}"
                m4a_path = wav_path.replace('.wav', '.m4a').replace('.WAV', '.m4a')
                
                try:
                    request = drive_service.files().get_media(
                        fileId=file['id'],
                        supportsAllDrives=True
                    )
                    with open(wav_path, 'wb') as f:
                        downloader = MediaIoBaseDownload(f, request)
                        done = False
                        while not done:
                            status, done = downloader.next_chunk()
                            logger.info(f"Download {int(status.progress() * 100)}%")
                    
                    # Convert to M4A
                    logger.info(f"Starting conversion of {wav_path} to {m4a_path}")
                    try:
                        # Check if input file exists and has content
                        if not os.path.exists(wav_path):
                            raise FileNotFoundError(f"WAV file not found: {wav_path}")
                        
                        file_size = os.path.getsize(wav_path)
                        logger.info(f"WAV file size: {file_size} bytes")
                        
                        if file_size == 0:
                            raise ValueError(f"WAV file is empty: {wav_path}")
                        
                        # Run FFmpeg conversion (without verbose progress to reduce log noise)
                        ffmpeg_cmd = [
                            'ffmpeg', '-i', wav_path,
                            '-c:a', 'aac', '-b:a', '192k',
                            '-loglevel', 'warning',  # Only show warnings and errors
                            m4a_path
                        ]
                        
                        result = subprocess.run(ffmpeg_cmd, check=True, capture_output=True, text=True)
                        logger.info(f"FFmpeg conversion completed successfully")
                        
                        # Only log stderr if there are warnings/errors
                        if result.stderr.strip():
                            logger.info(f"FFmpeg warnings/errors: {result.stderr.strip()}")
                        
                        # Wait a moment for GCSFuse to sync the file
                        logger.info("Waiting for file system sync...")
                        time.sleep(2)
                        
                        # Check if output file was created with retry logic
                        max_retries = 5
                        retry_count = 0
                        while retry_count < max_retries:
                            if os.path.exists(m4a_path):
                                try:
                                    output_size = os.path.getsize(m4a_path)
                                    if output_size > 0:
                                        logger.info(f"M4A file size: {output_size} bytes")
                                        break
                                    else:
                                        logger.warning(f"M4A file exists but is empty, retrying... ({retry_count + 1}/{max_retries})")
                                except OSError as e:
                                    logger.warning(f"Error checking M4A file size, retrying... ({retry_count + 1}/{max_retries}): {e}")
                            else:
                                logger.warning(f"M4A file not found, retrying... ({retry_count + 1}/{max_retries})")
                            
                            retry_count += 1
                            if retry_count < max_retries:
                                time.sleep(3)  # Wait longer between retries
                        
                        if retry_count >= max_retries:
                            raise FileNotFoundError(f"M4A file was not created or is invalid after {max_retries} retries: {m4a_path}")
                        
                        logger.info(f"M4A file successfully created and verified")
                        
                    except subprocess.CalledProcessError as e:
                        logger.error(f"FFmpeg conversion failed with return code {e.returncode}")
                        logger.error(f"FFmpeg stderr: {e.stderr}")
                        logger.error(f"FFmpeg stdout: {e.stdout}")
                        raise
                    except Exception as e:
                        logger.error(f"Conversion error: {str(e)}")
                        raise
                    
                    # Upload M4A to WordPress
                    wordpress_result = None
                    try:
                        # Verify file is still accessible before WordPress upload
                        if not os.path.exists(m4a_path):
                            raise FileNotFoundError(f"M4A file disappeared before WordPress upload: {m4a_path}")
                        
                        # Check file size one more time
                        file_size_check = os.path.getsize(m4a_path)
                        if file_size_check == 0:
                            raise ValueError(f"M4A file is empty before WordPress upload: {m4a_path}")
                        
                        logger.info(f"Verified M4A file before WordPress upload: {file_size_check} bytes")
                        
                        wordpress_result = upload_to_wordpress(
                            m4a_path, 
                            os.path.basename(m4a_path),
                            original_wav_name=file['name']
                        )
                        logger.info(f"WordPress upload successful for {file['name']}")
                    except Exception as e:
                        logger.error(f"WordPress upload failed for {file['name']}: {str(e)}")
                        logger.error(f"Continuing with Drive operations...")
                        # Don't raise - we still want to archive the files even if WordPress fails
                    
                    # Upload M4A to processed folder
                    logger.info(f"Uploading M4A file to PROCESSED folder...")
                    file_metadata = {
                        'name': os.path.basename(m4a_path),
                        'parents': [processed_folder_id]
                    }
                    media = MediaFileUpload(m4a_path, mimetype='audio/mp4', resumable=True)
                    uploaded_file = drive_service.files().create(
                        body=file_metadata,
                        media_body=media,
                        fields='id',
                        supportsAllDrives=True
                    ).execute()
                    logger.info(f"Successfully uploaded M4A file with ID: {uploaded_file['id']}")
                    
                    # Move original WAV to archive
                    logger.info(f"Moving original WAV file to ARCHIVE folder...")
                    drive_service.files().update(
                        fileId=file['id'],
                        addParents=archive_folder_id,
                        removeParents=raw_folder_id,
                        fields='id, parents',
                        supportsAllDrives=True
                    ).execute()
                    logger.info(f"Successfully moved WAV file to ARCHIVE folder")
                    
                    # Final success message with WordPress status
                    wp_status = "✓" if wordpress_result else "✗"
                    logger.info(f"Successfully processed {file['name']} - WordPress: {wp_status}")
                    
                    # Update counters
                    processed_count += 1
                    if wordpress_result:
                        wordpress_success_count += 1
                    
                except Exception as e:
                    logger.error(f"Error processing {file['name']}: {str(e)}")
                    logger.error(traceback.format_exc())
                    failed_count += 1
                    continue
                finally:
                    # Clean up temp files
                    try:
                        if os.path.exists(wav_path):
                            os.remove(wav_path)
                        if os.path.exists(m4a_path):
                            os.remove(m4a_path)
                    except Exception as e:
                        logger.error(f"Error cleaning up temp files: {str(e)}")
                
            except Exception as e:
                logger.error(f"Error processing {file['name']}: {str(e)}")
                logger.error(traceback.format_exc())
                continue
        
        # Final processing summary
        total_files = len(files)
        logger.info(f"=== PROCESSING COMPLETE ===")
        logger.info(f"Total files found: {total_files}")
        logger.info(f"Successfully processed: {processed_count}")
        logger.info(f"Failed: {failed_count}")
        logger.info(f"WordPress uploads successful: {wordpress_success_count}/{processed_count}")
        
        if processed_count > 0:
            success_rate = (processed_count / total_files) * 100
            wp_success_rate = (wordpress_success_count / processed_count) * 100
            logger.info(f"Overall success rate: {success_rate:.1f}%")
            logger.info(f"WordPress success rate: {wp_success_rate:.1f}%")
                
    except Exception as e:
        logger.error(f"Error in process_sermon_files: {str(e)}")
        logger.error(traceback.format_exc())
        raise

def require_api_key(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        api_key = request.headers.get('X-API-Key')
        if not api_key:
            return jsonify({"error": "API key is required"}), 401
        
        # Get the valid API key from Secret Manager
        valid_api_key = get_secret(API_KEY_SECRET)
        
        if api_key != valid_api_key:
            return jsonify({"error": "Invalid API key"}), 401
        
        return f(*args, **kwargs)
    return decorated_function

@app.route('/process', methods=['POST'])
@require_api_key
def process_sermons():
    """Process sermons endpoint."""
    try:
        process_sermon_files()
        return jsonify({"status": "success"}), 200
    except Exception as e:
        logger.error(f"Error in process_sermons endpoint: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080))) 