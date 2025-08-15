import os
import re
import sys
import time
import json
import logging
import requests
import flickrapi
from datetime import datetime
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from requests.exceptions import RequestException, Timeout

# Load environment variables from .env file
load_dotenv()

# Your Flickr API key and secret
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# Download settings
DOWNLOAD_VIDEO = os.getenv("DOWNLOAD_VIDEO", "true").lower() == "true"

# Where to store the downloaded files
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "./flickr_downloads")
CACHE_DIR = "./cache"
URL_CACHE_FILE = os.path.join(CACHE_DIR, "url_cache.json")
PROGRESS_FILE = os.path.join(CACHE_DIR, "progress.json")
LOG_FILE = os.path.join(CACHE_DIR, "flickr_downloader.log")

# Max simultaneous downloads
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 8))

# Minimum seconds between Flickr API calls to respect rate limits
API_CALL_DELAY = float(os.getenv("API_CALL_DELAY", 1.1))

# Retry/backoff settings
MAX_RETRIES = 5
INITIAL_BACKOFF = 2  # seconds
MAX_BACKOFF = 60     # max wait time between retries

# Thread-safe lock for API call timing and cache saving
api_lock = Lock()

def format_file_size(size_bytes):
    """Format file size in bytes to human-readable format"""
    if size_bytes == 0:
        return ""
    elif size_bytes < 1024:
        return f" ({size_bytes} B)"
    elif size_bytes < 1024 * 1024:
        return f" ({size_bytes / 1024:.1f} KB)"
    elif size_bytes < 1024 * 1024 * 1024:
        return f" ({size_bytes / (1024 * 1024):.1f} MB)"
    else:
        return f" ({size_bytes / (1024 * 1024 * 1024):.1f} GB)"

def setup_logging():
    """Setup logging to both console and file"""
    # Create cache directory if it doesn't exist
    os.makedirs(CACHE_DIR, exist_ok=True)
    
    # Disable flickrapi's verbose logging
    flickr_logger = logging.getLogger('flickrapi')
    flickr_logger.setLevel(logging.WARNING)
    
    # Set up our custom logger
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)  # Set to DEBUG to capture all levels
    
    # Remove existing handlers to avoid duplicates
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    # Create file handler
    file_handler = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    
    # Add only file handler to our logger
    logger.addHandler(file_handler)
    
    return logger

# Initialize logger
logger = None

def print_and_log(message, level="INFO"):
    """Print message to console and log to file with timestamp"""
    global logger
    if logger is None:
        logger = setup_logging()
    
    # For DEBUG messages, only log to file, don't print to console to avoid clutter
    if level.upper() != "DEBUG":
        # Print to console with timestamp
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        formatted_message = f"{timestamp} - {message}"
        print(formatted_message)
    
    # Log to file
    if level.upper() == "ERROR":
        logger.error(message)
    elif level.upper() == "WARNING":
        logger.warning(message)
    elif level.upper() == "DEBUG":
        logger.debug(message)
    else:
        logger.info(message)

class ProgressSpinner:
    """A rotating progress indicator"""
    def __init__(self, message=""):
        self.message = message
        self.spinner = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
        self.current = 0
        self.running = False
        self.last_logged_message = ""
        
    def start(self):
        """Start showing the spinner"""
        self.running = True
        self._show()
        
    def update(self, message=None):
        """Update the spinner and optionally the message"""
        if message:
            self.message = message
            # Log progress updates periodically (every 10th album or when message changes significantly)
            if message != self.last_logged_message:
                # Only log when the album changes (not just spinner updates)
                if "(" in message and ")" in message:
                    album_part = message.split(")")[-1].strip()
                    if album_part != self.last_logged_message:
                        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        log_message = f"{timestamp} - {message}"
                        # Write to log file directly without console output
                        global logger
                        if logger:
                            logger.info(message)
                        self.last_logged_message = album_part
                        
        if self.running:
            self._show()
            
    def stop(self, final_message=None):
        """Stop the spinner and show final message"""
        self.running = False
        if final_message:
            # Clear the line and use print_and_log for final message
            sys.stdout.write(f'\r{" " * 120}\r')
            sys.stdout.flush()
            print_and_log(final_message)
        else:
            # Just clear the line
            sys.stdout.write(f'\r{" " * 120}\r')
            sys.stdout.flush()
        
    def _show(self):
        """Show the current spinner frame"""
        if self.running:
            spinner_char = self.spinner[self.current % len(self.spinner)]
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            message = f'{timestamp} - {spinner_char} {self.message}'
            
            # Clear the entire line first, then write the new message
            sys.stdout.write(f'\r{" " * 120}\r{message}')
            sys.stdout.flush()
            self.current += 1

def create_spinner_message(album_index, total_albums, album_title):
    """Create a spinner message that fits within the display buffer"""
    base_message = f"Scanning albums... ({album_index}/{total_albums}) "
    available_space = 110 - len(base_message)  # Leave 10 chars buffer
    
    if len(album_title) > available_space:
        truncated_title = album_title[:available_space-3] + "..."
    else:
        truncated_title = album_title
        
    return f"{base_message}{truncated_title}"

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "_", name)

def is_video_file(filepath):
    """Check if a file is actually a video by reading its magic bytes"""
    if not os.path.exists(filepath):
        return False
    
    try:
        with open(filepath, 'rb') as f:
            header = f.read(12)
            if not header:
                return False
            
            # Check for common video file signatures
            # MP4: starts with 'ftyp' at offset 4
            if len(header) >= 8 and header[4:8] == b'ftyp':
                return True
            # AVI: starts with 'RIFF' and has 'AVI ' at offset 8
            if len(header) >= 12 and header[:4] == b'RIFF' and header[8:12] == b'AVI ':
                return True
            # MOV/QuickTime: similar to MP4, has 'ftyp' or 'moov'
            if len(header) >= 8 and (header[4:8] == b'moov' or header[4:8] == b'mdat'):
                return True
            # WebM: starts with EBML signature
            if header[:4] == b'\x1a\x45\xdf\xa3':
                return True
                
    except Exception:
        pass
    
    return False

def get_video_url_from_player(player_url):
    """
    Use the Flickr player URL directly for video downloads.
    Flickr will serve the highest quality available from this URL.
    """
    return player_url

def load_json_file(filepath):
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_json_file(filepath, data):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def flickr_api_call_with_retries(func, *args, **kwargs):
    backoff = INITIAL_BACKOFF
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with api_lock:
                # Rate limiting delay
                if hasattr(flickr_api_call_with_retries, "last_call_time"):
                    elapsed = time.time() - flickr_api_call_with_retries.last_call_time
                    if elapsed < API_CALL_DELAY:
                        time.sleep(API_CALL_DELAY - elapsed)
                        
                # Add timeout parameter to all API calls
                if 'timeout' not in kwargs:
                    kwargs['timeout'] = 120  # Increase timeout to 120 seconds
                    
                result = func(*args, **kwargs)
                flickr_api_call_with_retries.last_call_time = time.time()
            return result
        except flickrapi.exceptions.FlickrError as e:
            code = getattr(e, 'code', None)
            if code in [429, 503]:  # rate limit or server busy
                print(f"⚠️ API rate limit hit or server busy, retry {attempt}/{MAX_RETRIES} after {backoff}s...")
            else:
                print(f"⚠️ Flickr API error: {e}, retry {attempt}/{MAX_RETRIES} after {backoff}s...")
        except (RequestException, Timeout) as e:
            print(f"⚠️ Network error: {e}, retry {attempt}/{MAX_RETRIES} after {backoff}s...")
            # For network errors, use a longer backoff
            backoff = min(backoff * 2.5, MAX_BACKOFF)
            continue

        time.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)

    raise RuntimeError(f"API call failed after {MAX_RETRIES} retries.")

def get_original_url_and_info(flickr, photo_id, url_cache):
    # Check cache first
    cache_key = f"{photo_id}_info"
    if cache_key in url_cache:
        return url_cache[cache_key]

    # Get photo info to determine media type
    photo_info = flickr_api_call_with_retries(flickr.photos.getInfo, photo_id=photo_id)['photo']
    media_type = photo_info.get('media', 'photo')  # 'photo' or 'video'
    
    if media_type == 'video' and not DOWNLOAD_VIDEO:
        # Skip video downloads if disabled
        return None
    
    original_url = None
    selected_info = None
    
    try:
        # Get all available sizes
        sizes = flickr_api_call_with_retries(flickr.photos.getSizes, photo_id=photo_id)['sizes']['size']
        
        if media_type == 'video':
            # For videos, find the best video URL (prefer Original, then highest resolution, then largest file size)
            video_candidates = []
            for s in sizes:
                if '/play/' in s['source'] or 'video' in s['label'].lower():
                    resolution = int(s.get('width', 0)) * int(s.get('height', 0))
                    file_size = int(s.get('size', 0))  # File size in bytes
                    video_candidates.append({
                        'url': s['source'],
                        'label': s['label'],
                        'width': int(s.get('width', 0)),
                        'height': int(s.get('height', 0)),
                        'resolution': resolution,
                        'file_size': file_size,
                        'is_original': s['label'].lower() == 'original'
                    })
            
            if video_candidates:
                # Sort by: 1) original flag, 2) resolution, 3) file size (all descending)
                video_candidates.sort(key=lambda x: (x['is_original'], x['resolution'], x['file_size']), reverse=True)
                
                # Log all candidates for transparency (debug level)
                if len(video_candidates) > 1:
                    print_and_log(f"    📊 Video quality candidates for {photo_id}:", "DEBUG")
                    for i, candidate in enumerate(video_candidates):
                        marker = "👑" if i == 0 else "  "
                        size_info = format_file_size(candidate['file_size'])
                        print_and_log(f"      {marker} {candidate['label']} ({candidate['width']}x{candidate['height']}){size_info}", "DEBUG")
                
                best_video = video_candidates[0]
                original_url = best_video['url']
                
                # Include file size in selection info if available
                size_info = format_file_size(best_video['file_size'])
                selected_info = f"{best_video['label']} ({best_video['width']}x{best_video['height']}){size_info}"
                
                # Use player URL directly if it's a player URL
                if '/play/' in original_url:
                    original_url = get_video_url_from_player(original_url)
                    
                print_and_log(f"    🎬 Selected video quality: {selected_info}")
            else:
                print_and_log(f"    ❌ No video URLs found for {photo_id}", "ERROR")
                return None
        else:
            # For photos, find the highest quality image (prefer Original, then highest resolution, then largest file size)
            photo_candidates = []
            for s in sizes:
                # Skip video-specific URLs for photos
                if '/play/' not in s['source'] and 'video' not in s['label'].lower():
                    resolution = int(s.get('width', 0)) * int(s.get('height', 0))
                    file_size = int(s.get('size', 0))  # File size in bytes
                    photo_candidates.append({
                        'url': s['source'],
                        'label': s['label'],
                        'width': int(s.get('width', 0)),
                        'height': int(s.get('height', 0)),
                        'resolution': resolution,
                        'file_size': file_size,
                        'is_original': s['label'].lower() == 'original'
                    })
            
            if photo_candidates:
                # Sort by: 1) original flag, 2) resolution, 3) file size (all descending)
                photo_candidates.sort(key=lambda x: (x['is_original'], x['resolution'], x['file_size']), reverse=True)
                
                # Log all candidates for transparency (debug level)
                if len(photo_candidates) > 1:
                    print_and_log(f"    📊 Image quality candidates for {photo_id}:", "DEBUG")
                    for i, candidate in enumerate(photo_candidates):
                        marker = "👑" if i == 0 else "  "
                        size_info = format_file_size(candidate['file_size'])
                        print_and_log(f"      {marker} {candidate['label']} ({candidate['width']}x{candidate['height']}){size_info}", "DEBUG")
                
                best_photo = photo_candidates[0]
                original_url = best_photo['url']
                
                # Include file size in selection info if available
                size_info = format_file_size(best_photo['file_size'])
                selected_info = f"{best_photo['label']} ({best_photo['width']}x{best_photo['height']}){size_info}"
                
                print_and_log(f"    📷 Selected image quality: {selected_info}")
            else:
                print_and_log(f"    ❌ No image URLs found for {photo_id}", "ERROR")
                return None

    except Exception as e:
        print_and_log(f"  ❌ Error getting sizes for {photo_id}: {e}", "ERROR")
        return None

    if not original_url:
        return None

    # Cache and save immediately
    result = {'url': original_url, 'media_type': media_type, 'selected_info': selected_info}
    url_cache[cache_key] = result
    save_json_file(URL_CACHE_FILE, url_cache)
    return result

def download_file(url, filepath, media_type=None):
    try:
        response = requests.get(url, stream=True, timeout=180)
        response.raise_for_status()
        
        # Get actual content type and determine correct extension
        content_type = response.headers.get('content-type', '').lower()
        correct_ext = None
        
        if 'video' in content_type or media_type == 'video':
            if 'mp4' in content_type:
                correct_ext = '.mp4'
            elif 'mov' in content_type or 'quicktime' in content_type:
                correct_ext = '.mov'
            else:
                correct_ext = '.mp4'  # Default for videos
        elif 'image' in content_type:
            if 'jpeg' in content_type or 'jpg' in content_type:
                correct_ext = '.jpg'
            elif 'png' in content_type:
                correct_ext = '.png'
            elif 'gif' in content_type:
                correct_ext = '.gif'
            else:
                correct_ext = '.jpg'  # Default for images
        else:
            # Fallback based on media_type
            correct_ext = '.mp4' if media_type == 'video' else '.jpg'
        
        # Update filepath if extension needs correction
        if correct_ext:
            base_path, current_ext = os.path.splitext(filepath)
            if current_ext.lower() != correct_ext.lower():
                filepath = base_path + correct_ext
        
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(8192):
                f.write(chunk)
        return filepath
    except Exception as e:
        return f"ERROR: {filepath} - {e}"

def process_downloads(album_title, photo_ids, flickr, url_cache, downloaded_ids):
    album_folder = os.path.join(DOWNLOAD_DIR, album_title)
    os.makedirs(album_folder, exist_ok=True)
    print_and_log(f"📂 Processing album: {album_title} ({len(photo_ids)} photos)")
    
    # Debug directory permissions
    try:
        test_file_path = os.path.join(album_folder, ".write_test")
        with open(test_file_path, 'w') as f:
            f.write("test")
        os.remove(test_file_path)
        print(f"  ✅ Directory is writable: {album_folder}")
    except Exception as e:
        print(f"  ❌ CRITICAL: Directory permission issue: {e}")
        return {"album": album_title, "downloaded": 0, "skipped": 0, "failed": len(photo_ids)}

    # Reset tracking for empty album
    if os.path.exists(album_folder) and not os.listdir(album_folder) and photo_ids:
        print(f"  🔄 Album directory exists but is empty. Resetting tracking for this album.")
        album_photo_ids = {pid for pid, _ in photo_ids}
        # Remove these IDs from downloaded_ids to force re-download
        downloaded_ids.difference_update(album_photo_ids)
        
    download_tasks = []
    downloaded_count = 0
    skipped_count = 0
    failed_count = 0
    
    for i, (photo_id, title) in enumerate(photo_ids):
        if photo_id in downloaded_ids:
            print(f"  ⏩ Skipping {title} (ID: {photo_id}) (marked as downloaded)")
            skipped_count += 1
            continue

        try:
            url_info = get_original_url_and_info(flickr, photo_id, url_cache)
            if not url_info:  # Skip if video downloads are disabled
                skipped_count += 1
                continue
                
            url = url_info['url']
            media_type = url_info['media_type']
            
            # Log currently processed media file
            media_icon = "🎥" if media_type == 'video' else "📸"
            print_and_log(f"  {media_icon} Processing: {title} ({media_type})")
            
            # Determine file extension based on media type and URL
            ext = os.path.splitext(url)[1]
            if not ext:
                # If no extension in URL, use defaults based on media type
                if media_type == 'video':
                    ext = ".mp4"  # Default video extension
                else:
                    ext = ".jpg"  # Default photo extension
            elif media_type == 'video' and ext.lower() in ['.jpg', '.jpeg', '.png']:
                # If it's a video but has an image extension, it's likely incorrect
                ext = ".mp4"
                
            filepath = os.path.join(album_folder, f"{sanitize_filename(title)}{ext}")
            
            # Enhanced file existence check - for videos, check multiple possible extensions
            existing_file = None
            if media_type == 'video':
                # Check for common video extensions
                video_extensions = ['.mp4', '.mov', '.avi', '.webm', '.flv']
                base_name = sanitize_filename(title)
                for video_ext in video_extensions:
                    test_path = os.path.join(album_folder, f"{base_name}{video_ext}")
                    if os.path.exists(test_path):
                        existing_file = test_path
                        break
                # Also check with the wrong extension (jpg) in case it was downloaded incorrectly before
                for wrong_ext in ['.jpg', '.jpeg']:
                    test_path = os.path.join(album_folder, f"{base_name}{wrong_ext}")
                    if os.path.exists(test_path):
                        # Verify this is actually a video file
                        if is_video_file(test_path):
                            print(f"  🔄 Found video file with wrong extension: {test_path}")
                            # Try to rename it to the correct extension
                            correct_path = os.path.join(album_folder, f"{base_name}.mp4")
                            if not os.path.exists(correct_path):
                                try:
                                    os.rename(test_path, correct_path)
                                    print(f"  ✅ Renamed {os.path.basename(test_path)} -> {os.path.basename(correct_path)}")
                                    existing_file = correct_path
                                except Exception as e:
                                    print(f"  ⚠️ Could not rename file: {e}")
                                    existing_file = test_path
                            else:
                                existing_file = test_path
                        else:
                            # It's actually an image file, not a video
                            print(f"  ℹ️ Found image file: {test_path} (not a video)")
                        break
            else:
                # For photos, check common image extensions
                image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.tiff']
                base_name = sanitize_filename(title)
                for img_ext in image_extensions:
                    test_path = os.path.join(album_folder, f"{base_name}{img_ext}")
                    if os.path.exists(test_path):
                        existing_file = test_path
                        break
            
            if existing_file:
                print(f"  ⏩ Skipping {os.path.basename(existing_file)} (file exists)")
                skipped_count += 1
                downloaded_ids.add(photo_id)
                continue

            download_tasks.append((photo_id, url, filepath, media_type))
        except Exception as e:
            print(f"  ❌ Error preparing download for {photo_id}: {e}")
            failed_count += 1

    if not download_tasks:
        print(f"  ⚠️ No files to download in this album. All {skipped_count} files were skipped.")
        if photo_ids and skipped_count == 0 and failed_count == 0:
            print(f"  ⚠️ CRITICAL: No downloads were queued despite having {len(photo_ids)} files.")
        return {"album": album_title, "downloaded": 0, "skipped": skipped_count, "failed": failed_count}
    
    print_and_log(f"  🔽 Downloading {len(download_tasks)} files...")

    def download_task(task):
        photo_id, url, path, media_type = task
        try:
            result = download_file(url, path, media_type)
            return photo_id, result
        except Exception as e:
            return photo_id, f"ERROR: {path} - {e}"

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(download_task, t) for t in download_tasks]
        
        for future in as_completed(futures):
            try:
                photo_id, result = future.result()
                if isinstance(result, str) and result.startswith("ERROR"):
                    print_and_log(f"  ❌ Failed: {os.path.basename(result.split(' - ')[0].replace('ERROR: ', ''))}", "ERROR")
                    failed_count += 1
                else:
                    # Determine media type from file extension for logging
                    filename = os.path.basename(result)
                    is_video_download = result.lower().endswith(('.mp4', '.mov', '.avi', '.webm'))
                    media_icon = "🎥" if is_video_download else "📸"
                    print_and_log(f"  ✅ Downloaded: {media_icon} {filename}")
                    
                    # Verify file exists and has content
                    if os.path.exists(result) and os.path.getsize(result) > 0:
                        downloaded_count += 1
                        downloaded_ids.add(photo_id)
                    else:
                        print_and_log(f"  ⚠️ File verification failed for {filename}", "WARNING")
                        failed_count += 1
            except Exception as e:
                print(f"  ❌ Unexpected error processing download result: {e}")
                failed_count += 1

    # Save progress after album
    save_json_file(PROGRESS_FILE, {"downloaded_ids": list(downloaded_ids)})
    
    # Check downloads actually happened
    files_in_dir = len(os.listdir(album_folder))
    print(f"  📊 Files now in directory: {files_in_dir}")
    if downloaded_count > 0 and files_in_dir == 0:
        print(f"  ❌ CRITICAL: Files were reported as downloaded but directory is empty!")
    
    return {
        "album": album_title,
        "downloaded": downloaded_count,
        "skipped": skipped_count,
        "failed": failed_count
    }

def main():
    # Setup logging
    global logger
    logger = setup_logging()
    
    print_and_log("🚀 Starting Flickr Downloader")
    print_and_log("=" * 50)
    
    # Check for required configurations
    if not API_KEY or not API_SECRET:
        print_and_log("❌ ERROR: API_KEY and API_SECRET must be set in .env file", "ERROR")
        return    
    
    print_and_log(f"ℹ️ Video downloads: {'✅ Enabled' if DOWNLOAD_VIDEO else '❌ Disabled'}")
    print_and_log("")
    
    flickr = flickrapi.FlickrAPI(API_KEY, API_SECRET, format='parsed-json')
    if not flickr.token_valid(perms='read'):
        flickr.get_request_token(oauth_callback='oob')
        authorize_url = flickr.auth_url(perms='read')
        print_and_log(f"Open this URL to authorize: {authorize_url}")
        verifier = input("Enter the verification code: ")
        flickr.get_access_token(verifier)

    user_info = flickr_api_call_with_retries(flickr.test.login)
    user_id = user_info['user']['id']

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    url_cache = load_json_file(URL_CACHE_FILE)
    progress = load_json_file(PROGRESS_FILE)
    downloaded_ids = set(progress.get("downloaded_ids", []))

    # First, gather all photos from all albums to track duplicates
    print_and_log("🔍 Scanning albums to identify duplicate media files...")
    all_album_photo_ids = set()
    photo_locations = {}  # Maps photo_id -> list of albums containing it
    album_photos = {}     # Maps album_title -> list of (photo_id, title) tuples
    
    # Get list of all albums
    photosets = flickr_api_call_with_retries(flickr.photosets.getList, user_id=user_id)['photosets']['photoset']
    
    # Initialize progress spinner
    spinner = ProgressSpinner("Scanning albums...")
    spinner.start()
    
    # First pass: collect all photos and their locations
    total_albums = len(photosets)
    for album_index, photoset in enumerate(photosets, 1):
        album_id = photoset['id']
        album_title = sanitize_filename(photoset['title']['_content'])
        photo_ids = []

        # Update progress spinner with current album
        spinner.update(create_spinner_message(album_index, total_albums, album_title))

        page = 1
        while True:
            photos_data = flickr_api_call_with_retries(
                flickr.photosets.getPhotos,
                photoset_id=album_id,
                user_id=user_id,
                media="all",
                page=page
            )['photoset']

            for photo in photos_data['photo']:
                pid = photo['id']
                title = sanitize_filename(photo['title'] or pid)
                photo_ids.append((pid, title))
                all_album_photo_ids.add(pid)
                
                # Track which albums contain this photo
                if pid not in photo_locations:
                    photo_locations[pid] = []
                photo_locations[pid].append(album_title)
                
                # Update spinner occasionally to show activity
                if len(photo_ids) % 50 == 0:
                    spinner.update(create_spinner_message(album_index, total_albums, album_title))
                
            if page >= photos_data['pages']:
                break
            page += 1

        # Final update for this album
        spinner.update(create_spinner_message(album_index, total_albums, album_title))
        album_photos[album_title] = photo_ids
    
    # Stop the spinner and show completion
    spinner.stop("✅ Album scanning completed!")
    
    # Find duplicates and decide primary location
    duplicate_count = 0
    photos_to_download = {}  # Maps photo_id -> (album_title, photo_title)
    
    for pid, albums in photo_locations.items():
        if len(albums) > 1:
            duplicate_count += 1
            # If photo appears in multiple albums, prefer any album other than "Auto Upload"
            primary_album = next((album for album in albums if album != "Auto Upload"), albums[0])
            
            # Find the photo title from the primary album
            for photo_id, title in album_photos[primary_album]:
                if photo_id == pid:
                    photos_to_download[pid] = (primary_album, title)
                    break
        else:
            # For non-duplicates, just use the only album
            album = albums[0]
            for photo_id, title in album_photos[album]:
                if photo_id == pid:
                    photos_to_download[pid] = (album, title)
                    break
    
    print_and_log(f"📊 Found {duplicate_count} media files that appear in multiple albums")
    
    # Second pass: Download photos to their primary locations
    album_summaries = {}
    
    for pid, (album_title, photo_title) in photos_to_download.items():
        # Initialize album summary if needed
        if album_title not in album_summaries:
            album_summaries[album_title] = {
                "album": album_title,
                "to_download": [],
                "downloaded": 0,
                "skipped": 0,
                "failed": 0
            }
            
        # Add to download list if not already downloaded
        if pid not in downloaded_ids:
            album_summaries[album_title]["to_download"].append((pid, photo_title))
        else:
            album_summaries[album_title]["skipped"] += 1
    
    # Process each album
    result_summaries = []
    for album_title, summary in album_summaries.items():
        if summary["to_download"]:
            result = process_downloads(album_title, summary["to_download"], flickr, url_cache, downloaded_ids)
            result_summaries.append(result)
        else:
            print_and_log(f"\n📂 Album: {album_title} - All {summary['skipped']} photos already downloaded")
            result_summaries.append({
                "album": album_title,
                "downloaded": 0,
                "skipped": summary["skipped"],
                "failed": 0
            })

    # Download Unsorted (not in any album)
    unsorted_photo_ids = []
    page = 1
    while True:
        photos_data = flickr_api_call_with_retries(
            flickr.people.getPhotos,
            user_id=user_id,
            privacy_filter=1,
            media="all",
            page=page
        )['photos']

        for photo in photos_data['photo']:
            pid = photo['id']
            if pid not in all_album_photo_ids:
                title = sanitize_filename(photo['title'] or pid)
                unsorted_photo_ids.append((pid, title))

        if page >= photos_data['pages']:
            break
        page += 1

    if unsorted_photo_ids:
        summary = process_downloads("Unsorted", unsorted_photo_ids, flickr, url_cache, downloaded_ids)
        result_summaries.append(summary)
    else:
        print_and_log("\n📂 No media files found outside of albums.")

    # Final summary
    print_and_log("\n📊 Download Summary:")
    for summary in result_summaries:
        print_and_log(f"  {summary['album']}: {summary['downloaded']} downloaded, "
              f"{summary['skipped']} skipped, {summary['failed']} failed")

    print_and_log("\n✅ All albums processed.")
    print_and_log("=" * 50)

if __name__ == "__main__":
    main()
