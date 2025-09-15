"""
Flickr API client with retry logic and rate limiting.
Handles all communication with the Flickr API.
"""
import time
import flickrapi
from threading import Lock
from requests.exceptions import RequestException, Timeout

from ..config import config
from ..utils.ui import print_and_log
from ..utils.files import format_file_size, save_json_file


class FlickrAPIClient:
    """Wrapper for Flickr API with retry logic and rate limiting."""
    
    def __init__(self):
        self.api_lock = Lock()
        self.last_call_time = None
        
    def call_with_retries(self, func, *args, **kwargs):
        """Make a Flickr API call with retry logic and rate limiting."""
        backoff = config.INITIAL_BACKOFF
        
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                with self.api_lock:
                    # Rate limiting delay
                    if self.last_call_time:
                        elapsed = time.time() - self.last_call_time
                        if elapsed < config.API_CALL_DELAY:
                            time.sleep(config.API_CALL_DELAY - elapsed)
                            
                    # Add timeout parameter to all API calls
                    if 'timeout' not in kwargs:
                        kwargs['timeout'] = 120  # Increase timeout to 120 seconds
                        
                    result = func(*args, **kwargs)
                    self.last_call_time = time.time()
                return result
                
            except flickrapi.exceptions.FlickrError as e:
                code = getattr(e, 'code', None)
                if code in [429, 503]:  # rate limit or server busy
                    print(f"⚠️ API rate limit hit or server busy, retry {attempt}/{config.MAX_RETRIES} after {backoff}s...")
                elif code:
                    print(f"⚠️ Flickr API status code: {code}, retry {attempt}/{config.MAX_RETRIES} after {backoff}s...")
                else:
                    print(f"⚠️ Flickr API error: {str(e)}, retry {attempt}/{config.MAX_RETRIES} after {backoff}s...")
                    
            except (RequestException, Timeout) as e:
                print(f"⚠️ Network error: {e}, retry {attempt}/{config.MAX_RETRIES} after {backoff}s...")
                # For network errors, use a longer backoff
                backoff = min(backoff * 2.5, config.MAX_BACKOFF)
                continue

            time.sleep(backoff)
            backoff = min(backoff * 2, config.MAX_BACKOFF)

        raise RuntimeError(f"API call failed after {config.MAX_RETRIES} retries.")

    def fetch_album_photos(self, flickr, album_id, user_id):
        """Fetch all photos from an album with pagination, respecting video download settings."""
        page = 1
        album_photos = []
        
        while True:
            photos_data = self.call_with_retries(
                flickr.photosets.getPhotos,
                photoset_id=album_id,
                user_id=user_id,
                extras="url_o,media",
                per_page=500,
                page=page
            )['photoset']

            # Filter out videos if video downloads are disabled
            if config.DOWNLOAD_VIDEO:
                album_photos.extend(photos_data['photo'])
            else:
                # Only include photos, skip videos
                for photo in photos_data['photo']:
                    media_type = photo.get('media', 'photo')
                    if media_type == 'photo':
                        album_photos.append(photo)
            
            if page >= photos_data['pages']:
                break
            page += 1
        
        return album_photos

    def fetch_unsorted_photos(self, flickr, user_id, all_album_photo_ids):
        """Fetch all unsorted photos (not in any album) with pagination."""
        page = 1
        unsorted_photo_ids = []
        
        while True:
            photos_data = self.call_with_retries(
                flickr.people.getPhotos,
                user_id=user_id,
                privacy_filter=1,
                media="all",
                page=page
            )['photos']

            for photo in photos_data['photo']:
                pid = photo['id']
                if pid not in all_album_photo_ids:
                    from ..utils.files import sanitize_filename
                    title = sanitize_filename(photo['title'] or pid)
                    unsorted_photo_ids.append((pid, title))

            if page >= photos_data['pages']:
                break
            page += 1
        
        return unsorted_photo_ids

    def get_original_url_and_info(self, flickr, photo_id, url_cache):
        """Get the best quality URL and info for a photo or video."""
        # Check cache first
        cache_key = f"{photo_id}_info"
        if cache_key in url_cache:
            return url_cache[cache_key]

        # Get photo info to determine media type
        photo_info = self.call_with_retries(flickr.photos.getInfo, photo_id=photo_id)['photo']
        media_type = photo_info.get('media', 'photo')  # 'photo' or 'video'
        
        if media_type == 'video' and not config.DOWNLOAD_VIDEO:
            # Skip video downloads if disabled
            return None
        
        original_url = None
        selected_info = None
        
        try:
            # Get all available sizes
            sizes = self.call_with_retries(flickr.photos.getSizes, photo_id=photo_id)['sizes']['size']
            
            if media_type == 'video':
                original_url, selected_info = self._select_best_video(sizes, photo_id)
            else:
                original_url, selected_info = self._select_best_photo(sizes, photo_id)

        except Exception as e:
            print_and_log(f"  ❌ Error getting sizes for {photo_id}: {e}", "ERROR")
            return None

        if not original_url:
            return None

        # Cache and save immediately
        result = {'url': original_url, 'media_type': media_type, 'selected_info': selected_info}
        url_cache[cache_key] = result
        save_json_file(config.url_cache_file, url_cache)
        return result

    def _select_best_video(self, sizes, photo_id):
        """Select the best video quality from available sizes."""
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
                
            print_and_log(f"    🎬 Selected video quality: {selected_info}")
            return original_url, selected_info
        else:
            print_and_log(f"    ❌ No video URLs found for {photo_id}", "ERROR")
            return None, None

    def _select_best_photo(self, sizes, photo_id):
        """Select the best photo quality from available sizes."""
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
            return original_url, selected_info
        else:
            print_and_log(f"    ❌ No image URLs found for {photo_id}", "ERROR")
            return None, None
