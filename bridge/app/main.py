import os
import uuid
import logging
import asyncio
import re
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel

from .torrserver_client import TorrServerClient
from .transmission_rpc import TransmissionRequest, TransmissionResponse

# Configure logging level from environment
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
# httpx/httpcore log every request to TorrServer (DEBUG: ~10 lines each), which floods the log
for noisy_logger in ("httpx", "httpcore"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


SESSION_HEADER = "X-Transmission-Session-Id"


def get_env(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


app = FastAPI()


_session_id = uuid.uuid4().hex


TORRSERVER_URL = get_env("TORRSERVER_URL", "http://127.0.0.1:5666")
BRIDGE_USERNAME = os.getenv("BRIDGE_USERNAME")
BRIDGE_PASSWORD = os.getenv("BRIDGE_PASSWORD")
BRIDGE_DOWNLOAD_DIR = os.getenv("BRIDGE_DOWNLOAD_DIR", "/downloads")
STRM_CLEANUP_INTERVAL = int(os.getenv("STRM_CLEANUP_INTERVAL", "3600"))  # seconds, default 1 hour
# Paths to scan for orphaned .strm files - if empty, cleanup is disabled
# Example: STRM_CLEANUP_PATHS=/data/downloads,/data/sonarr/tv,/data/radarr/movies
_cleanup_paths_env = os.getenv("STRM_CLEANUP_PATHS", "")
STRM_CLEANUP_PATHS = [p.strip() for p in _cleanup_paths_env.split(",") if p.strip()] if _cleanup_paths_env else []


client = TorrServerClient(base_url=TORRSERVER_URL)


class AuthError(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=401, detail="Unauthorized")


def ensure_session_id(current_id: Optional[str]) -> Optional[Response]:
    if current_id != _session_id:
        response = Response(status_code=409)
        response.headers[SESSION_HEADER] = _session_id
        return response
    return None


def ensure_basic_auth(request: Request) -> None:
    if not BRIDGE_USERNAME and not BRIDGE_PASSWORD:
        return
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Basic "):
        raise AuthError()
    try:
        import base64

        encoded = auth.split(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        raise AuthError()
    if username != (BRIDGE_USERNAME or "") or password != (BRIDGE_PASSWORD or ""):
        raise AuthError()


async def cleanup_orphaned_strm_files() -> Dict[str, Any]:
    """
    Scan directories for .strm files and check if corresponding torrents exist in TorrServer.
    Remove orphaned .strm files (those pointing to non-existent torrents).
    Scans both download directories and library directories.
    """
    logger.info("Starting orphaned .strm files cleanup")
    
    # Get all torrents from TorrServer
    try:
        torrents = await client.list_torrents()
        torrent_hashes = set(t.get("hash", "").lower() for t in torrents if t.get("hash"))
        logger.info(f"Found {len(torrent_hashes)} torrents in TorrServer")
    except Exception as e:
        logger.error(f"Failed to get torrents from TorrServer: {e}")
        return {"error": str(e)}
    
    # Pattern to extract hash from .strm URL: http://torrserver:5666/play/{hash}/{file_id}
    hash_pattern = re.compile(r"/play/([a-f0-9]+)/\d+", re.IGNORECASE)
    
    scanned_files = 0
    orphaned_files = []
    deleted_files = 0
    errors = []
    
    # Scan all configured paths (downloads + libraries)
    for lib_path in STRM_CLEANUP_PATHS:
        lib_path = lib_path.strip()
        if not os.path.exists(lib_path):
            logger.warning(f"Path does not exist: {lib_path}")
            continue
        
        logger.info(f"Scanning directory: {lib_path}")
        
        # Walk through all directories
        for root, dirs, files in os.walk(lib_path):
            for filename in files:
                if not filename.endswith(".strm"):
                    continue
                
                strm_path = os.path.join(root, filename)
                scanned_files += 1
                
                try:
                    # Read .strm file content (should be a URL)
                    with open(strm_path, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                    
                    # Extract hash from URL
                    match = hash_pattern.search(content)
                    if not match:
                        logger.debug(f"Could not parse hash from: {strm_path}")
                        continue
                    
                    file_hash = match.group(1).lower()
                    
                    # Check if torrent exists in TorrServer
                    if file_hash not in torrent_hashes:
                        orphaned_files.append({
                            "path": strm_path,
                            "hash": file_hash,
                            "url": content
                        })
                        
                        # Delete the orphaned file
                        try:
                            os.remove(strm_path)
                            deleted_files += 1
                            logger.info(f"Deleted orphaned .strm: {strm_path} (hash: {file_hash[:8]})")
                            
                            # Try to remove empty parent directories
                            parent_dir = os.path.dirname(strm_path)
                            while parent_dir != lib_path and parent_dir:
                                try:
                                    if os.path.exists(parent_dir) and not os.listdir(parent_dir):
                                        os.rmdir(parent_dir)
                                        logger.info(f"Removed empty directory: {parent_dir}")
                                        parent_dir = os.path.dirname(parent_dir)
                                    else:
                                        break
                                except OSError:
                                    break
                        except Exception as e:
                            errors.append({"file": strm_path, "error": str(e)})
                            logger.error(f"Failed to delete {strm_path}: {e}")
                
                except Exception as e:
                    errors.append({"file": strm_path, "error": str(e)})
                    logger.warning(f"Error processing {strm_path}: {e}")
    
    # Second pass: cleanup empty directories
    # This is done after all files are deleted to properly handle nested empty dirs
    deleted_dirs = 0
    logger.info("Cleaning up empty directories...")
    
    # Create set of protected paths (paths from STRM_CLEANUP_PATHS) - NEVER delete these
    # Only explicitly listed paths are protected, no automatic subdirectory protection
    protected_paths = set()
    for lib_path in STRM_CLEANUP_PATHS:
        lib_path_normalized = os.path.normpath(lib_path.strip())
        protected_paths.add(lib_path_normalized)
    
    for lib_path in STRM_CLEANUP_PATHS:
        lib_path = lib_path.strip()
        if not os.path.exists(lib_path):
            continue
        
        # Collect all directories (deepest first for proper cleanup)
        all_dirs = []
        for root, dirs, files in os.walk(lib_path, topdown=False):
            for dirname in dirs:
                dir_path = os.path.join(root, dirname)
                all_dirs.append(dir_path)
        
        # Try to remove empty directories (except protected paths)
        for dir_path in all_dirs:
            dir_path_normalized = os.path.normpath(dir_path)
            
            # NEVER delete protected paths (base paths and their direct children)
            if dir_path_normalized in protected_paths:
                logger.debug(f"Skipping protected path: {dir_path}")
                continue
            
            try:
                if os.path.exists(dir_path) and not os.listdir(dir_path):
                    os.rmdir(dir_path)
                    deleted_dirs += 1
                    logger.info(f"Removed empty directory: {dir_path}")
            except OSError as e:
                # Directory not empty or other OS error - skip silently
                logger.debug(f"Could not remove directory {dir_path}: {e}")
            except Exception as e:
                logger.warning(f"Error removing directory {dir_path}: {e}")
    
    result = {
        "scanned_files": scanned_files,
        "orphaned_found": len(orphaned_files),
        "deleted_files": deleted_files,
        "deleted_dirs": deleted_dirs,
        "errors": len(errors),
        "torrents_in_server": len(torrent_hashes)
    }
    
    logger.info(f"Cleanup completed: {result}")
    return result


async def periodic_cleanup_task():
    """Background task that runs cleanup periodically."""
    logger.info(f"Periodic cleanup task started (interval: {STRM_CLEANUP_INTERVAL}s)")
    
    while True:
        try:
            await asyncio.sleep(STRM_CLEANUP_INTERVAL)
            logger.info("Running periodic .strm cleanup")
            result = await cleanup_orphaned_strm_files()
            if result.get("orphaned_found", 0) > 0:
                logger.info(f"Periodic cleanup: found and deleted {result['deleted_files']} orphaned files")
        except Exception as e:
            logger.error(f"Error in periodic cleanup task: {e}", exc_info=True)


@app.on_event("startup")
async def startup_event():
    """Start background tasks on application startup."""
    if STRM_CLEANUP_PATHS:
        logger.info(f"Starting periodic .strm cleanup task (paths: {', '.join(STRM_CLEANUP_PATHS)})")
        asyncio.create_task(periodic_cleanup_task())
    else:
        logger.info("Periodic .strm cleanup is disabled (STRM_CLEANUP_PATHS not set)")


@app.get("/transmission/rpc")
async def transmission_rpc_get(
    request: Request,
    x_transmission_session_id: Optional[str] = Header(default=None, alias=SESSION_HEADER),
):
    ensure_basic_auth(request)
    # Всегда возвращаем 409 с актуальным X-Transmission-Session-Id, чтобы клиенты могли продолжить хэндшейк
    response = Response(status_code=409)
    response.headers[SESSION_HEADER] = _session_id
    return response


@app.post("/transmission/rpc", response_model=TransmissionResponse)
async def transmission_rpc(
    request: Request,
    data: Optional[TransmissionRequest] = None,
    x_transmission_session_id: Optional[str] = Header(default=None, alias=SESSION_HEADER),
) -> Any:
    ensure_basic_auth(request)
    invalid_session = ensure_session_id(x_transmission_session_id)
    if invalid_session is not None:
        return invalid_session

    if data is None:
        # Пустое тело допустимо для некоторых проверок; вернём минимальный «успех»
        return TransmissionResponse(result="success", tag=None, arguments={})

    method = data.method
    args = data.arguments or {}
    tag = data.tag

    if method == "session-get":
        return TransmissionResponse(
            result="success",
            tag=tag,
            arguments={
                "version": "2.94",
                "rpc-version": 17,
                "rpc-version-minimum": 15,
                "download-dir": BRIDGE_DOWNLOAD_DIR,
            },
        )

    if method == "session-stats":
        stats = await client.get_session_stats()
        return TransmissionResponse(result="success", tag=tag, arguments=stats)

    if method == "torrent-add":
        if not args.get("filename") and not args.get("metainfo"):
            raise HTTPException(status_code=400, detail="filename or metainfo required")
        
        # Extract metadata from Sonarr/Radarr
        download_dir = args.get("download-dir") or ""
        
        # Category can come from labels or be auto-detected from download_dir
        category = args.get("labels")
        if isinstance(category, list) and category:
            category = category[0]
        elif not isinstance(category, str):
            category = None
        
        # Parse series/movie name from download_dir and auto-detect category
        # Sonarr: /downloads/series/Series Name/Season 01/
        # Radarr: /downloads/movies/Movie Name (2024)/
        metadata_title = None
        if download_dir:
            parts = [p for p in download_dir.split("/") if p and p not in ("downloads", "torrents", "data")]
            logger.info(f"Parsed download_dir parts: {parts}")
            
            # Auto-detect category from path structure if not provided
            if not category:
                # Check if path contains common indicators
                path_lower = download_dir.lower()
                if any(x in path_lower for x in ("/series/", "/tv/", "/shows/")):
                    category = "series"
                elif any(x in path_lower for x in ("/movies/", "/films/")):
                    category = "movie"
                else:
                    # Default based on typical Sonarr/Radarr patterns
                    category = "other"
                logger.info(f"Auto-detected category: {category} from path")
            
            if len(parts) >= 2:
                # Try to get series/movie name from path
                # If last part is "Season XX", take the one before
                if "season" in parts[-1].lower():
                    metadata_title = parts[-2] if len(parts) >= 2 else None
                else:
                    # Otherwise take the last meaningful part
                    metadata_title = parts[-1]
            elif len(parts) == 1:
                metadata_title = parts[0]
        
        if not category:
            category = "other"
        
        logger.info(f"Extracted metadata_title: {metadata_title}, category: {category} from download_dir: {download_dir}")
        
        added = await client.torrent_add(
            filename=args.get("filename"),
            metainfo=args.get("metainfo"),
            category=category,
            metadata_title=metadata_title
        )
        
        # Auto-generate .strm file(s) for import mode
        if added.get("hashString"):
            try:
                h = added["hashString"]
                
                logger.info(f"STRM generation: hash={h}, download_dir={download_dir}")
                logger.info(f"Waiting for TorrServer to load torrent metadata...")
                video_files = await client.get_all_video_files(h, max_retries=10, delay=3.0)
                logger.info(f"Found {len(video_files)} video files")
                
                if video_files and download_dir:
                    # Create .strm files in the download_dir with original filenames
                    created_count = 0
                    created_files = []
                    
                    for vf in video_files:
                        # Original path: Breaking.Bad.S01.BDRip/Breaking.Bad.S01.E01.mkv
                        original_path = vf["path"]
                        # Change extension to .strm
                        strm_relative_path = os.path.splitext(original_path)[0] + ".strm"
                        strm_full_path = os.path.join(download_dir, strm_relative_path)
                        
                        # Create directory structure
                        os.makedirs(os.path.dirname(strm_full_path), exist_ok=True)
                        
                        # Write TorrServer URL
                        url = f"{TORRSERVER_URL}/play/{h}/{vf['id']}"
                        with open(strm_full_path, "w", encoding="utf-8") as f:
                            f.write(url)
                        
                        logger.info(f"Created STRM file: {strm_full_path}")
                        created_files.append(strm_full_path)
                        created_count += 1
                    
                    # Save metadata for cleanup on removal
                    client.save_torrent_metadata(h, {
                        "download_dir": download_dir,
                        "strm_files": created_files,
                        "category": category
                    })
                    
                    logger.info(f"Created {created_count} .strm files in download_dir for Sonarr/Radarr import")
                else:
                    logger.warning(f"Cannot create files for import: video_files={len(video_files) if video_files else 0}, download_dir={download_dir}")
            except Exception as e:
                logger.error(f"STRM generation failed: {e}", exc_info=True)
                pass  # Don't fail RPC if .strm generation fails
        
        return TransmissionResponse(result="success", tag=tag, arguments={"torrent-added": added})

    if method == "torrent-get":
        fields: List[str] = args.get("fields") or []
        ids = args.get("ids")
        torrents = await client.torrent_get(ids=ids, fields=fields, pending_only=True)
        return TransmissionResponse(result="success", tag=tag, arguments={"torrents": torrents})

    if method == "torrent-remove":
        ids = args.get("ids")
        delete_data = bool(args.get("delete-local-data"))
        logger.info(f"torrent-remove request: ids={ids}, delete-local-data={delete_data}")
        
        # Only delete .strm files from /data/downloads/
        # NEVER delete torrent from TorrServer - it's needed for streaming!
        if delete_data:
            try:
                # Get torrent hashes
                torrents_info = await client.torrent_get(ids=ids, fields=["hashString", "name"])
                logger.info(f"Found {len(torrents_info)} torrents to cleanup")
                
                for torrent_info in torrents_info:
                    h = torrent_info.get("hashString")
                    if h:
                        # Get saved metadata with list of created .strm files
                        metadata = client.get_torrent_metadata(h)
                        
                        if metadata and metadata.get("strm_files"):
                            strm_files = metadata["strm_files"]
                            logger.info(f"Cleaning up {len(strm_files)} .strm files from download_dir for torrent {h[:8]}")
                            logger.info(f"Note: Torrent will remain in TorrServer for streaming")
                            
                            deleted_count = 0
                            for strm_file in strm_files:
                                try:
                                    if os.path.exists(strm_file):
                                        os.remove(strm_file)
                                        logger.info(f"Deleted STRM file from downloads: {strm_file}")
                                        deleted_count += 1
                                    
                                    # Try to remove empty directories
                                    dir_path = os.path.dirname(strm_file)
                                    while dir_path and dir_path != metadata.get("download_dir"):
                                        try:
                                            if os.path.exists(dir_path) and not os.listdir(dir_path):
                                                os.rmdir(dir_path)
                                                logger.info(f"Removed empty directory: {dir_path}")
                                            else:
                                                break
                                        except OSError:
                                            break
                                        dir_path = os.path.dirname(dir_path)
                                        
                                except Exception as e:
                                    logger.warning(f"Failed to delete {strm_file}: {e}")
                            
                            logger.info(f"Cleaned up {deleted_count} .strm files from downloads for torrent {h[:8]}")
                            logger.info(f"Torrent {h[:8]} remains in TorrServer for streaming")
                            
                            # Remove metadata after cleanup (but keep torrent in TorrServer!)
                            client.remove_torrent_metadata(h)
                        else:
                            logger.info(f"No .strm files metadata found for torrent {h[:8]}")
                            
            except Exception as e:
                logger.error(f"STRM cleanup failed: {e}", exc_info=True)
        
        # Return success WITHOUT actually removing torrent from TorrServer
        # The imported .strm files in library still need the torrent for streaming!
        logger.info(f"torrent-remove completed (torrent kept in TorrServer for streaming)")
        return TransmissionResponse(result="success", tag=tag, arguments={})

    if method in ("torrent-start", "torrent-start-now"):
        ids = args.get("ids")
        await client.torrent_start(ids=ids)
        return TransmissionResponse(result="success", tag=tag, arguments={})

    if method == "torrent-stop":
        ids = args.get("ids")
        await client.torrent_stop(ids=ids)
        return TransmissionResponse(result="success", tag=tag, arguments={})

    raise HTTPException(status_code=400, detail=f"Unsupported method: {method}")


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "service": "ts-transmission-bridge"}


@app.get("/torrserver/status")
async def torrserver_status() -> Dict[str, Any]:
    """Get TorrServer status for diagnostics - shows which torrents are actively downloading."""
    torrents = await client.list_torrents()
    stats = []
    
    for torrent in torrents:
        h = torrent.get("hash")
        if h:
            if torrent.get("stat") in (4, 5):
                # "get" would activate an inactive torrent, so report it from the list entry
                stats.append({
                    "hash": h[:8],
                    "hash_full": h,
                    "name": torrent.get("title", "Unknown")[:50],
                    "status_code": torrent.get("stat"),
                    "status": "InDB" if torrent.get("stat") == 5 else "Inactive",
                    "download_speed_mbps": 0.0,
                    "upload_speed_mbps": 0.0,
                    "loaded_mb": 0.0,
                    "total_mb": 0.0,
                    "percent": 0,
                })
                continue
            try:
                # Get detailed status from TorrServer
                data = await client._post("/torrents", {"action": "get", "hash": h})
                if isinstance(data, dict):
                    t = data.get("torrent") if "torrent" in data else data
                    if isinstance(t, dict):
                        status_code = int(t.get("stat") or 0)
                        status_names = {0: "Added", 1: "GettingInfo", 2: "Preload", 3: "Working", 4: "Closed", 5: "InDB"}
                        
                        stats.append({
                            "hash": h[:8],
                            "hash_full": h,
                            "name": t.get("name", "Unknown")[:50],
                            "status_code": status_code,
                            "status": status_names.get(status_code, "Unknown"),
                            "download_speed_mbps": round(float(t.get("download_speed") or 0) / 1024 / 1024, 2),
                            "upload_speed_mbps": round(float(t.get("upload_speed") or 0) / 1024 / 1024, 2),
                            "loaded_mb": round(int(t.get("loaded_size") or 0) / 1024 / 1024, 2),
                            "total_mb": round(int(t.get("torrent_size") or 0) / 1024 / 1024, 2),
                            "percent": round(int(t.get("loaded_size") or 0) / int(t.get("torrent_size") or 1) * 100, 1) if t.get("torrent_size") else 0
                        })
            except Exception as e:
                logger.warning(f"Failed to get status for {h}: {e}")
                continue
    
    # Separate active torrents (Working or Preload)
    active = [s for s in stats if s["status_code"] in (2, 3)]
    inactive = [s for s in stats if s["status_code"] not in (2, 3)]
    
    return {
        "total_torrents": len(torrents),
        "active_count": len(active),
        "inactive_count": len(inactive),
        "active_torrents": active,
        "inactive_torrents": inactive,
        "total_download_mbps": round(sum(s["download_speed_mbps"] for s in stats), 2),
        "total_upload_mbps": round(sum(s["upload_speed_mbps"] for s in stats), 2)
    }


@app.post("/torrserver/drop")
async def torrserver_drop(hash: Optional[str] = None) -> Dict[str, Any]:
    """Close torrent(s) in TorrServer - stops active downloading/preloading.
    
    If hash is provided, closes that specific torrent.
    If hash is not provided, closes all active (Working/Preload) torrents.
    """
    if hash:
        # Drop specific torrent
        result = await client._post("/torrents", {"action": "drop", "hash": hash})
        logger.info(f"Dropped torrent {hash[:8]}")
        return {"dropped": 1, "hash": hash[:8], "result": result}
    else:
        # Drop all active torrents
        torrents = await client.list_torrents()
        dropped = []
        
        for torrent in torrents:
            h = torrent.get("hash")
            # Drop only active torrents (Preload=2 or Working=3)
            if h and torrent.get("stat") in (2, 3):
                await client._post("/torrents", {"action": "drop", "hash": h})
                dropped.append(h[:8])
                logger.info(f"Dropped active torrent {h[:8]}")
        
        return {"dropped": len(dropped), "hashes": dropped}


@app.post("/torrserver/cache/drop")
async def torrserver_cache_drop() -> Dict[str, Any]:
    """Drop entire TorrServer cache - closes all torrents and clears cache."""
    result = await client._post("/cache", {"action": "drop"})
    logger.info("Dropped entire TorrServer cache")
    return {"result": "ok", "details": result}


@app.post("/strm/cleanup")
async def manual_strm_cleanup() -> Dict[str, Any]:
    """
    Manually trigger cleanup of orphaned .strm files.
    Scans library directories and removes .strm files pointing to non-existent torrents.
    """
    logger.info("Manual .strm cleanup triggered")
    result = await cleanup_orphaned_strm_files()
    return result

