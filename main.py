import os
import time
import base64
from google.cloud import secretmanager
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
            '--log-severity=ERROR',
            '--foreground=false',
            'sermonbot-temp',
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

def upload_to_wordpress(file_path, filename, original_wav_name=None):
    """Upload file to WordPress media library with metadata."""
    logger.info(f"Uploading {filename} to WordPress...")
    
    try:
        # Get WordPress credentials
        wp_username = os.environ.get('WORDPRESS_USERNAME', 'sermonbot')
        wp_password = get_secret(WORDPRESS_APP_PASSWORD_SECRET)
        
        # Create proper Basic auth header
        credentials = f"{wp_username}:{wp_password}"
        encoded_credentials = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')
        
        headers = {
            'Authorization': f'Basic {encoded_credentials}'
        }
        
        # Check file size
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        logger.info(f"WordPress upload: {filename} ({file_size_mb:.1f}MB)")
        
        # Extract metadata from filename
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
            'file_size_mb': str(round(file_size_mb, 2)),
            'processing_date': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        
        if original_wav_name:
            custom_meta['original_filename'] = original_wav_name
        
        with open(file_path, 'rb') as f:
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
            
            # Set timeout based on file size
            timeout = max(300, int(file_size_mb * 2))
            
            # Try with SSL verification first, then without if it fails
            try:
                response = requests.post(
                    WORDPRESS_API_URL, 
                    headers=headers, 
                    files=files,
                    data=data,
                    timeout=timeout,
                    verify=True
                )
            except requests.exceptions.SSLError:
                logger.warning("SSL verification failed, retrying without verification")
                response = requests.post(
                    WORDPRESS_API_URL, 
                    headers=headers, 
                    files=files,
                    data=data,
                    timeout=timeout,
                    verify=False
                )
            
            response.raise_for_status()
            result = response.json()
            logger.info(f"Successfully uploaded to WordPress - Media ID: {result.get('id')}")
            return result
            
    except requests.exceptions.Timeout:
        logger.error(f"WordPress upload timed out for {filename}")
        raise
    except requests.exceptions.RequestException as e:
        logger.error(f"WordPress upload failed for {filename}: {str(e)}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response status: {e.response.status_code}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error uploading {filename} to WordPress: {str(e)}")
        raise

def process_sermon_files():
    """Process sermon files from Google Drive"""
    logger.info("Starting sermon file processing...")
    
    try:
        # Initialize Drive service
        drive_service = initialize_drive_service()
        
        # Get folder IDs from Secret Manager
        raw_folder_id = get_secret(RAW_FOLDER_ID_SECRET)
        processed_folder_id = get_secret(PROCESSED_FOLDER_ID_SECRET)
        archive_folder_id = get_secret(ARCHIVE_FOLDER_ID_SECRET)
        
        # Ensure temp directory exists
        os.makedirs('/mnt/gcs/temp', exist_ok=True)
        
        # Search for WAV files in the RAW folder
        logger.info("Searching for WAV files in RAW folder...")
        wav_query = f"'{raw_folder_id}' in parents and (name contains '.wav' or name contains '.WAV') and trashed=false"
        
        results = drive_service.files().list(
            q=wav_query,
            fields="files(id, name, mimeType, size, createdTime)",
            orderBy="createdTime desc",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        
        files = results.get('files', [])
        logger.info(f"Found {len(files)} WAV files")
        
        if not files:
            logger.warning("No WAV files found in RAW folder")
            return
        
        # Check for duplicates in ARCHIVE folder
        logger.info("Checking for duplicates...")
        archive_query = f"'{archive_folder_id}' in parents and trashed=false"
        archive_results = drive_service.files().list(
            q=archive_query,
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        
        archived_files = {f['name'].lower() for f in archive_results.get('files', [])}
        
        # Filter out duplicates
        files_to_process = []
        skipped_count = 0
        
        for file in files:
            if file['name'].lower() in archived_files:
                logger.info(f"SKIPPING {file['name']} - already processed")
                skipped_count += 1
            else:
                files_to_process.append(file)
        
        logger.info(f"Processing {len(files_to_process)} new files, {skipped_count} duplicates skipped")
        
        if not files_to_process:
            logger.info("No new files to process")
            return
        
        # Initialize counters
        processed_count = 0
        failed_count = 0
        wordpress_success_count = 0
            
        for file in files_to_process:
            try:
                file_size_gb = int(file.get('size', 0)) / (1024**3)
                logger.info(f"Processing: {file['name']} ({file_size_gb:.1f}GB)")
                
                # Download WAV file to temp storage
                wav_path = f"/mnt/gcs/temp/{file['name']}"
                m4a_path = wav_path.replace('.wav', '.m4a').replace('.WAV', '.m4a')
                
                # Download file
                request = drive_service.files().get_media(
                    fileId=file['id'],
                    supportsAllDrives=True
                )
                with open(wav_path, 'wb') as f:
                    downloader = MediaIoBaseDownload(f, request)
                    done = False
                    while not done:
                        status, done = downloader.next_chunk()
                        if status.progress() * 100 % 20 == 0:  # Log every 20%
                            logger.info(f"Download {int(status.progress() * 100)}%")
                
                # Step 1: Analyze max volume using ffmpeg volumedetect
                analyze_cmd = [
                    'ffmpeg', '-i', wav_path,
                    '-af', 'volumedetect',
                    '-f', 'null', '/dev/null'
                ]
                analyze_result = subprocess.run(analyze_cmd, capture_output=True, text=True)
                max_volume = None
                for line in analyze_result.stderr.splitlines():
                    if 'max_volume:' in line:
                        try:
                            max_volume = float(line.split('max_volume:')[1].split('dB')[0].strip())
                        except Exception:
                            pass
                if max_volume is None:
                    raise RuntimeError(f"Could not detect max volume for {wav_path}")
                target_db = -5.0  # Target normalization level in dBFS
                gain = target_db - max_volume
                logger.info(f"Detected max volume: {max_volume} dB, applying gain: {gain} dB")

                # Step 2: Convert to M4A with normalization
                ffmpeg_cmd = [
                    'ffmpeg', '-i', wav_path,
                    '-af', f'volume={gain}dB',
                    '-c:a', 'aac', '-b:a', '192k',
                    '-loglevel', 'warning',
                    m4a_path
                ]
                result = subprocess.run(ffmpeg_cmd, check=True, capture_output=True, text=True)
                
                if result.stderr.strip():
                    logger.warning(f"FFmpeg warnings: {result.stderr.strip()}")
                
                # Wait for filesystem sync and verify file
                time.sleep(2)
                
                # Verify M4A file was created
                max_retries = 3
                for retry in range(max_retries):
                    if os.path.exists(m4a_path) and os.path.getsize(m4a_path) > 0:
                        break
                    if retry < max_retries - 1:
                        logger.warning(f"M4A file not ready, retrying... ({retry + 1}/{max_retries})")
                        time.sleep(3)
                else:
                    raise FileNotFoundError(f"M4A file was not created: {m4a_path}")
                
                logger.info("Conversion completed successfully")
                
                # Upload M4A to WordPress
                wordpress_result = None
                try:
                    wordpress_result = upload_to_wordpress(
                        m4a_path, 
                        os.path.basename(m4a_path),
                        original_wav_name=file['name']
                    )
                except Exception as e:
                    logger.error(f"WordPress upload failed: {str(e)}")
                
                # Upload M4A to processed folder
                logger.info("Uploading to PROCESSED folder...")
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
                
                # Move original WAV to archive
                logger.info("Moving to ARCHIVE folder...")
                drive_service.files().update(
                    fileId=file['id'],
                    addParents=archive_folder_id,
                    removeParents=raw_folder_id,
                    fields='id, parents',
                    supportsAllDrives=True
                ).execute()
                
                # Update counters
                processed_count += 1
                if wordpress_result:
                    wordpress_success_count += 1
                
                wp_status = "✓" if wordpress_result else "✗"
                logger.info(f"✓ Processed {file['name']} - WordPress: {wp_status}")
                
            except Exception as e:
                logger.error(f"✗ Failed to process {file['name']}: {str(e)}")
                failed_count += 1
            finally:
                # Clean up temp files
                try:
                    if os.path.exists(wav_path):
                        os.remove(wav_path)
                    if os.path.exists(m4a_path):
                        os.remove(m4a_path)
                except Exception as e:
                    logger.error(f"Error cleaning up temp files: {str(e)}")
        
        # Final processing summary
        logger.info("=== PROCESSING COMPLETE ===")
        logger.info(f"Files found: {len(files)}")
        logger.info(f"Duplicates skipped: {skipped_count}")
        logger.info(f"Successfully processed: {processed_count}")
        logger.info(f"Failed: {failed_count}")
        logger.info(f"WordPress uploads: {wordpress_success_count}/{processed_count}")
        
        if len(files_to_process) > 0:
            success_rate = (processed_count / len(files_to_process)) * 100
            logger.info(f"Success rate: {success_rate:.1f}%")
                
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
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080))) 