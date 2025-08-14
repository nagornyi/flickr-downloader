import os
import re
import time
import json
import requests
import flickrapi
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from requests.exceptions import RequestException, Timeout

# Load environment variables from .env file
load_dotenv()

# Your Flickr API key and secret
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# Video download token (account-specific, get from browser network tab)
VIDEO_TOKEN = os.getenv("VIDEO_TOKEN")

# Where to store the downloaded files
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "./flickr_downloads")
CACHE_DIR = "./cache"
URL_CACHE_FILE = os.path.join(CACHE_DIR, "url_cache.json")
PROGRESS_FILE = os.path.join(CACHE_DIR, "progress.json")

# Max simultaneous downloads
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 8))

# Minimum seconds between Flickr API calls to respect rate limits
API_CALL_DELAY = 1.1

# Retry/backoff settings
MAX_RETRIES = 5
INITIAL_BACKOFF = 2  # seconds
MAX_BACKOFF = 60     # max wait time between retries

# Thread-safe lock for API call timing and cache saving
api_lock = Lock()

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

def clean_fake_video_files(directory):
    """Find and remove .mp4/.mov files that are actually images"""
    cleaned_count = 0
    if not os.path.exists(directory):
        return cleaned_count
        
    for filename in os.listdir(directory):
        if filename.lower().endswith(('.mp4', '.mov', '.avi', '.webm')):
            filepath = os.path.join(directory, filename)
            if not is_video_file(filepath):
                print(f"  🗑️ Removing fake video file: {filename}")
                try:
                    os.remove(filepath)
                    cleaned_count += 1
                except Exception as e:
                    print(f"  ❌ Could not remove {filename}: {e}")
    
    return cleaned_count

def construct_direct_video_url(player_url, video_token=None):
    """
    Construct direct video URL from Flickr player URL using the pattern:
    https://www.flickr.com/photos/user/PHOTO_ID/play/orig/SECRET/ ->
    https://live.staticflickr.com/video/PHOTO_ID/SECRET/orig.mp4?s=TOKEN
    """
    try:
        # Parse the player URL to extract photo ID and secret
        url_parts = player_url.rstrip('/').split('/')
        if len(url_parts) < 7 or 'play' not in url_parts:
            print(f"  ⚠️ Invalid player URL format: {player_url}")
            return None
        
        photo_id = url_parts[5]  # Photo ID
        secret = url_parts[-1] if url_parts[-1] != 'orig' else url_parts[-2]  # Secret
        
        # Construct the direct URL
        direct_url = f"https://live.staticflickr.com/video/{photo_id}/{secret}/orig.mp4"
        
        # Add the token if provided
        if video_token:
            direct_url += f"?s={video_token}"
        else:
            print(f"  ⚠️ VIDEO_TOKEN not provided - direct download may fail without authentication token")
        
        print(f"  🔗 Constructed direct URL: {direct_url}")
        return direct_url
        
    except Exception as e:
        print(f"  ❌ Error constructing direct URL: {e}")
        return None

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
    
    original_url = None
    
    if media_type == 'video':
        # For videos, we need to try different approaches to get the actual video file
        try:
            # Method 1: Try to get all sizes and look for video-specific URLs
            sizes = flickr_api_call_with_retries(flickr.photos.getSizes, photo_id=photo_id)['sizes']['size']
            print(f"  🔍 Available sizes for video {photo_id}:")
            
            video_sizes = []
            image_sizes = []
            
            for s in sizes:
                url = s['source']
                label = s['label']
                width = int(s.get('width', 0))
                height = int(s.get('height', 0))
                
                print(f"    {label} ({width}x{height}): {url}")
                
                # Check content type to determine if it's actually a video
                try:
                    head_response = requests.head(url, timeout=15, allow_redirects=True)
                    content_type = head_response.headers.get('content-type', '').lower()
                    content_length = head_response.headers.get('content-length', 0)
                    try:
                        content_length = int(content_length)
                    except:
                        content_length = 0
                    
                    print(f"      Content-Type: {content_type}, Size: {content_length} bytes")
                    
                    if 'video' in content_type:
                        video_sizes.append({
                            'label': label,
                            'url': url,
                            'width': width,
                            'height': height,
                            'content_type': content_type,
                            'size_bytes': content_length,
                            'resolution': width * height
                        })
                    elif 'image' in content_type:
                        image_sizes.append({
                            'label': label,
                            'url': url,
                            'width': width,
                            'height': height,
                            'content_type': content_type,
                            'size_bytes': content_length,
                            'resolution': width * height
                        })
                    else:
                        # Unknown content type, categorize by other indicators
                        label_lower = label.lower()
                        url_lower = url.lower()
                        if ('video' in label_lower or 'mp4' in label_lower or 
                            '.mp4' in url_lower or '.mov' in url_lower or '.avi' in url_lower):
                            video_sizes.append({
                                'label': label,
                                'url': url,
                                'width': width,
                                'height': height,
                                'content_type': content_type or 'unknown',
                                'size_bytes': content_length,
                                'resolution': width * height
                            })
                        else:
                            image_sizes.append({
                                'label': label,
                                'url': url,
                                'width': width,
                                'height': height,
                                'content_type': content_type or 'unknown',
                                'size_bytes': content_length,
                                'resolution': width * height
                            })
                except Exception as e:
                    print(f"      Error checking content type: {e}")
                    # If we can't check content type, categorize by label/URL
                    label_lower = label.lower()
                    url_lower = url.lower()
                    if ('video' in label_lower or 'mp4' in label_lower or 
                        '.mp4' in url_lower or '.mov' in url_lower or '.avi' in url_lower):
                        video_sizes.append({
                            'label': label,
                            'url': url,
                            'width': width,
                            'height': height,
                            'content_type': 'unknown',
                            'size_bytes': 0,
                            'resolution': width * height
                        })
                    else:
                        image_sizes.append({
                            'label': label,
                            'url': url,
                            'width': width,
                            'height': height,
                            'content_type': 'unknown',
                            'size_bytes': 0,
                            'resolution': width * height
                        })
            
            # Select the best video URL
            if video_sizes:
                # Sort by: 1. file size (bytes), 2. resolution, 3. prefer "original" in label
                def video_sort_key(v):
                    size_score = v['size_bytes'] if v['size_bytes'] > 0 else v['resolution']
                    original_bonus = 1000000 if 'original' in v['label'].lower() else 0
                    return size_score + original_bonus
                
                video_sizes.sort(key=video_sort_key, reverse=True)
                best_video = video_sizes[0]
                original_url = best_video['url']
                
                # Check if this is a Flickr video player URL that needs to be resolved to direct URL
                if '/play/' in original_url:
                    print(f"  🔄 Converting Flickr video player URL to direct download URL...")
                    direct_url = construct_direct_video_url(original_url, VIDEO_TOKEN)
                    if direct_url and direct_url != original_url:
                        original_url = direct_url
                        print(f"  ✅ Converted to direct URL: {original_url}")
                    else:
                        print(f"  ⚠️ Could not convert to direct URL, using original")
                
                print(f"  ✅ Selected highest quality video:")
                print(f"    {best_video['label']} ({best_video['width']}x{best_video['height']})")
                print(f"    {best_video['size_bytes']} bytes, Content-Type: {best_video['content_type']}")
                print(f"    URL: {original_url}")
                
            elif image_sizes:
                # No actual video URLs found, use highest resolution image as fallback
                image_sizes.sort(key=lambda x: x['resolution'], reverse=True)
                best_image = image_sizes[0]
                original_url = best_image['url']
                print(f"  ⚠️ No video URLs found! Using highest resolution image as fallback:")
                print(f"    {best_image['label']} ({best_image['width']}x{best_image['height']})")
                print(f"    This will likely be a preview image, not the actual video!")
                
            else:
                # Fallback to original logic if we can't categorize anything
                for s in sizes:
                    if s['label'].lower() == "original":
                        original_url = s['source']
                        print(f"  ⚠️ Using 'Original' size (may be preview): {original_url}")
                        break
                        
            if not original_url:
                original_url = sizes[-1]['source']
                print(f"  ⚠️ Using largest available size: {original_url}")
                
        except Exception as e:
            print(f"  ❌ Error processing video {photo_id}: {e}")
            # Fallback to normal sizes API
            sizes = flickr_api_call_with_retries(flickr.photos.getSizes, photo_id=photo_id)['sizes']['size']
            for s in sizes:
                if s['label'].lower() == "original":
                    original_url = s['source']
                    break
            if not original_url:
                original_url = sizes[-1]['source']
    else:
        # For photos, use the normal sizes API
        sizes = flickr_api_call_with_retries(flickr.photos.getSizes, photo_id=photo_id)['sizes']['size']
        for s in sizes:
            if s['label'].lower() == "original":
                original_url = s['source']
                break
        if not original_url:
            original_url = sizes[-1]['source']

    # Cache and save immediately
    result = {'url': original_url, 'media_type': media_type}
    url_cache[cache_key] = result
    save_json_file(URL_CACHE_FILE, url_cache)
    return result

def download_file(url, filepath, media_type=None):
    try:
        # Increase timeout for downloads
        response = requests.get(url, stream=True, timeout=180)
        response.raise_for_status()
        
        # Get actual content type from response headers
        content_type = response.headers.get('content-type', '').lower()
        print(f"  📄 Content-Type: {content_type}")
        
        # Determine correct extension based on content type
        correct_ext = None
        if 'video' in content_type or media_type == 'video':
            if 'mp4' in content_type:
                correct_ext = '.mp4'
            elif 'mov' in content_type or 'quicktime' in content_type:
                correct_ext = '.mov'
            elif 'avi' in content_type:
                correct_ext = '.avi'
            elif 'webm' in content_type:
                correct_ext = '.webm'
            elif 'flv' in content_type:
                correct_ext = '.flv'
            else:
                correct_ext = '.mp4'  # Default for videos
                print(f"  ⚠️ Unknown video content-type '{content_type}', using .mp4")
        elif 'image' in content_type:
            if 'jpeg' in content_type or 'jpg' in content_type:
                correct_ext = '.jpg'
            elif 'png' in content_type:
                correct_ext = '.png'
            elif 'gif' in content_type:
                correct_ext = '.gif'
            elif 'webp' in content_type:
                correct_ext = '.webp'
            elif 'tiff' in content_type:
                correct_ext = '.tiff'
            else:
                correct_ext = '.jpg'  # Default for images
                print(f"  ⚠️ Unknown image content-type '{content_type}', using .jpg")
        else:
            # Fallback: try to determine from media_type if content-type is unclear
            if media_type == 'video':
                correct_ext = '.mp4'
                print(f"  ⚠️ Unclear content-type '{content_type}' for video, using .mp4")
            else:
                correct_ext = '.jpg'
                print(f"  ⚠️ Unclear content-type '{content_type}' for photo, using .jpg")
        
        # Update filepath if extension is incorrect
        if correct_ext:
            base_path, current_ext = os.path.splitext(filepath)
            if current_ext.lower() != correct_ext.lower():
                new_filepath = base_path + correct_ext
                print(f"  🔄 Correcting extension: {os.path.basename(filepath)} -> {os.path.basename(new_filepath)}")
                filepath = new_filepath
        
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(8192):  # Larger chunks for better performance
                f.write(chunk)
        return filepath
    except Exception as e:
        return f"ERROR: {filepath} - {e}"

def process_downloads(album_title, photo_ids, flickr, url_cache, downloaded_ids):
    album_folder = os.path.join(DOWNLOAD_DIR, album_title)
    os.makedirs(album_folder, exist_ok=True)
    print(f"\n📂 Processing album: {album_title} ({len(photo_ids)} photos)")
    
    # Clean up any fake video files from previous runs
    cleaned_count = clean_fake_video_files(album_folder)
    if cleaned_count > 0:
        print(f"  🧹 Cleaned up {cleaned_count} fake video files")
    
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

    # Check if we should limit initial downloads for large albums
    photo_limit = None
    if len(photo_ids) > 1000:
        # Comment out or remove these lines to download all photos
        # photo_limit = 50
        # print(f"  ⚠️ Large album detected ({len(photo_ids)} photos). First downloading {photo_limit} photos as a test.")
        print(f"  ℹ️ Large album detected ({len(photo_ids)} photos). Downloading all photos.")
    
    for i, (photo_id, title) in enumerate(photo_ids):
        if photo_limit and i >= photo_limit:
            break
            
        if photo_id in downloaded_ids:
            print(f"  ⏩ Skipping {title} (ID: {photo_id}) (marked as downloaded)")
            skipped_count += 1
            continue

        try:
            print(f"  🔍 Getting URL for photo {photo_id} ({title})")
            url_info = get_original_url_and_info(flickr, photo_id, url_cache)
            url = url_info['url']
            media_type = url_info['media_type']
            print(f"  📋 URL: {url} (media type: {media_type})")
            
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
        print(f"  ⚠️ No files to download in this album. All {skipped_count} photos were skipped.")
        if photo_ids and skipped_count == 0 and failed_count == 0:
            print(f"  ⚠️ CRITICAL: No downloads were queued despite having {len(photo_ids)} photos.")
        return {"album": album_title, "downloaded": 0, "skipped": skipped_count, "failed": failed_count}
    
    print(f"  🔽 Downloading {len(download_tasks)} photos...")

    def download_task(task):
        photo_id, url, path, media_type = task
        try:
            print(f"  ⬇️ Starting download: {os.path.basename(path)} ({media_type})")
            result = download_file(url, path, media_type)
            print(f"  ✓ Finished download: {os.path.basename(result) if not result.startswith('ERROR') else 'FAILED'}")
            return photo_id, result
        except Exception as e:
            print(f"  ❌ Download exception: {e}")
            return photo_id, f"ERROR: {path} - {e}"

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(download_task, t) for t in download_tasks]
        
        for future in as_completed(futures):
            try:
                photo_id, result = future.result()
                if isinstance(result, str) and result.startswith("ERROR"):
                    print(f"  ❌ {result}")
                    failed_count += 1
                else:
                    print(f"  ✅ Downloaded: {os.path.basename(result)}")
                    # Verify file exists and has content
                    if os.path.exists(result) and os.path.getsize(result) > 0:
                        # Additional check for video files - make sure they're actually videos
                        if result.lower().endswith(('.mp4', '.mov', '.avi', '.webm')) and not is_video_file(result):
                            print(f"  ❌ File {os.path.basename(result)} is not a valid video file (likely a preview image)")
                            try:
                                os.remove(result)
                                print(f"  🗑️ Removed invalid video file")
                            except Exception as e:
                                print(f"  ⚠️ Could not remove invalid file: {e}")
                            failed_count += 1
                        else:
                            downloaded_count += 1
                            downloaded_ids.add(photo_id)
                    else:
                        print(f"  ⚠️ File verification failed for {result}")
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
    # Check for required configurations
    if not API_KEY or not API_SECRET:
        print("❌ ERROR: API_KEY and API_SECRET must be set in .env file")
        return
    
    if not VIDEO_TOKEN:
        print("⚠️ WARNING: VIDEO_TOKEN not set in .env file")
        print("   Video downloads may fail without the authentication token")
        print("   See Readme for instructions on how to get the token")
        print()
    
    flickr = flickrapi.FlickrAPI(API_KEY, API_SECRET, format='parsed-json')
    if not flickr.token_valid(perms='read'):
        flickr.get_request_token(oauth_callback='oob')
        authorize_url = flickr.auth_url(perms='read')
        print(f"Open this URL to authorize: {authorize_url}")
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
    print("🔍 Scanning albums to identify duplicate photos...")
    all_album_photo_ids = set()
    photo_locations = {}  # Maps photo_id -> list of albums containing it
    album_photos = {}     # Maps album_title -> list of (photo_id, title) tuples
    
    # Get list of all albums
    photosets = flickr_api_call_with_retries(flickr.photosets.getList, user_id=user_id)['photosets']['photoset']
    
    # First pass: collect all photos and their locations
    for photoset in photosets:
        album_id = photoset['id']
        album_title = sanitize_filename(photoset['title']['_content'])
        photo_ids = []

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
                
            if page >= photos_data['pages']:
                break
            page += 1

        album_photos[album_title] = photo_ids
    
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
    
    print(f"📊 Found {duplicate_count} photos that appear in multiple albums")
    
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
            print(f"\n📂 Album: {album_title} - All {summary['skipped']} photos already downloaded")
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
        print("\n📂 No photos found outside of albums.")

    # Final summary
    print("\n📊 Download Summary:")
    for summary in result_summaries:
        print(f"  {summary['album']}: {summary['downloaded']} downloaded, "
              f"{summary['skipped']} skipped, {summary['failed']} failed")

    print("\n✅ All albums processed.")

if __name__ == "__main__":
    main()
