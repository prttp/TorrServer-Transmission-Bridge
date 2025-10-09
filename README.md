# TorrServer Transmission Bridge

A Transmission RPC-compatible bridge that allows Sonarr/Radarr to manage torrents in TorrServer while enabling Jellyfin to stream directly from TorrServer without storing files locally.

## Overview

This bridge acts as a Transmission download client for Sonarr/Radarr, but instead of downloading files to disk, it:
1. Adds torrents to TorrServer
2. Generates `.strm` files that point to TorrServer streaming URLs
3. Allows Sonarr/Radarr to import these `.strm` files as if they were actual media files
4. Enables Jellyfin to play content by streaming directly from TorrServer

**Key Features:**
- ?? No local storage needed - stream torrents on-demand
- ?? Full Sonarr/Radarr integration with metadata
- ?? Works with Jellyfin for seamless playback
- ?? Automatic cleanup of orphaned `.strm` files
- ?? Built-in TorrServer diagnostics and monitoring

## Architecture

```
Sonarr/Radarr ? (Transmission RPC) ? ts-bridge ? (REST API) ? TorrServer
                                          ?
                                    .strm files
                                          ?
                                      Jellyfin ? TorrServer stream
```

## Quick Start

### Docker Compose Example

```yaml
version: "3"
services:
  torrserver:
    image: ghcr.io/yourok/torrserver:latest
    container_name: torrserver
    ports:
      - "5665:5665"
    volumes:
      - torrserver-data:/opt/ts
    restart: unless-stopped

  ts-bridge:
    image: prttp/ts-bridge:latest
    container_name: ts-bridge
    ports:
      - "9091:9091"  # Transmission RPC
    environment:
      - TORRSERVER_URL=http://torrserver:5665
      
      # Optional: automatic cleanup of orphaned .strm files
      # Uncomment to enable (disabled by default):
      # - STRM_CLEANUP_PATHS=/data/downloads/movies,/data/downloads/tv
      # - STRM_CLEANUP_INTERVAL=3600  # 1 hour in seconds
      
      # Optional: Basic authentication
      # - BRIDGE_USERNAME=admin
      # - BRIDGE_PASSWORD=secret
      
      # Optional: logging
      # - LOG_LEVEL=INFO
    volumes:
      - /path/to/data:/data
      - ts-bridge-state:/data/ts-bridge  # Stores ids.json and metadata
    restart: unless-stopped
    depends_on:
      - torrserver

volumes:
  ts-bridge-state:
  torrserver-data:
```

> **Note:** For a complete setup example including Sonarr, Radarr, Jellyfin, and VPN (Gluetun), see [All-jellyfin-media-server](https://github.com/Morzomb/All-jellyfin-media-server) - a comprehensive media server setup with detailed configuration guide.

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TORRSERVER_URL` | `http://127.0.0.1:5666` | TorrServer API endpoint |
| `BRIDGE_DOWNLOAD_DIR` | `/downloads` | Base download directory |
| `STRM_CLEANUP_PATHS` | _(empty)_ | Comma-separated paths to scan for orphaned `.strm` files. If empty, cleanup is **disabled**. Also protects listed paths from deletion. |
| `STRM_CLEANUP_INTERVAL` | `3600` | Cleanup interval in seconds (1 hour) |
| `BRIDGE_USERNAME` | _(empty)_ | Optional: Basic auth username |
| `BRIDGE_PASSWORD` | _(empty)_ | Optional: Basic auth password |
| `LOG_LEVEL` | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### Sonarr/Radarr Setup

1. **Add Download Client:**
   - Settings ? Download Clients ? Add ? Transmission
   - Host: `127.0.0.1` (or your bridge host)
  - Port: `9091`
  - SSL: off
   - Username/Password: _(if configured)_
   - **Directory:** Set the download path (required!)
    - Sonarr: `/data/downloads/tv`
    - Radarr: `/data/downloads/movies`
   - **Category:** Leave empty (bridge auto-detects from path)

2. **Enable Completed Download Handling:**
   - Settings ? Download Clients
   - Check "Enable Completed Download Handling"
   - Check "Remove" under "Completed Download Handling"

3. **Import Settings:**
   - The bridge creates `.strm` files with original filenames
   - Sonarr/Radarr import these files to their libraries with full metadata
   - After import, `.strm` files are removed from `/data/downloads`
   - **Important:** Torrents remain in TorrServer for streaming!

### Jellyfin Setup

1. **Add Libraries:**
   - Use the same paths as Sonarr/Radarr root folders
   - TV Shows: `/data/sonarr/tv`
   - Movies: `/data/radarr/movies`

2. **Playback:**
   - Jellyfin will find the imported `.strm` files
   - Clicking play opens the TorrServer stream URL
   - Content streams on-demand without local storage

## How It Works

1. **Torrent Added:**
   - Sonarr/Radarr sends torrent to bridge via Transmission RPC
   - Bridge adds torrent to TorrServer
   - Bridge creates `.strm` files in download directory with original filenames
   - Each `.strm` contains: `http://torrserver:5665/play/{hash}/{file_id}`

2. **Import:**
   - Sonarr/Radarr detect "completed download" (bridge reports 100% immediately)
   - Files are imported to library with proper naming and metadata
   - Bridge receives `torrent-remove` request with `delete-local-data=true`

3. **Cleanup:**
   - Bridge deletes `.strm` files from `/data/downloads`
   - Empty directories are removed
   - **Torrent stays in TorrServer** - needed for streaming!

4. **Streaming:**
   - Jellyfin plays `.strm` file from library
   - TorrServer streams the torrent on-demand
   - No local storage required

## Orphaned .strm Cleanup

The bridge can automatically clean up `.strm` files pointing to deleted torrents.

**Enable cleanup:**
```yaml
environment:
  - STRM_CLEANUP_PATHS=/data/downloads/movies,/data/downloads/tv
  - STRM_CLEANUP_INTERVAL=3600
```

**How it works:**
1. Scans all `.strm` files in specified paths
2. Extracts torrent hash from URL
3. Checks if torrent exists in TorrServer
4. Removes orphaned files and empty directories
5. **Protects** paths listed in `STRM_CLEANUP_PATHS` from deletion

**Manual cleanup:**
```bash
curl -X POST http://localhost:9091/strm/cleanup
```

**Response:**
```json
{
  "scanned_files": 150,
  "orphaned_found": 3,
  "deleted_files": 3,
  "deleted_dirs": 5,
  "errors": 0,
  "torrents_in_server": 25
}
```

## API Reference

### Transmission RPC

- `GET/POST /transmission/rpc` - Standard Transmission RPC endpoint
- Supported methods:
  - `session-get` - Get session info
  - `session-stats` - Get statistics
  - `torrent-add` - Add torrent (generates `.strm` files)
  - `torrent-get` - Get torrent info
  - `torrent-remove` - Remove torrent (cleans up `.strm` files)
  - `torrent-start` - No-op (not needed for TorrServer)
  - `torrent-stop` - No-op (not needed for TorrServer)

### TorrServer Diagnostics

**Check torrent status:**
```bash
curl http://localhost:9091/torrserver/status | jq .
```

**Response:**
```json
{
  "total_torrents": 10,
  "active_count": 2,
  "inactive_count": 8,
  "active_torrents": [
    {
      "hash": "abc12345",
      "name": "Series S01E01",
      "status": "Working",
      "download_speed_mbps": 5.2,
      "percent": 15.3
    }
  ],
  "total_download_mbps": 5.2,
  "total_upload_mbps": 1.1
}
```

**Close active torrents:**
```bash
# Close all active torrents
curl -X POST http://localhost:9091/torrserver/drop

# Close specific torrent
curl -X POST "http://localhost:9091/torrserver/drop?hash=abc12345"
```

**Clear TorrServer cache:**
```bash
curl -X POST http://localhost:9091/torrserver/cache/drop
```

## Troubleshooting

### Bridge can't connect to TorrServer
- Check `TORRSERVER_URL` setting
- For Docker Desktop: use `http://host.docker.internal:5665`
- Ensure TorrServer is not behind VPN

### .strm files not created
- Check bridge logs: `docker logs ts-bridge`
- TorrServer needs 3-10 seconds to load torrent metadata
- Ensure `Directory` is set in Sonarr/Radarr download client

### Torrents not removed from TorrServer
- This is **by design** - torrents must stay in TorrServer for streaming
- Only `.strm` files are removed from `/data/downloads`
- Use cleanup endpoint to remove orphaned files from libraries

### Playback fails in Jellyfin
- Verify torrent still exists in TorrServer
- Check TorrServer is accessible from Jellyfin
- Ensure `.strm` file contains valid URL

## Development

### Build

```bash
docker compose build ts-bridge
```

### Run locally

```bash
cd bridge
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 9091
```
## License

MIT License - see LICENSE file for details

## Related Projects

- [All-jellyfin-media-server](https://github.com/Morzomb/All-jellyfin-media-server) - Complete self-hosted media server setup with Jellyfin, Sonarr, Radarr, Prowlarr, and VPN integration

## Credits

- [TorrServer](https://github.com/YouROK/TorrServer) - Torrent streaming server
- [Sonarr](https://sonarr.tv/) - TV series management
- [Radarr](https://radarr.video/) - Movie management
- [Jellyfin](https://jellyfin.org/) - Media server

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
