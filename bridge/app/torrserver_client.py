import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(value, max_value))


@dataclass
class TorrentItem:
    id: int
    hash: str
    name: str
    total_size: int
    loaded_size: int
    download_speed: float
    upload_speed: float
    status_code: int
    status_string: str


class IdMap:
    def __init__(self, path: str) -> None:
        self._path = path
        self._hash_to_id: Dict[str, int] = {}
        self._id_to_hash: Dict[int, str] = {}
        self._hash_to_metadata: Dict[str, Dict[str, Any]] = {}  # Store download_dir and other metadata
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self._hash_to_id = {str(k): int(v) for k, v in data.get("hash_to_id", {}).items()}
                self._id_to_hash = {int(k): str(v) for k, v in data.get("id_to_hash", {}).items()}
                self._hash_to_metadata = data.get("hash_to_metadata", {})
        except FileNotFoundError:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
        except Exception:
            self._hash_to_id = {}
            self._id_to_hash = {}
            self._hash_to_metadata = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump({
                "hash_to_id": self._hash_to_id, 
                "id_to_hash": self._id_to_hash,
                "hash_to_metadata": self._hash_to_metadata
            }, f, indent=2)

    def get_or_create(self, hash_value: str) -> int:
        if hash_value in self._hash_to_id:
            return self._hash_to_id[hash_value]
        new_id = (max(self._id_to_hash.keys()) + 1) if self._id_to_hash else 1
        self._hash_to_id[hash_value] = new_id
        self._id_to_hash[new_id] = hash_value
        self._save()
        return new_id

    def by_id(self, id_value: int) -> Optional[str]:
        return self._id_to_hash.get(id_value)

    def by_hash(self, hash_value: str) -> Optional[int]:
        return self._hash_to_id.get(hash_value)
    
    def set_metadata(self, hash_value: str, metadata: Dict[str, Any]) -> None:
        """Store metadata (download_dir, strm_files, etc.) for a hash."""
        self._hash_to_metadata[hash_value] = metadata
        self._save()
    
    def get_metadata(self, hash_value: str) -> Optional[Dict[str, Any]]:
        """Get stored metadata for a hash."""
        return self._hash_to_metadata.get(hash_value)
    
    def remove_metadata(self, hash_value: str) -> Optional[Dict[str, Any]]:
        """Remove and return metadata for a hash."""
        metadata = self._hash_to_metadata.pop(hash_value, None)
        if metadata:
            self._save()
        return metadata

    def rebuild(self, hashes: List[str]) -> None:
        changed = False
        for h in hashes:
            if h not in self._hash_to_id:
                self.get_or_create(h)
                changed = True
        if changed:
            self._save()


class TorrServerClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        state_dir = os.getenv("STATE_DIR", "/data/ts-bridge")
        self._ids = IdMap(os.path.join(state_dir, "ids.json"))
        self._client = httpx.AsyncClient(timeout=30)

    async def _post(self, path: str, json_body: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            resp = await self._client.post(url, json=json_body)
            resp.raise_for_status()
            if resp.headers.get("content-type", "").startswith("application/json"):
                return resp.json()
            return {}
        except Exception:
            # Никогда не пробрасываем исключение наружу, чтобы не ронять RPC — вернём пустой ответ
            return {}

    async def _list_items(self) -> List[Dict[str, Any]]:
        # "list" reads TorrServer's DB and does NOT activate torrents, unlike "get"
        data = await self._post("/torrents", {"action": "list"})
        items = data if isinstance(data, list) else data.get("items") or data.get("torrents") or []
        return [item for item in items if isinstance(item, dict)]

    async def _list_hashes(self) -> List[str]:
        hashes: List[str] = []
        for item in await self._list_items():
            h = item.get("hash") or item.get("Hash")
            if isinstance(h, str):
                hashes.append(h)
        return hashes

    async def list_torrents(self) -> List[Dict[str, Any]]:
        data = await self._post("/torrents", {"action": "list"})
        items = data if isinstance(data, list) else data.get("items") or data.get("torrents") or []
        results: List[Dict[str, Any]] = []
        for item in items:
            results.append(
                {
                    "hash": str(item.get("hash") or item.get("Hash") or "").lower(),
                    "title": str(item.get("title") or item.get("name") or ""),
                    "category": str(item.get("category") or ""),
                    "stat": int(item.get("stat") or 0),
                }
            )
        return results

    async def get_session_stats(self) -> Dict[str, Any]:
        return {
            "activeTorrentCount": 0,
            "downloadSpeed": 0,
            "uploadSpeed": 0,
            "torrentCount": 0,
        }

    async def torrent_add(self, filename: Optional[str], metainfo: Optional[str], category: Optional[Union[str, List[str]]], metadata_title: Optional[str] = None) -> Dict[str, Any]:
        # Перед добавлением снимем список хэшей, чтобы после добавить отличия
        before_hashes = set([h.lower() for h in (await self._list_hashes())])

        # Приоритет: если есть metainfo (base64 .torrent), используем загрузку на /torrent/upload
        if metainfo:
            try:
                import base64
                import tempfile
                import aiohttp
                import asyncio

                decoded = base64.b64decode(metainfo)
                # Используем multipart на /torrent/upload
                url = f"{self._base_url}/torrent/upload"
                form = aiohttp.FormData()
                form.add_field("file", decoded, filename="radarr.torrent", content_type="application/x-bittorrent")
                if category:
                    cat = category[0] if isinstance(category, list) and category else category if isinstance(category, str) else None
                    if cat:
                        form.add_field("category", cat)
                form.add_field("save", "1")
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, data=form) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                        else:
                            data = {}
            except Exception:
                data = {}
        else:
            payload: Dict[str, Any] = {"action": "add", "save_to_db": True}
            if filename:
                payload["link"] = filename
            if category:
                if isinstance(category, list) and category:
                    payload["category"] = category[0]
                elif isinstance(category, str):
                    payload["category"] = category
            data = await self._post("/torrents", payload)
        # Попытка №1: взять хэш из ответа
        h = (data.get("hash") or data.get("torrent", {}).get("hash") or "").lower()
        # Попытка №2: если есть magnet в filename — вытащить из xt=urn:btih:
        if not h and filename and isinstance(filename, str) and filename.startswith("magnet:"):
            try:
                from urllib.parse import parse_qs, urlparse

                qs = parse_qs(urlparse(filename).query)
                xt = qs.get("xt", [""])[0]
                if xt.lower().startswith("urn:btih:"):
                    h = xt.split(":", 2)[-1].lower()
            except Exception:
                pass
        # Попытка №3: переснять список и найти добавившийся хэш
        if not h:
            after_hashes = set([x.lower() for x in (await self._list_hashes())])
            diff = list(after_hashes - before_hashes)
            if len(diff) == 1:
                h = diff[0]
        if not h:
            # Try relist to find new hash
            await self._refresh_id_map()
            # We cannot determine which torrent added; return minimal response
            return {"id": None, "hashString": h}
        tid = self._ids.get_or_create(h)
        name = metadata_title or data.get("name") or data.get("torrent", {}).get("name") or ""
        return {"id": tid, "hashString": h, "name": name}

    async def _refresh_id_map(self) -> None:
        hashes = await self._list_hashes()
        self._ids.rebuild(hashes)

    @staticmethod
    def _name_from_list(t: Dict[str, Any]) -> str:
        # Inactive entries carry no "name"; the torrent's info name is the root of its file paths
        try:
            files = json.loads(t.get("data") or "{}").get("TorrServer", {}).get("Files") or []
            path = str(files[0].get("path") or "") if files else ""
        except Exception:
            path = ""
        return path.split("/", 1)[0] or str(t.get("name") or t.get("title") or "")

    def _item_from_list(self, t: Dict[str, Any]) -> TorrentItem:
        hash_value = str(t.get("hash") or t.get("Hash") or "").lower()
        return TorrentItem(
            id=self._ids.get_or_create(hash_value),
            hash=hash_value,
            name=self._name_from_list(t),
            total_size=int(t.get("torrent_size") or t.get("TorrentSize") or 0),
            loaded_size=int(t.get("loaded_size") or t.get("LoadedSize") or 0),
            download_speed=float(t.get("download_speed") or 0),
            upload_speed=float(t.get("upload_speed") or 0),
            status_code=int(t.get("stat") or 0),
            status_string=str(t.get("stat_string") or ""),
        )

    async def get_best_file_id(self, hash_value: str) -> Optional[int]:
        data = await self._post("/torrents", {"action": "get", "hash": hash_value})
        # TorrServer can return data directly or wrapped in "torrent" key
        if isinstance(data, dict):
            t = data.get("torrent") if "torrent" in data else data
        else:
            t = data
        if not isinstance(t, dict):
            return None
        files = t.get("file_stats") or []
        if not isinstance(files, list) or not files:
            return None
        # Предпочитаем самый большой файл (часто это нужное видео)
        best = None
        best_len = -1
        for f in files:
            try:
                length = int(f.get("length") or 0)
                file_id = int(f.get("id") or 0)
            except Exception:
                continue
            if length > best_len:
                best_len = length
                best = file_id
        return best

    async def get_all_video_files(self, hash_value: str, max_retries: int = 10, delay: float = 3.0) -> List[Dict[str, Any]]:
        """Get all video files from torrent with id, path, length. Retries if metadata not ready."""
        import asyncio
        import logging
        logger = logging.getLogger(__name__)
        
        for attempt in range(max_retries):
            data = await self._post("/torrents", {"action": "get", "hash": hash_value})
            
            if attempt == 0:
                logger.info(f"TorrServer raw response type: {type(data)}")
            
            # TorrServer can return data directly or wrapped in "torrent" key
            if isinstance(data, dict):
                t = data.get("torrent") if "torrent" in data else data
            else:
                t = data
            
            if attempt == 0:
                logger.info(f"Extracted torrent type: {type(t)}, has file_stats: {'file_stats' in t if isinstance(t, dict) else False}")
            
            if not isinstance(t, dict):
                logger.warning(f"Attempt {attempt+1}/{max_retries}: torrent data is not a dict, type={type(t)}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay)
                    continue
                return []
            
            files = t.get("file_stats") or []
            logger.info(f"Attempt {attempt+1}/{max_retries}: file_stats type={type(files)}, len={len(files) if isinstance(files, list) else 'N/A'}")
            
            if not isinstance(files, list):
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay)
                    continue
                return []
            
            # If we got files, process them
            if files:
                video_exts = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts", ".m2ts"}
                results = []
                for f in files:
                    try:
                        file_id = int(f.get("id") or 0)
                        path = str(f.get("path") or "")
                        length = int(f.get("length") or 0)
                        
                        # Filter video files by extension and minimum size (50MB)
                        if length < 50 * 1024 * 1024:
                            continue
                        ext = os.path.splitext(path.lower())[1]
                        if ext not in video_exts:
                            continue
                        
                        results.append({"id": file_id, "path": path, "length": length})
                    except Exception:
                        continue
                
                if results:
                    return results
            
            # No files yet, wait and retry
            if attempt < max_retries - 1:
                await asyncio.sleep(delay)
        
        return []

    def _map_status(self, item: TorrentItem) -> int:
        if item.total_size > 0 and item.loaded_size >= item.total_size:
            return 6  # seeding
        if item.status_code == 4:
            return 0  # closed -> stopped
        if item.status_code in (2, 3, 5):
            return 4  # downloading (5 = in db, not activated)
        return 3  # default: download wait

    def _percent_done(self, item: TorrentItem) -> float:
        if item.total_size <= 0:
            return 0.0
        return clamp(item.loaded_size / float(item.total_size), 0.0, 1.0)

    def _pending_import(self, hash_value: str) -> Optional[Tuple[str, str]]:
        """Return (download_dir, name) if this torrent has .strm files waiting to be imported."""
        metadata = self._ids.get_metadata(hash_value) or {}
        download_dir = metadata.get("download_dir")
        strm_files = [f for f in metadata.get("strm_files") or [] if isinstance(f, str)]
        if not download_dir or not any(os.path.exists(f) for f in strm_files):
            return None
        # Sonarr/Radarr import from downloadDir + name, so name must be the top-level
        # entry the .strm files were written under (a folder, or the .strm itself)
        name = os.path.relpath(strm_files[0], download_dir).split(os.sep, 1)[0]
        return download_dir, name

    async def torrent_get(self, ids: Optional[Union[List[Union[int, str]], Union[int, str]]], fields: List[str], pending_only: bool = False) -> List[Dict[str, Any]]:
        """With pending_only, report only torrents whose .strm files await import, as finished downloads.

        Everything else is hidden from Sonarr/Radarr: TorrServer never "completes" a torrent,
        so they would otherwise sit in the queue as downloading forever.
        """
        items = await self._list_items()
        by_hash = {str(i.get("hash") or i.get("Hash") or "").lower(): i for i in items}
        by_hash.pop("", None)
        self._ids.rebuild(list(by_hash))
        selected_hashes: List[str] = []
        # Спец. значение Transmission: "recently-active" — вернуть активные/все
        def includes_recently_active(value: Any) -> bool:
            return isinstance(value, str) and value.lower() == "recently-active"

        if ids is None:
            selected_hashes = list(by_hash)
        else:
            if includes_recently_active(ids):
                selected_hashes = list(by_hash)
            else:
                if not isinstance(ids, list):
                    ids = [ids]
                if any(includes_recently_active(v) for v in ids):
                    selected_hashes = list(by_hash)
                else:
                    for ident in ids:
                        if isinstance(ident, int):
                            h = self._ids.by_id(ident)
                            if h:
                                selected_hashes.append(h)
                        elif isinstance(ident, str):
                            selected_hashes.append(ident.lower())

        results: List[Dict[str, Any]] = []
        for h in selected_hashes:
            entry = by_hash.get(h)
            if entry is None:
                continue
            st = self._item_from_list(entry)
            if pending_only:
                pending = self._pending_import(h)
                if pending is None:
                    continue
                download_dir, name = pending
                results.append({
                    "id": st.id,
                    "hashString": st.hash,
                    "name": name,
                    "downloadDir": download_dir,
                    # Stopped + finished + seed ratio limit reached: Sonarr/Radarr import by moving
                    # the .strm files and then remove the download (the torrent stays in TorrServer)
                    "status": 0,
                    "isFinished": True,
                    "percentDone": 1.0,
                    "leftUntilDone": 0,
                    "totalSize": st.total_size,
                    "downloadedEver": st.total_size,
                    "uploadedEver": 0,
                    "seedRatioMode": 1,
                    "seedRatioLimit": 0,
                    "eta": -1,
                    "errorString": "",
                    "rateDownload": 0,
                    "rateUpload": 0,
                    "peersConnected": 0,
                })
                continue
            result: Dict[str, Any] = {
                "id": st.id,
                "hashString": st.hash,
                "name": st.name,
                "status": self._map_status(st),
                "percentDone": self._percent_done(st),
                "rateDownload": int(st.download_speed),
                "rateUpload": int(st.upload_speed),
                "totalSize": st.total_size,
                "downloadedEver": st.loaded_size,
                "uploadedEver": 0,
                "peersConnected": 0,
            }
            results.append(result)
        return results

    async def torrent_remove(self, ids: Optional[Union[List[Union[int, str]], Union[int, str]]], delete_data: bool) -> None:
        if not ids:
            return
        if not isinstance(ids, list):
            ids = [ids]
        for ident in ids:
            h: Optional[str]
            if isinstance(ident, int):
                h = self._ids.by_id(ident)
            else:
                h = ident
            if not h:
                continue
            action = "wipe" if delete_data else "rem"
            await self._post("/torrents", {"action": action, "hash": h})

    def save_torrent_metadata(self, hash_value: str, metadata: Dict[str, Any]) -> None:
        """Save metadata (download_dir, strm_files, etc.) for a torrent."""
        self._ids.set_metadata(hash_value, metadata)
    
    def get_torrent_metadata(self, hash_value: str) -> Optional[Dict[str, Any]]:
        """Get saved metadata for a torrent."""
        return self._ids.get_metadata(hash_value)
    
    def remove_torrent_metadata(self, hash_value: str) -> Optional[Dict[str, Any]]:
        """Remove and return metadata for a torrent."""
        return self._ids.remove_metadata(hash_value)

    async def torrent_start(self, ids: Optional[Union[List[Union[int, str]], Union[int, str]]]) -> None:
        # TorrServer starts automatically; nothing to do.
        return None

    async def torrent_stop(self, ids: Optional[Union[List[Union[int, str]], Union[int, str]]]) -> None:
        # Not supported by TorrServer; no-op.
        return None


