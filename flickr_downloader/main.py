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
        album_summaries, album_ids = self._scan_albums(args, downloaded_ids)
        
        if not album_summaries:
            return
            
        # Process downloads
        result_summaries = self._process_downloads(
            args, album_summaries, album_ids, 
            url_cache, downloaded_ids
        )
        
        # Skip unsorted photos processing - only download from organized albums
        print_and_log("ℹ️ Skipping unsorted photos - downloading from organized albums only")
        
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
        """Scan all albums and prepare for downloads."""
        print_and_log("🔍 Scanning albums...")
        
        # Get list of all albums
        photosets = self.api_client.call_with_retries(
            self.flickr.photosets.getList, user_id=self.user_id
        )['photosets']['photoset']
        
        # Filter out skipped albums (Auto Upload and others from SKIP_ALBUMS)
        original_count = len(photosets)
        filtered_photosets = []
        skipped_albums = []
        
        for photoset in photosets:
            album_title = photoset['title']['_content']
            if config.should_skip_album(album_title):
                skipped_albums.append(album_title)
            else:
                filtered_photosets.append(photoset)
        
        photosets = filtered_photosets
        
        # Show what was skipped
        if skipped_albums:
            print_and_log(f"⏭️ Skipped {len(skipped_albums)} albums: {', '.join(skipped_albums)}")
        
        print_and_log(f"📊 Found {original_count} total albums, {len(photosets)} albums to process")
        
        # Filter albums if album parameter is provided
        photosets, album_ids = self._filter_albums(args, photosets)
        
        if not photosets:
            return {}, {}
        
        # Initialize progress spinner
        spinner = ProgressSpinner("Scanning albums...")
        spinner.start()
        
        # Create simple album summaries
        album_summaries = {}
        total_albums = len(photosets)
        
        for album_index, photoset in enumerate(photosets, 1):
            album_id = photoset['id']
            album_title = sanitize_filename(photoset['title']['_content'])
            album_ids[album_title] = album_id

            # Update progress spinner with current album
            spinner.update(create_spinner_message(album_index, total_albums, album_title))

            # Get album info to check photo/video counts before fetching all content
            album_info = self.api_client.call_with_retries(
                self.flickr.photosets.getInfo, photoset_id=album_id
            )['photoset']
            
            photo_count = int(album_info.get('count_photos', 0))
            video_count = int(album_info.get('count_videos', 0))
            
            # Skip albums that only contain videos when video downloads are disabled
            if not config.DOWNLOAD_VIDEO and photo_count == 0 and video_count > 0:
                print_and_log(f"⏭️ Skipping '{album_title}' - contains only {video_count} videos (DOWNLOAD_VIDEO=false)")
                continue
            elif not config.DOWNLOAD_VIDEO and video_count > 0:
                print_and_log(f"📊 Album '{album_title}': {photo_count} photos, {video_count} videos (videos will be skipped)")

            # Fetch all photos from this album
            album_photo_data = self.api_client.fetch_album_photos(self.flickr, album_id, self.user_id)
            
            # Create list of photos to download
            photos_to_download = []
            skipped_count = 0
            
            for photo in album_photo_data:
                pid = photo['id']
                title = sanitize_filename(photo['title'] or pid)
                
                if pid not in downloaded_ids:
                    photos_to_download.append((pid, title))
                else:
                    skipped_count += 1
                
                # Update spinner occasionally to show activity
                if len(photos_to_download) % 50 == 0:
                    spinner.update(create_spinner_message(album_index, total_albums, album_title))

            # Create album summary
            album_summaries[album_title] = {
                "album": album_title,
                "to_download": photos_to_download,
                "downloaded": 0,
                "skipped": skipped_count,
                "failed": 0
            }

            # Final update for this album
            spinner.update(create_spinner_message(album_index, total_albums, album_title))
        
        # Stop the spinner and show completion
        spinner.stop("✅ Album scanning completed!")
        
        return album_summaries, album_ids

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
                print_and_log("Available albums (excluding skipped albums):")
                # Get the original unfiltered list but apply skip filtering
                all_photosets = self.api_client.call_with_retries(
                    self.flickr.photosets.getList, user_id=self.user_id
                )['photosets']['photoset']
                
                # Filter out skipped albums from display list too
                available_albums = [ps for ps in all_photosets 
                                  if not config.should_skip_album(ps['title']['_content'])]
                
                for ps in available_albums[:20]:  # Show first 20 available albums
                    print_and_log(f"  - {ps['title']['_content']}")
                if len(available_albums) > 20:
                    print_and_log(f"  ... and {len(available_albums) - 20} more")
                return [], {}
            
            print_and_log("Matching albums:")
            for ps in photosets:
                print_and_log(f"  ✅ {ps['title']['_content']}")
            print_and_log("")
        
        return photosets, album_ids

    def _process_downloads(self, args, album_summaries, album_ids, url_cache, downloaded_ids):
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
                    albums_with_verification_issues
                )
            else:
                print_and_log(f"\n📂 Album: {album_title} - All {summary['skipped']} media files already downloaded")
                
                # For single album downloads, verify immediately
                self.verifier.handle_single_album_verification(
                    args, album_title, album_ids, self.flickr, downloaded_ids, 
                    albums_with_verification_issues
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
            album_ids, url_cache, downloaded_ids, result_summaries
        )
        
        return result_summaries

    def _handle_verification_issues(self, args, albums_with_verification_issues, album_summaries, 
                                   album_ids, url_cache, downloaded_ids, result_summaries):
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
                        downloaded_ids
                    )
                    
                    if not verification_passed:
                        # Save updated progress cache after resetting tracking
                        save_json_file(config.progress_file, {"downloaded_ids": list(downloaded_ids)})
                        print_and_log(f"     Saved updated progress cache", "INFO")
                        albums_with_verification_issues.append((album_title, album_ids[album_title]))

        # Handle albums that need retry downloads (with user confirmation)
        if albums_with_verification_issues:
            self._process_retry_downloads(args, albums_with_verification_issues, 
                                        url_cache, downloaded_ids, result_summaries)

    def _process_retry_downloads(self, args, albums_with_verification_issues,
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
                    downloaded_ids
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
