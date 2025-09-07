"""
Main application module for the Flickr downloader.
Orchestrates the entire download process.
"""
import os
import flickrapi

from .config import config
from .utils.ui import print_and_log, ProgressSpinner, create_spinner_message, setup_logging
from .utils.files import load_json_file, save_json_file, sanitize_filename
from .api.client import FlickrAPIClient
from .download.manager import DownloadManager
from .verification.checker import AlbumVerifier
from .cli import parse_arguments, filter_albums_by_pattern


class FlickrDownloaderApp:
    """Main application class that orchestrates the download process."""
    
    def __init__(self):
        self.api_client = FlickrAPIClient()
        self.download_manager = DownloadManager(self.api_client)
        self.verifier = AlbumVerifier(self.api_client)
        self.flickr = None
        self.user_id = None
        
    def run(self):
        """Run the main application."""
        # Parse command line arguments
        args = parse_arguments()
        
        # Setup logging
        setup_logging()
        
        print_and_log("🚀 Starting Flickr Downloader")
        print_and_log("=" * 50)
        
        if args.album:
            print_and_log(f"📂 Filtering albums: '{args.album}'")
            print_and_log("=" * 50)
        
        # Validate configuration
        try:
            config.validate()
        except ValueError as e:
            print_and_log(f"❌ ERROR: {e}", "ERROR")
            return
        
        print_and_log(f"ℹ️ Video downloads: {'✅ Enabled' if config.DOWNLOAD_VIDEO else '❌ Disabled'}")
        print_and_log("")
        
        # Initialize Flickr API
        if not self._initialize_flickr_api():
            return
            
        # Setup directories and load cache
        os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
        os.makedirs(config.CACHE_DIR, exist_ok=True)

        url_cache = load_json_file(config.url_cache_file)
        progress = load_json_file(config.progress_file)
        downloaded_ids = set(progress.get("downloaded_ids", []))

        # Scan albums and process downloads
        album_summaries, album_ids, photo_locations = self._scan_albums(args, downloaded_ids)
        
        if not album_summaries:
            return
            
        # Process downloads
        result_summaries = self._process_downloads(
            args, album_summaries, album_ids, photo_locations, 
            url_cache, downloaded_ids
        )
        
        # Handle unsorted photos if not filtering by album
        if not args.album:
            self._process_unsorted_photos(url_cache, downloaded_ids, result_summaries)
        
        # Final summary and verification
        self._show_final_summary(args, result_summaries)
        
        print_and_log("=" * 50)

    def _initialize_flickr_api(self):
        """Initialize and authenticate with Flickr API."""
        self.flickr = flickrapi.FlickrAPI(config.API_KEY, config.API_SECRET, format='parsed-json')
        
        if not self.flickr.token_valid(perms='read'):
            self.flickr.get_request_token(oauth_callback='oob')
            authorize_url = self.flickr.auth_url(perms='read')
            print_and_log(f"Open this URL to authorize: {authorize_url}")
            verifier = input("Enter the verification code: ")
            self.flickr.get_access_token(verifier)

        user_info = self.api_client.call_with_retries(self.flickr.test.login)
        self.user_id = user_info['user']['id']
        return True

    def _scan_albums(self, args, downloaded_ids):
        """Scan all albums and identify duplicates."""
        print_and_log("🔍 Scanning albums to identify duplicate media files...")
        
        all_album_photo_ids = set()
        photo_locations = {}  # Maps photo_id -> list of albums containing it
        album_photos = {}     # Maps album_title -> list of (photo_id, title) tuples
        
        # Get list of all albums
        photosets = self.api_client.call_with_retries(
            self.flickr.photosets.getList, user_id=self.user_id
        )['photosets']['photoset']
        
        # Filter albums if album parameter is provided
        photosets, album_ids = self._filter_albums(args, photosets)
        
        if not photosets:
            return {}, {}, {}
        
        # Initialize progress spinner
        spinner = ProgressSpinner("Scanning albums...")
        spinner.start()
        
        # First pass: collect all photos and their locations
        total_albums = len(photosets)
        for album_index, photoset in enumerate(photosets, 1):
            album_id = photoset['id']
            album_title = sanitize_filename(photoset['title']['_content'])
            album_ids[album_title] = album_id
            photo_ids = []

            # Update progress spinner with current album
            spinner.update(create_spinner_message(album_index, total_albums, album_title))

            # Fetch all photos from this album
            album_photo_data = self.api_client.fetch_album_photos(self.flickr, album_id, self.user_id)
            
            for photo in album_photo_data:
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

            # Final update for this album
            spinner.update(create_spinner_message(album_index, total_albums, album_title))
            album_photos[album_title] = photo_ids
        
        # Stop the spinner and show completion
        spinner.stop("✅ Album scanning completed!")
        
        # Process duplicates and create download plan
        album_summaries = self._process_duplicates_and_create_plan(
            photo_locations, album_photos, downloaded_ids
        )
        
        return album_summaries, album_ids, photo_locations

    def _filter_albums(self, args, photosets):
        """Filter albums based on command line arguments."""
        album_ids = {}
        
        if args.album:
            original_count = len(photosets)
            photosets = filter_albums_by_pattern(photosets, args.album)
            filtered_count = len(photosets)
            
            print_and_log(f"📊 Found {original_count} total albums, {filtered_count} match filter '{args.album}'")
            
            if filtered_count == 0:
                print_and_log(f"❌ No albums found matching pattern '{args.album}'")
                print_and_log("Available albums:")
                all_photosets = self.api_client.call_with_retries(
                    self.flickr.photosets.getList, user_id=self.user_id
                )['photosets']['photoset']
                for ps in all_photosets[:20]:  # Show first 20 albums
                    print_and_log(f"  - {ps['title']['_content']}")
                if len(all_photosets) > 20:
                    print_and_log(f"  ... and {len(all_photosets) - 20} more")
                return [], {}
            
            print_and_log("Matching albums:")
            for ps in photosets:
                print_and_log(f"  ✅ {ps['title']['_content']}")
            print_and_log("")
        
        return photosets, album_ids

    def _process_duplicates_and_create_plan(self, photo_locations, album_photos, downloaded_ids):
        """Process duplicates and create download plan."""
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
        
        # Create album summaries
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
        
        return album_summaries

    def _process_downloads(self, args, album_summaries, album_ids, photo_locations, url_cache, downloaded_ids):
        """Process downloads for all albums."""
        result_summaries = []
        albums_with_verification_issues = []
        
        for album_title, summary in album_summaries.items():
            album_result = None
            
            if summary["to_download"]:
                result = self.download_manager.process_downloads(
                    album_title, summary["to_download"], self.flickr, url_cache, downloaded_ids
                )
                album_result = result
                
                # For single album downloads, verify immediately
                self.verifier.handle_single_album_verification(
                    args, album_title, album_ids, self.flickr, downloaded_ids, 
                    photo_locations, albums_with_verification_issues
                )
            else:
                print_and_log(f"\n📂 Album: {album_title} - All {summary['skipped']} media files already downloaded")
                
                # For single album downloads, verify immediately
                self.verifier.handle_single_album_verification(
                    args, album_title, album_ids, self.flickr, downloaded_ids, 
                    photo_locations, albums_with_verification_issues
                )
                
                album_result = {
                    "album": album_title,
                    "downloaded": 0,
                    "skipped": summary["skipped"],
                    "failed": 0
                }
            
            if album_result:
                result_summaries.append(album_result)
        
        # Handle verification issues and retries
        self._handle_verification_issues(
            args, albums_with_verification_issues, album_summaries, 
            album_ids, photo_locations, url_cache, downloaded_ids, result_summaries
        )
        
        return result_summaries

    def _handle_verification_issues(self, args, albums_with_verification_issues, album_summaries, 
                                   album_ids, photo_locations, url_cache, downloaded_ids, result_summaries):
        """Handle albums that need verification and potential retries."""
        # For full downloads (no --album parameter), verify all albums after all downloads
        if not args.album:
            print_and_log("\n🔍 Verifying completion of all albums...")
            for album_title in album_summaries.keys():
                if album_title in album_ids:
                    print_and_log(f"🔍 Verifying album completion: {album_title}")
                    verification_passed = self.verifier.verify_album_completion(
                        album_title, 
                        album_ids[album_title], 
                        self.flickr, 
                        downloaded_ids, 
                        photo_locations
                    )
                    
                    if not verification_passed:
                        # Save updated progress cache after resetting tracking
                        save_json_file(config.progress_file, {"downloaded_ids": list(downloaded_ids)})
                        print_and_log(f"     Saved updated progress cache", "INFO")
                        albums_with_verification_issues.append((album_title, album_ids[album_title]))

        # Handle albums that need retry downloads (with user confirmation)
        if albums_with_verification_issues:
            self._process_retry_downloads(args, albums_with_verification_issues, photo_locations, 
                                        url_cache, downloaded_ids, result_summaries)

    def _process_retry_downloads(self, args, albums_with_verification_issues, photo_locations,
                               url_cache, downloaded_ids, result_summaries):
        """Process retry downloads for albums with verification issues."""
        if not args.album:
            # For full downloads, ask once for all albums with issues
            album_names = [name for name, _ in albums_with_verification_issues]
            print_and_log(f"\n⚠️  Found {len(albums_with_verification_issues)} albums with missing files:")
            for name in album_names:
                print_and_log(f"    📂 {name}")
            
            response = input(f"\n🤔 Do you want to retry downloading missing files for these albums? (y/n): ").strip().lower()
            if response not in ['y', 'yes']:
                albums_with_verification_issues = []
        
        # Process retry downloads for confirmed albums
        for album_title, album_id in albums_with_verification_issues:
            print_and_log(f"🔄 Retrying download for album with missing files: {album_title}")
            
            # Re-scan this album to find files that need downloading
            retry_photos = []
            
            # Fetch all photos from this album
            retry_album_data = self.api_client.fetch_album_photos(self.flickr, album_id, self.user_id)
            
            for photo in retry_album_data:
                pid = photo['id']
                title = sanitize_filename(photo['title'] or pid)
                
                # Only add if not in downloaded_ids (after reset)
                if pid not in downloaded_ids:
                    retry_photos.append((pid, title))
            
            if retry_photos:
                print_and_log(f"     Found {len(retry_photos)} files to retry")
                retry_result = self.download_manager.process_downloads(
                    album_title, retry_photos, self.flickr, url_cache, downloaded_ids
                )
                
                # Update the result summary for this album
                for summary in result_summaries:
                    if summary["album"] == album_title:
                        summary["downloaded"] += retry_result["downloaded"]
                        summary["failed"] += retry_result["failed"]
                        break
                
                # Verify again after retry
                print_and_log(f"🔍 Re-verifying album after retry: {album_title}")
                final_verification = self.verifier.verify_album_completion(
                    album_title, 
                    album_id, 
                    self.flickr, 
                    downloaded_ids, 
                    photo_locations
                )
                
                if final_verification:
                    print_and_log(f"     ✅ Album verification passed after retry: {album_title}")
                else:
                    print_and_log(f"     ⚠️ Album still has missing files after retry: {album_title}", "WARNING")
            else:
                print_and_log(f"     No files found to retry (this may indicate a verification logic issue)")

    def _process_unsorted_photos(self, url_cache, downloaded_ids, result_summaries):
        """Process unsorted photos that aren't in any album."""
        print_and_log("📂 Processing unsorted media files...")
        
        # Get all album photo IDs to exclude from unsorted
        all_album_photo_ids = set()
        for summary in result_summaries:
            # This is a simplified approach; in a real implementation, 
            # we'd need to track all album photo IDs properly
            pass
        
        unsorted_photo_ids = self.api_client.fetch_unsorted_photos(
            self.flickr, self.user_id, all_album_photo_ids
        )

        if unsorted_photo_ids:
            summary = self.download_manager.process_downloads(
                "Unsorted", unsorted_photo_ids, self.flickr, url_cache, downloaded_ids
            )
            result_summaries.append(summary)
        else:
            print_and_log("\n📂 No media files found outside of albums.")

    def _show_final_summary(self, args, result_summaries):
        """Show final download summary and perform account verification."""
        print_and_log("\n📊 Download Summary:")
        
        # Separate successful and failed albums
        successful_albums = []
        failed_albums = []
        total_downloaded = 0
        total_skipped = 0
        total_failed = 0
        
        for summary in result_summaries:
            total_downloaded += summary['downloaded']
            total_skipped += summary['skipped']
            total_failed += summary['failed']
            
            if summary['failed'] > 0:
                failed_albums.append(summary)
            else:
                successful_albums.append(summary)
        
        # Show successful albums first
        if successful_albums:
            print_and_log("  ✅ Albums completed successfully:")
            for summary in successful_albums:
                print_and_log(f"    📂 {summary['album']}: {summary['downloaded']} downloaded, {summary['skipped']} skipped")
        
        # Show failed albums in a separate section
        if failed_albums:
            print_and_log("  ❌ Albums with failures:")
            for summary in failed_albums:
                print_and_log(f"    📂 {summary['album']}: {summary['downloaded']} downloaded, "
                      f"{summary['skipped']} skipped, {summary['failed']} failed")
        
        # Overall totals
        print_and_log(f"\n📈 Overall totals: {total_downloaded} downloaded, {total_skipped} skipped, {total_failed} failed")
        
        # Final verification for full downloads
        if not args.album:
            self._perform_final_account_verification(total_failed)

    def _perform_final_account_verification(self, total_failed):
        """Perform final account-wide verification."""
        print_and_log("\n🔍 Performing final account verification...")
        try:
            # Get total items count from Flickr account
            person_info = self.api_client.call_with_retries(
                self.flickr.people.getInfo, user_id=self.user_id
            )['person']
            flickr_photos_count = int(person_info.get('photos', {}).get('count', 0))
            flickr_videos_count = int(person_info.get('videos', {}).get('count', 0))
            
            print_and_log(f"  📊 Flickr account totals: {flickr_photos_count:,} photos, {flickr_videos_count:,} videos")
            
            # Calculate expected local count based on settings
            expected_flickr_total = flickr_photos_count
            if config.DOWNLOAD_VIDEO:
                expected_flickr_total += flickr_videos_count
                print_and_log(f"  📊 Expected local files (photos + videos): {expected_flickr_total:,}")
            else:
                print_and_log(f"  📊 Expected local files (photos only): {expected_flickr_total:,}")
            
            # Count actual local files
            actual_local_count = 0
            if os.path.exists(config.DOWNLOAD_DIR):
                for root, dirs, files in os.walk(config.DOWNLOAD_DIR):
                    for filename in files:
                        file_path = os.path.join(root, filename)
                        if os.path.isfile(file_path) and os.path.getsize(file_path) > 0:
                            # If video downloads are disabled, skip video files
                            if not config.DOWNLOAD_VIDEO:
                                ext = os.path.splitext(filename)[1].lower()
                                if ext in ['.mp4', '.mov', '.avi', '.webm', '.mkv', '.flv', '.wmv']:
                                    continue
                            actual_local_count += 1
            
            print_and_log(f"  📊 Actual local files found: {actual_local_count:,}")
            
            # Compare and report
            difference = expected_flickr_total - actual_local_count
            if difference == 0:
                print_and_log(f"  ✅ Perfect match! All {actual_local_count:,} files are accounted for.")
            elif difference > 0:
                print_and_log(f"  ⚠️ Missing {difference:,} files locally (Expected: {expected_flickr_total:,}, Found: {actual_local_count:,})")
                print_and_log(f"     💡 This might be due to:")
                print_and_log(f"        • Private photos not accessible via API")
                print_and_log(f"        • Photos deleted from Flickr but still counted")
                print_and_log(f"        • Failed downloads from previous runs")
                print_and_log(f"        • Network issues during download")
            else:
                extra_files = -difference
                print_and_log(f"  ℹ️ Found {extra_files:,} extra local files (Expected: {expected_flickr_total:,}, Found: {actual_local_count:,})")
                print_and_log(f"     💡 This might be due to:")
                print_and_log(f"        • Duplicate downloads from different albums")
                print_and_log(f"        • Files added manually to the download directory")
                print_and_log(f"        • Previous downloads with different naming schemes")
                
        except Exception as e:
            print_and_log(f"  ❌ Could not perform final verification: {e}", "ERROR")
        
        # Final status message
        if total_failed > 0:
            print_and_log(f"\n⚠️  Some downloads failed. You may want to run the script again to retry failed downloads.")
            print_and_log(f"💡 Failed downloads are often due to temporary network issues and usually succeed on retry.")
        else:
            print_and_log(f"\n🎉 All downloads completed successfully! No failures detected.")


def main():
    """Main entry point for the application."""
    app = FlickrDownloaderApp()
    app.run()


if __name__ == "__main__":
    main()
