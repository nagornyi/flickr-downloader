# Flickr Downloader

This script downloads all your Flickr photos and videos in parallel threads, using the highest available resolution. If it hits the Flickr API limit, it will either retry or back off. All downloaded files are tracked. The script will not attempt to download files that have already been downloaded. You can stop and resume this script multiple times. The 'Auto Upload' Flickr album is also included. Media files that are not part of an album or set will be downloaded to the 'Unsorted' directory.

This script allows you to create a local mirror of your Flickr collection. Simply run the script regularly to add missing albums and/or photos to your local mirror and keep it consistent with the Flickr cloud.

Attention! Using Flickr Downloader is at your own risk. It is not affiliated with Flickr; it is a third-party project. This script uses the open API from Flickr https://www.flickr.com/services/api/ and relies on the `flickrapi` package (https://pypi.org/project/flickrapi/).

## Set environment variables

Make sure you create an `.env` file and put the required environment variables into it. Generate your personal Flickr API key and secret https://www.flickr.com/account/sharing. Feel free to adjust the other variables according to your needs. Make sure you don't exceed the Flickr API limit; this is why there is a delay between API calls. To avoid hitting the 3,600 queries per hour limit or triggering Flickr server rate limiting or temporary blocks, we recommend keeping the default values of `API_CALL_DELAY` (If you encounter any API errors, try increasing this value) and `MAX_WORKERS` (If you hit diminishing returns or errors, lower this value). All variables except `API_KEY` and `API_SECRET` are optional and will use their default values if not specified.

```sh
API_KEY="your_api_key_here"
API_SECRET="your_api_secret_here"
DOWNLOAD_DIR="./flickr_downloads" # The location where all the files are downloaded
MAX_WORKERS=8 # Limit on parallel downloads
DOWNLOAD_VIDEO=true  # Set this to 'false' to skip video downloads
API_CALL_DELAY=1.1 # The minimum delay in seconds between Flickr API calls
```

## Video download limitations

Please note that this script only downloads compressed video files, not the originals. This is a limitation of the Flickr API. However, it ensures that the compressed file with the highest possible resolution is downloaded. Download videos from Flickr manually when you need them in their original quality and size.

## Installation and running

```sh
# 1. Create the virtual environment
python3 -m venv .venv

# 2. Activate it
source .venv/bin/activate

# 3. Upgrade pip (optional but recommended)
pip3 install --upgrade pip

# 4. Install requirements
pip3 install -r requirements.txt

# 5. Run the script
python3 flickr_downloader.py

# 6. Deactivate the virtual environment
deactivate
```

## Command Line Options

The script supports several command line options for more control over what gets downloaded:

### Download specific albums only

Use the `--album` (or `-a`) option to download only albums matching a specific pattern:

```sh
# Download all albums (default behavior)
python3 flickr_downloader.py

# Download only the "Vacation 2023" album
python3 flickr_downloader.py --album "Vacation 2023"

# Download albums starting with "Trip" (supports wildcards)
python3 flickr_downloader.py --album "Trip*"

# Download albums containing "2023" anywhere in the name
python3 flickr_downloader.py --album "*2023*"

# Short form of the option
python3 flickr_downloader.py -a "Family Photos"
```

**Wildcard support:**
- `*` matches any number of characters
- `?` matches any single character
- Use quotes around album names with spaces or special characters

**Examples:**
- `"Trip*"` → matches "Trip to Paris", "Trip 2023", "Tropical Vacation"
- `"*2023*"` → matches "Vacation 2023", "2023 Summer", "Photos 2023-2024"
- `"Family ??"` → matches "Family 01", "Family 02", but not "Family 123"

If no albums match your pattern, the script will show you a list of available albums to help you find the right name.

**Note:** When using the `--album` filter, unsorted photos (photos not in any album) will be skipped. This keeps the download focused on the specific album(s) you requested.

### Get help

```sh
python3 flickr_downloader.py --help
```

## Duplicate Detection

The downloader automatically detects and handles photos that appear in multiple albums to prevent duplicate downloads and save storage space.

### How It Works

1. **Two-Phase Scanning**: The script first scans all albums to map which photos appear where
2. **Smart Primary Location Selection**: For photos in multiple albums, it chooses the best location using this priority:
   - Prefers any manually created album over "Auto Upload"
   - If multiple manual albums contain the same photo, uses the first one found
   - Only uses "Auto Upload" if that's the only location

3. **Single Download**: Each photo is downloaded only once to its primary location, regardless of how many albums contain it

### Example

```
Photo "sunset.jpg" appears in:
├── "Auto Upload" 
├── "Vacation 2023"
└── "Best Photos"

→ Downloads to: "Vacation 2023" (avoids Auto Upload, uses first manual album)
```

### Benefits

- **Saves Storage**: No duplicate files on your disk
- **Saves Bandwidth**: Each photo downloaded only once
- **Smart Organization**: Respects your manual album organization over automatic uploads
- **Performance**: Significantly faster for users with many cross-album photos

### Output Example

```
📊 Found 25 media files that appear in multiple albums
  Wedding photo "ceremony.jpg" appears in: ["Wedding", "Family Photos", "Auto Upload"]
  → Will download to: "Wedding" (avoiding Auto Upload)
```

The duplicate detection is particularly valuable for Flickr users who use Auto Upload from mobile devices or organize photos into multiple themed albums
