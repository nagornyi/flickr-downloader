# Flickr Downloader

This script will download all your files from your Flickr account in parallel threads, and it'll respect the Flickr API limit. If it hits that limit, it'll just retry or back off.

## Environment variables in `.env` file

```sh
API_KEY="your_api_key_here"
API_SECRET="your_api_secret_here"
DOWNLOAD_DIR="./flickr_downloads"
MAX_WORKERS=8
```

## Installation and running

```sh
pip3 install -r requirements.txt
python3 flickr_downloader.py
```
