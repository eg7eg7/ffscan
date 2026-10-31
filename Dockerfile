# ffscan in a container: an easy way to run the app without installing Python, exiftool or rclone.
#
# The macOS-only extras are unavailable in the container:
#   - face-based auto-rotate and red-eye detection (Apple Vision)
#   - Bonjour discovery: the app scans the subnet given in FFSCAN_SUBNET, or use FFSCAN_SCANNER
#   - the native folder picker and Keychain: type the folder path; pass ANTHROPIC_API_KEY as an env var
#   - browser sign-in for Google Drive / Dropbox / OneDrive: connect them with ./run.sh on the host once, or
#     mount an existing rclone config (see docker-compose.yml)
# Everything else works: scanning, crop, enhancement, duplicates, metadata, AI analysis, S3 and rclone uploads.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends libimage-exiftool-perl rclone ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ffscan ffscan

ENV FFSCAN_BIND=0.0.0.0 \
    FFSCAN_PORT=8680 \
    FFSCAN_DATA_DIR=/data \
    FFSCAN_CACHE_DIR=/cache \
    FFSCAN_PHOTOS_DIR=/photos \
    PYTHONUNBUFFERED=1

VOLUME ["/photos", "/data"]
EXPOSE 8680
CMD ["python", "-m", "ffscan.app"]
