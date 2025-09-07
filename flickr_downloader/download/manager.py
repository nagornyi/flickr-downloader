"""
Download manager for the Flickr downloader.
Handles file downloads, concurrent processing, and progress tracking.
"""
import os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import config
from ..utils.ui import print_and_log
from ..utils.files import sanitize_filename, save_json_file


def download_file(url, filepath, media_type=None):
    """Download a single file from URL to filepath."""
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
    except requests.exceptions.RequestException as e:
        error_msg = f"Network error downloading {os.path.basename(filepath)}: {str(e)}"
        return f"ERROR: {filepath} - {error_msg}"
    except IOError as e:
        error_msg = f"File I/O error for {os.path.basename(filepath)}: {str(e)}"
        return f"ERROR: {filepath} - {error_msg}"
    except Exception as e:
        error_msg = f"Unexpected error downloading {os.path.basename(filepath)}: {str(e)}"
        return f"ERROR: {filepath} - {error_msg}"


class DownloadManager:
    """Manages concurrent downloads and progress tracking."""
    
    def __init__(self, api_client):
        self.api_client = api_client
        
    def process_downloads(self, album_title, photo_ids, flickr, url_cache, downloaded_ids):
        """Process downloads for an entire album."""
        album_folder = os.path.join(config.DOWNLOAD_DIR, album_title)
        os.makedirs(album_folder, exist_ok=True)
        print_and_log(f"📂 Processing album: {album_title} ({len(photo_ids)} media files)")
        
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
        
        # Prepare download tasks
        download_tasks, skipped_count, failed_count = self._prepare_download_tasks(
            photo_ids, flickr, url_cache, downloaded_ids, album_folder
        )
        
        if not download_tasks:
            print(f"  ⚠️ No media files to download in this album. All {skipped_count} media files were skipped.")
            if photo_ids and skipped_count == 0 and failed_count == 0:
                print(f"  ⚠️ CRITICAL: No downloads were queued despite having {len(photo_ids)} media files.")
            return {"album": album_title, "downloaded": 0, "skipped": skipped_count, "failed": failed_count}
        
        print_and_log(f"  🔽 Downloading {len(download_tasks)} media files...")

        # Execute downloads concurrently
        downloaded_count, additional_failed = self._execute_downloads(download_tasks, downloaded_ids)
        failed_count += additional_failed

        # Save progress after album
        save_json_file(config.progress_file, {"downloaded_ids": list(downloaded_ids)})
        
        # Verify downloads
        files_in_dir = len(os.listdir(album_folder))
        print(f"  📊 Media files now in directory: {files_in_dir}")
        if downloaded_count > 0 and files_in_dir == 0:
            print(f"  ❌ CRITICAL: Media files were reported as downloaded but directory is empty!")
        
        return {
            "album": album_title,
            "downloaded": downloaded_count,
            "skipped": skipped_count,
            "failed": failed_count
        }
    
    def _prepare_download_tasks(self, photo_ids, flickr, url_cache, downloaded_ids, album_folder):
        """Prepare the list of download tasks."""
        download_tasks = []
        skipped_count = 0
        failed_count = 0
        
        for i, (photo_id, title) in enumerate(photo_ids):
            if photo_id in downloaded_ids:
                print(f"  ⏩ Skipping {title} (ID: {photo_id}) (marked as downloaded)")
                skipped_count += 1
                continue

            try:
                url_info = self.api_client.get_original_url_and_info(flickr, photo_id, url_cache)
                if not url_info:  # Skip if video downloads are disabled
                    skipped_count += 1
                    continue
                    
                url = url_info['url']
                media_type = url_info['media_type']
                
                # Log currently processed media file
                media_icon = "🎥" if media_type == 'video' else "📸"
                print_and_log(f"  {media_icon} Processing: {title} ({media_type})")
                
                # Use title + unique photo ID format: "title_uniqueid.extension"
                # Get file extension from URL or use defaults
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
                
                # Create filename: title_photoid.extension
                safe_title = sanitize_filename(title)
                filename = f"{safe_title}_{photo_id}{ext}"
                filepath = os.path.join(album_folder, filename)
                
                # Check if file already exists
                if os.path.exists(filepath):
                    print(f"  ⏩ Skipping {os.path.basename(filepath)} (file exists)")
                    skipped_count += 1
                    downloaded_ids.add(photo_id)
                    continue

                download_tasks.append((photo_id, url, filepath, media_type))
            except Exception as e:
                error_msg = f"Error preparing download for {title} (ID: {photo_id}): {str(e)}"
                print_and_log(f"  ❌ {error_msg}", "ERROR")
                failed_count += 1
        
        return download_tasks, skipped_count, failed_count
    
    def _execute_downloads(self, download_tasks, downloaded_ids):
        """Execute downloads concurrently and track results."""
        downloaded_count = 0
        failed_count = 0
        
        def download_task(task):
            photo_id, url, path, media_type = task
            try:
                result = download_file(url, path, media_type)
                return photo_id, result
            except Exception as e:
                return photo_id, f"ERROR: {path} - {e}"

        with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as executor:
            futures = [executor.submit(download_task, t) for t in download_tasks]
            
            for future in as_completed(futures):
                try:
                    photo_id, result = future.result()
                    if isinstance(result, str) and result.startswith("ERROR"):
                        # Extract the actual error message for better logging
                        error_parts = result.split(" - ", 1)
                        filename = os.path.basename(error_parts[0].replace('ERROR: ', ''))
                        error_detail = error_parts[1] if len(error_parts) > 1 else "Unknown error"
                        print_and_log(f"  ❌ Failed: {filename} - {error_detail}", "ERROR")
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
                            error_msg = f"File verification failed for {filename} - file is empty or missing"
                            print_and_log(f"  ⚠️ {error_msg}", "WARNING")
                            # Try to remove the empty/corrupted file
                            try:
                                if os.path.exists(result):
                                    os.remove(result)
                                    print_and_log(f"  🗑️ Removed empty file: {filename}", "DEBUG")
                            except Exception as cleanup_error:
                                print_and_log(f"  ⚠️ Could not remove empty file {filename}: {cleanup_error}", "WARNING")
                            failed_count += 1
                except Exception as e:
                    error_msg = f"Unexpected error processing download result: {str(e)}"
                    print_and_log(f"  ❌ {error_msg}", "ERROR")
                    failed_count += 1
        
        return downloaded_count, failed_count
