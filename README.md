# Flickr Downloader

This script downloads all your Flickr photos and videos in parallel threads, using the highest available resolution. If it hits the Flickr API limit, it will either retry or back off. All downloaded files are tracked. The script will not attempt to download files that have already been downloaded when you stop and then resume it. The 'Auto Upload' Flickr album is also included. Media files that are not part of an album or set will be downloaded to the 'Unsorted' directory.

Using Flickr Downloader is at your own risk. It is not affiliated with Flickr; it is a third-party project.

## Environment variables

Make sure you create an `.env` file and put these environment variables into it. Generate your personal Flickr API key and secret https://www.flickr.com/account/sharing. Feel free to adjust the other variables according to your needs. Make sure you don't exceed the Flickr API limit; this is why there is a delay between API calls. All variables except `API_KEY` and `API_SECRET` are optional and will use their default values if not specified.

```sh
API_KEY="your_api_key_here"
API_SECRET="your_api_secret_here"
DOWNLOAD_DIR="./flickr_downloads" # The location where all the files are downloaded
MAX_WORKERS=8 # Limit on parallel downloads
DOWNLOAD_VIDEO=true  # Set this to 'false' to skip video downloads
API_CALL_DELAY=1.1 # The minimum delay in seconds between Flickr API calls
```

## Video download limitations

Please note that this script only downloads compressed video files, not the originals. This is a limitation of the Flickr API. However, it ensures that the compressed file with the highest possible resolution is downloaded.

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
