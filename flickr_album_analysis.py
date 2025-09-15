#!/usr/bin/env python3
"""
Flickr Album Analysis Script

Creates a CSV report comparing Flickr album metadata with local files.
Sorted by number of items (largest to smallest), excluding videos if DOWNLOAD_VIDEO=false.
Skips albums configured in SKIP_ALBUMS from .env file.

Output CSV format:
- Column 1: Album Name
- Column 2: Remote Items Count
- Column 3: Local Items Count  
- Column 4: Difference Flag (🚩 if different)
- Bottom row: Totals

Respects DOWNLOAD_VIDEO and SKIP_ALBUMS settings from .env file.
"""

import os
import sys
import csv
from collections import defaultdict
from dotenv import load_dotenv

# Add the current directory to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load environment variables
load_dotenv()

from flickr_downloader.api.client import FlickrAPIClient
from flickr_downloader import config
from flickr_downloader.utils.files import sanitize_filename
from flickr_downloader.utils.ui import ProgressSpinner, create_spinner_message
import flickrapi


def get_album_metadata():
    """Get all album metadata from Flickr using just album list API."""
    print("🔍 Fetching album metadata from Flickr...")
    
    # Initialize Flickr API
    flickr = flickrapi.FlickrAPI(config.API_KEY, config.API_SECRET, format='parsed-json')
    api_client = FlickrAPIClient()
    
    # Authenticate
    if not flickr.token_valid(perms='read'):
        flickr.get_request_token(oauth_callback='oob')
        authorize_url = flickr.auth_url(perms='read')
        print(f"Open this URL to authorize: {authorize_url}")
        verifier = input("Enter the verification code: ")
        flickr.get_access_token(verifier)

    # Get user info
    user_info = api_client.call_with_retries(flickr.test.login)
    user_id = user_info['user']['id']
    
    # Get all albums with metadata - this includes photo counts!
    photosets_response = api_client.call_with_retries(
        flickr.photosets.getList, user_id=user_id
    )
    photosets = photosets_response['photosets']['photoset']
    
    print(f"📋 Found {len(photosets)} albums on Flickr")
    
    album_data = []
    skipped_albums = []
    
    # Scan albums (skip those configured in SKIP_ALBUMS)
    for photoset in photosets:
        album_title = photoset['title']['_content']
        album_id = photoset['id']
        
        # Skip albums configured in SKIP_ALBUMS
        if config.should_skip_album(album_title):
            skipped_albums.append(album_title)
            continue
        
        # Get photo and video counts from album metadata
        photo_count = int(photoset.get('count_photos', 0))
        video_count = int(photoset.get('count_videos', 0))
        
        # Calculate item count based on DOWNLOAD_VIDEO setting
        if config.DOWNLOAD_VIDEO:
            item_count = photo_count + video_count
        else:
            item_count = photo_count  # Only photos
        
        album_info = {
            'id': album_id,
            'name': album_title,
            'photo_count': photo_count,
            'video_count': video_count,
            'remote_count': item_count
        }
        
        album_data.append(album_info)
    
    # Show what was skipped
    if skipped_albums:
        print(f"⏭️ Skipped {len(skipped_albums)} albums: {', '.join(skipped_albums)}")
    
    return album_data


def count_local_files():
    """Count local files in each album directory."""
    print("📁 Counting local files...")
    
    if not os.path.exists(config.DOWNLOAD_DIR):
        print(f"⚠️ Download directory not found: {config.DOWNLOAD_DIR}")
        return {}
    
    # Define video extensions
    video_extensions = {'.mp4', '.mov', '.avi', '.webm', '.mkv', '.flv', '.wmv', '.m4v', '.3gp'}
    
    local_counts = {}
    
    # Walk through each subdirectory (album folder)
    for item in os.listdir(config.DOWNLOAD_DIR):
        item_path = os.path.join(config.DOWNLOAD_DIR, item)
        if os.path.isdir(item_path):
            # Count files in this album directory
            file_count = 0
            for filename in os.listdir(item_path):
                file_path = os.path.join(item_path, filename)
                if os.path.isfile(file_path) and os.path.getsize(file_path) > 0:
                    # Check if we should exclude videos
                    if not config.DOWNLOAD_VIDEO:
                        ext = os.path.splitext(filename)[1].lower()
                        if ext in video_extensions:
                            continue  # Skip video files
                    file_count += 1
            
            local_counts[item] = file_count
    
    return local_counts


def find_matching_album(album_name, local_counts):
    """Find the best matching local directory for an album name."""
    # Try exact match first
    sanitized_name = sanitize_filename(album_name)
    if sanitized_name in local_counts:
        return sanitized_name, local_counts[sanitized_name]
    
    # Try case-insensitive match
    for local_name, count in local_counts.items():
        if local_name.lower() == sanitized_name.lower():
            return local_name, count
    
    # Try partial match (album name contains local name or vice versa)
    for local_name, count in local_counts.items():
        if (sanitized_name.lower() in local_name.lower() or 
            local_name.lower() in sanitized_name.lower()):
            return local_name, count
    
    return None, 0


def create_csv_report(album_data, local_counts, output_file):
    """Create the CSV report with comparison data."""
    print(f"📊 Creating CSV report: {output_file}")
    
    # Sort albums by remote count (largest to smallest)
    album_data.sort(key=lambda x: x['remote_count'], reverse=True)
    
    # Prepare report data
    report_rows = []
    total_remote = 0
    total_local = 0
    total_photos = 0
    total_videos = 0
    used_local_dirs = set()
    
    for album in album_data:
        album_name = album['name']
        remote_count = album['remote_count']
        photo_count = album.get('photo_count', 0)
        video_count = album.get('video_count', 0)
        
        # Find matching local directory
        local_dir, local_count = find_matching_album(album_name, local_counts)
        if local_dir:
            used_local_dirs.add(local_dir)
        
        # Check for differences
        flag = "🚩" if remote_count != local_count else ""
        
        # Create breakdown info
        breakdown = f"{photo_count}p"
        if video_count > 0:
            breakdown += f"+{video_count}v"
        
        report_rows.append([
            album_name,
            remote_count,
            breakdown,
            local_count,
            flag
        ])
        
        total_remote += remote_count
        total_local += local_count
        total_photos += photo_count
        total_videos += video_count
    
    # Add any local directories that don't match any albums
    unused_local_dirs = set(local_counts.keys()) - used_local_dirs
    for local_dir in sorted(unused_local_dirs):
        local_count = local_counts[local_dir]
        report_rows.append([
            f"[LOCAL ONLY] {local_dir}",
            0,
            "0p+0v",
            local_count,
            "🚩" if local_count > 0 else ""
        ])
        total_local += local_count
    
    # Write CSV file
    with open(output_file, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile)
        
        # Header
        writer.writerow(['Album Name', 'Remote Items', 'Breakdown (P+V)', 'Local Items', 'Diff Flag'])
        
        # Data rows
        for row in report_rows:
            writer.writerow(row)
        
        # Total row
        total_flag = "🚩" if total_remote != total_local else ""
        total_breakdown = f"{total_photos}p"
        if total_videos > 0:
            total_breakdown += f"+{total_videos}v"
        
        writer.writerow([
            'TOTAL',
            total_remote,
            total_breakdown,
            total_local,
            total_flag
        ])
    
    print(f"✅ Report saved to: {output_file}")
    print(f"📊 Summary: {total_remote:,} remote items ({total_photos:,} photos, {total_videos:,} videos), {total_local:,} local items")
    if total_remote != total_local:
        diff = abs(total_remote - total_local)
        print(f"⚠️ Difference: {diff:,} items")
    else:
        print("✅ Perfect match!")


def main():
    """Main function to run the album analysis."""
    try:
        print("🚀 Starting Flickr Album Analysis")
        print("=" * 50)
        
        # Check if videos are included
        video_status = "✅ Included" if config.DOWNLOAD_VIDEO else "❌ Excluded"
        print(f"📹 Video files: {video_status}")
        print(f"📂 Download directory: {config.DOWNLOAD_DIR}")
        print("")
        
        # Get album metadata from Flickr
        album_data = get_album_metadata()
        print(f"📋 Found {len(album_data)} albums on Flickr")
        
        # Count local files
        local_counts = count_local_files()
        print(f"📁 Found {len(local_counts)} local directories")
        
        # Create output filename
        video_suffix = "_with_videos" if config.DOWNLOAD_VIDEO else "_photos_only"
        output_file = f"flickr_album_analysis{video_suffix}.csv"
        
        # Generate report
        create_csv_report(album_data, local_counts, output_file)
        
        print("=" * 50)
        print("🎉 Analysis completed successfully!")
        
    except KeyboardInterrupt:
        print("\n⚠️ Analysis interrupted by user")
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
