"""
Verification module for the Flickr downloader.
Handles album completion verification and duplicate detection.
"""
import os

from ..config import config
from ..utils.ui import print_and_log
from ..utils.files import save_json_file


class AlbumVerifier:
    """Handles verification of album download completion."""
    
    def __init__(self, api_client):
        self.api_client = api_client
    
    def verify_album_completion(self, album_title, album_id, flickr, downloaded_ids, duplicates_info=None):
        """
        Verify that an album download is complete by comparing Flickr counts with local files.
        Returns True if complete, False if missing files detected (and resets tracking).
        """
        try:
            # Get album info from Flickr to get exact photo/video counts
            album_info = self.api_client.call_with_retries(flickr.photosets.getInfo, photoset_id=album_id)['photoset']
            flickr_photos = int(album_info.get('count_photos', 0))
            flickr_videos = int(album_info.get('count_videos', 0))
            
            # Get all photo details to check for internal duplicates
            album_photos = self.api_client.fetch_album_photos(flickr, album_id, flickr.test.login()['user']['id'])
            
            # Count unique filenames (this is what we actually expect to download)
            unique_photo_ids = set()
            photos_only_count = 0
            videos_only_count = 0
            videos_skipped_count = 0
            
            for photo in album_photos:
                media_type = photo.get('media', 'photo')
                photo_id = photo['id']
                
                if media_type == 'photo':
                    photos_only_count += 1
                    unique_photo_ids.add(photo_id)
                elif media_type == 'video':
                    videos_only_count += 1
                    if config.DOWNLOAD_VIDEO:
                        unique_photo_ids.add(photo_id)
                    else:
                        videos_skipped_count += 1
            
            # Calculate expected local count based on unique photo IDs and settings
            expected_local_count = len(unique_photo_ids)
            
            # If we have duplicate info, subtract duplicates that should be in other albums
            duplicates_in_other_albums = 0
            if duplicates_info:
                duplicates_in_other_albums = self._calculate_cross_album_duplicates(
                    duplicates_info, album_title
                )
            
            expected_local_count -= duplicates_in_other_albums
            
            # Count actual local files (excluding videos if they're disabled)
            actual_local_count = self._count_local_files(album_title)
            
            # Check for differences and handle accordingly
            return self._evaluate_verification_results(
                album_title, expected_local_count, actual_local_count,
                flickr_photos, flickr_videos, videos_skipped_count,
                duplicates_in_other_albums, album_photos, downloaded_ids
            )
            
        except Exception as e:
            print_and_log(f"  ❌ Album verification error for {album_title}: {e}", "ERROR")
            return True  # Don't reset on verification errors
    
    def handle_single_album_verification(self, args, album_title, album_ids, flickr, downloaded_ids, photo_locations, albums_with_verification_issues):
        """
        Handle verification for single album downloads with user confirmation.
        Returns True if verification passed, False otherwise.
        """
        if args.album and album_title in album_ids:
            print_and_log(f"🔍 Verifying album completion: {album_title}")
            verification_passed = self.verify_album_completion(
                album_title, 
                album_ids[album_title], 
                flickr, 
                downloaded_ids, 
                photo_locations
            )
            
            if not verification_passed:
                # Save updated progress cache after resetting tracking
                save_json_file(config.progress_file, {"downloaded_ids": list(downloaded_ids)})
                print_and_log(f"     Saved updated progress cache", "INFO")
                
                # Ask user if they want to retry
                response = input(f"     🤔 Do you want to retry downloading missing files for '{album_title}'? (y/n): ").strip().lower()
                if response in ['y', 'yes']:
                    albums_with_verification_issues.append((album_title, album_ids[album_title]))
            
            return verification_passed
        
        return True  # No verification needed for multi-album downloads
    
    def _calculate_cross_album_duplicates(self, duplicates_info, album_title):
        """Calculate how many duplicates should be in other albums."""
        duplicates_in_other_albums = 0
        
        for photo_id, locations in duplicates_info.items():
            if len(locations) > 1 and album_title in locations:
                # This photo appears in multiple albums
                # Check if this album is the primary location
                primary_album = None
                for loc in locations:
                    if not loc.startswith("Auto Upload"):  # Prefer non-Auto Upload
                        primary_album = loc
                        break
                if not primary_album:
                    primary_album = locations[0]  # Fallback to first
                
                # If this album is not the primary, we expect one less file here
                if album_title != primary_album:
                    duplicates_in_other_albums += 1
        
        return duplicates_in_other_albums
    
    def _count_local_files(self, album_title):
        """Count actual local files in the album folder."""
        album_folder = os.path.join(config.DOWNLOAD_DIR, album_title)
        actual_local_count = 0
        
        if os.path.exists(album_folder):
            for filename in os.listdir(album_folder):
                file_path = os.path.join(album_folder, filename)
                if os.path.isfile(file_path) and os.path.getsize(file_path) > 0:
                    # Skip video files if video downloads are disabled
                    if not config.DOWNLOAD_VIDEO:
                        ext = os.path.splitext(filename)[1].lower()
                        if ext in ['.mp4', '.mov', '.avi', '.webm', '.mkv', '.flv', '.wmv']:
                            continue
                    actual_local_count += 1
        
        return actual_local_count
    
    def _evaluate_verification_results(self, album_title, expected_local_count, actual_local_count,
                                     flickr_photos, flickr_videos, videos_skipped_count,
                                     duplicates_in_other_albums, album_photos, downloaded_ids):
        """Evaluate verification results and take appropriate action."""
        difference = expected_local_count - actual_local_count
        
        if difference > 0:
            print_and_log(f"  ⚠️ Album verification failed: {album_title}", "WARNING")
            print_and_log(f"     Expected: {expected_local_count} files, Found: {actual_local_count} files", "WARNING")
            print_and_log(f"     Flickr: {flickr_photos} photos, {flickr_videos} videos", "DEBUG")
            if videos_skipped_count > 0:
                print_and_log(f"     Note: {videos_skipped_count} videos excluded (DOWNLOAD_VIDEO=false)", "INFO")
            if duplicates_in_other_albums > 0:
                print_and_log(f"     Cross-album duplicates: {duplicates_in_other_albums}", "DEBUG")
            
            # Reset tracking for this album by removing all its photo IDs from downloaded_ids
            try:
                album_photo_ids = set(photo['id'] for photo in album_photos)
                
                # Remove these IDs from downloaded_ids
                removed_count = len(album_photo_ids.intersection(downloaded_ids))
                downloaded_ids.difference_update(album_photo_ids)
                
                print_and_log(f"     Reset tracking for {removed_count} files in {album_title}", "INFO")
                return False
                
            except Exception as e:
                print_and_log(f"     Failed to reset tracking for {album_title}: {e}", "ERROR")
                return False
        
        elif difference == 0:
            if videos_skipped_count > 0:
                print_and_log(f"  ✅ Album verification passed: {album_title} ({actual_local_count} files, {videos_skipped_count} videos excluded)", "INFO")
            else:
                print_and_log(f"  ✅ Album verification passed: {album_title} ({actual_local_count} files)", "DEBUG")
            return True
        
        else:  # difference < 0 (more local files than expected)
            extra_files = -difference
            if videos_skipped_count > 0:
                print_and_log(f"  ℹ️ Album has extra files: {album_title} (+{extra_files} files, {videos_skipped_count} videos excluded)", "INFO")
            else:
                print_and_log(f"  ℹ️ Album has extra files: {album_title} (+{extra_files} files)", "INFO")
            print_and_log(f"  ✅ Album verification passed: {album_title} ({actual_local_count} files)", "DEBUG")
            return True
