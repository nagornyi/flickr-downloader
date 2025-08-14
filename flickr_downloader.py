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

def get_original_url(flickr, photo_id, url_cache):
    # Check cache first
    if photo_id in url_cache:
        return url_cache[photo_id]

    sizes = flickr_api_call_with_retries(flickr.photos.getSizes, photo_id=photo_id)['sizes']['size']
    original_url = None
    for s in sizes:
        if s['label'].lower() == "original":
            original_url = s['source']
            break
    if not original_url:
        original_url = sizes[-1]['source']

    # Cache and save immediately
    url_cache[photo_id] = original_url
    save_json_file(URL_CACHE_FILE, url_cache)
    return original_url

def download_file(url, filepath):
    try:
        # Increase timeout for downloads
        response = requests.get(url, stream=True, timeout=180)
        response.raise_for_status()
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
            url = get_original_url(flickr, photo_id, url_cache)
            print(f"  📋 URL: {url}")
            
            ext = os.path.splitext(url)[1] or ".jpg"
            filepath = os.path.join(album_folder, f"{sanitize_filename(title)}{ext}")
            
            if os.path.exists(filepath):
                print(f"  ⏩ Skipping {filepath} (file exists)")
                skipped_count += 1
                downloaded_ids.add(photo_id)
                continue

            download_tasks.append((photo_id, url, filepath))
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
        photo_id, url, path = task
        try:
            print(f"  ⬇️ Starting download: {os.path.basename(path)}")
            result = download_file(url, path)
            print(f"  ✓ Finished download: {os.path.basename(path)}")
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
