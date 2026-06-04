import json
import math
import os
import random
import threading
import time
from typing import List
from urllib.parse import urlencode

import requests
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode
from app.utils import utils

# Thread-safe counter for API key rotation
_api_key_counter = 0
_api_key_lock = threading.Lock()


def _get_tls_verify() -> bool:
    # 默认开启 TLS 证书校验，防止素材搜索和下载过程被中间人篡改。
    # 仅在企业代理、自签证书等明确需要的场景下，允许用户通过
    # `config.toml` 显式设置 `tls_verify = false` 临时关闭。
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")

    if not tls_verify:
        logger.warning(
            "TLS certificate verification is disabled by config.app.tls_verify=false. "
            "Only use this in trusted proxy environments."
        )

    return bool(tls_verify)


def get_api_key(cfg_key: str):
    api_keys = config.app.get(cfg_key)
    if not api_keys:
        raise ValueError(
            f"\n\n##### {cfg_key} is not set #####\n\nPlease set it in the config.toml file: {config.config_file}\n\n"
            f"{utils.to_json(config.app)}"
        )

    # if only one key is provided, return it
    if isinstance(api_keys, str):
        return api_keys

    global _api_key_counter
    with _api_key_lock:
        _api_key_counter += 1
        return api_keys[_api_key_counter % len(api_keys)]


def search_videos_pexels(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)
    video_orientation = aspect.name
    video_width, video_height = aspect.to_resolution()
    api_key = get_api_key("pexels_api_keys")
    headers = {
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    }
    # Build URL
    params = {"query": search_term, "per_page": 20, "orientation": video_orientation}
    query_url = f"https://api.pexels.com/videos/search?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        response = r.json()
        video_items = []
        if "videos" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["videos"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["video_files"]
            # loop through each url to determine the best quality
            for video in video_files:
                w = int(video["width"])
                h = int(video["height"])
                if w == video_width and h == video_height:
                    item = MaterialInfo()
                    item.provider = "pexels"
                    item.url = video["link"]
                    item.duration = duration
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_pixabay(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)

    video_width, video_height = aspect.to_resolution()

    api_key = get_api_key("pixabay_api_keys")
    # Build URL
    params = {
        "q": search_term,
        "video_type": "all",  # Accepted values: "all", "film", "animation"
        "per_page": 50,
        "key": api_key,
    }
    query_url = f"https://pixabay.com/api/videos/?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url, proxies=config.proxy, verify=_get_tls_verify(), timeout=(30, 60)
        )
        response = r.json()
        video_items = []
        if "hits" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["hits"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["videos"]
            # loop through each url to determine the best quality
            for video_type in video_files:
                video = video_files[video_type]
                w = int(video["width"])
                # h = int(video["height"])
                if w >= video_width:
                    item = MaterialInfo()
                    item.provider = "pixabay"
                    item.url = video["url"]
                    item.duration = duration
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def save_video(video_url: str, save_dir: str = "") -> str:
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    url_without_query = video_url.split("?")[0]
    url_hash = utils.md5(url_without_query)
    video_id = f"vid-{url_hash}"
    video_path = f"{save_dir}/{video_id}.mp4"

    # if video already exists, return the path
    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        logger.info(f"video already exists: {video_path}")
        return video_path

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    # if video does not exist, download it
    with open(video_path, "wb") as f:
        f.write(
            requests.get(
                video_url,
                headers=headers,
                proxies=config.proxy,
                verify=_get_tls_verify(),
                timeout=(60, 240),
            ).content
        )

    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        clip = None
        try:
            clip = VideoFileClip(video_path)
            duration = clip.duration
            fps = clip.fps
            if duration > 0 and fps > 0:
                return video_path
        except Exception as e:
            logger.warning(f"invalid video file: {video_path} => {str(e)}")
            try:
                os.remove(video_path)
            except Exception as remove_error:
                logger.warning(
                    f"failed to remove invalid video file: {video_path}, error: {str(remove_error)}"
                )
        finally:
            if clip is not None:
                try:
                    clip.close()
                except Exception as close_error:
                    logger.warning(
                        f"failed to close video clip: {video_path}, error: {str(close_error)}"
                    )
    return ""


def download_videos(
    task_id: str,
    search_terms: List[str],
    source: str = "pexels",
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_contact_mode: VideoConcatMode = VideoConcatMode.random,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
    video_subject: str = "",
) -> List[str]:
    valid_video_items = []
    valid_video_urls = []
    found_duration = 0.0
    search_videos = search_videos_pexels
    if source == "pixabay":
        search_videos = search_videos_pixabay

    # How many clips we need: at least MIN_CLIPS for variety, capped at 4x for failure safety
    MIN_CLIPS = 5
    needed_clips = max(MIN_CLIPS, math.ceil(audio_duration * 1.5 / max(max_clip_duration, 1)))
    max_candidates = needed_clips * 4

    for search_term in search_terms:
        if len(valid_video_items) >= max_candidates:
            logger.info(f"enough candidates collected ({len(valid_video_items)}), skipping remaining search terms")
            break
        video_items = search_videos(
            search_term=search_term,
            minimum_duration=max_clip_duration,
            video_aspect=video_aspect,
        )
        logger.info(f"found {len(video_items)} videos for '{search_term}'")

        for item in video_items:
            if item.url not in valid_video_urls:
                item.title = search_term
                valid_video_items.append(item)
                valid_video_urls.append(item.url)
                found_duration += item.duration

    logger.info(
        f"found total videos: {len(valid_video_items)}, required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
    )

    ranker_enabled = config.app.get("ranker_enabled", True)
    if ranker_enabled and len(valid_video_items) >= 2:
        from app.services import llm
        logger.info("ranker: ranking video candidates by semantic relevance")
        valid_video_items = llm.rank_video_candidates(valid_video_items, video_subject, search_terms)
    elif video_contact_mode.value == VideoConcatMode.random.value:
        random.shuffle(valid_video_items)

    video_paths = []
    clip_term_map = {}

    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task":
        material_directory = utils.task_dir(task_id)
    elif material_directory and not os.path.isdir(material_directory):
        material_directory = ""

    total_duration = 0.0
    n_cached = 0
    n_downloaded = 0
    tries = 0

    # Single pass in ranked order: cache hit → use directly, miss → download
    for item in valid_video_items:
        if total_duration > audio_duration * 1.5 and len(video_paths) >= MIN_CLIPS:
            break
        if tries >= max_candidates:
            logger.warning(f"reached download attempt limit ({max_candidates}), stopping")
            break
        tries += 1
        if total_duration > audio_duration * 1.5:
            break
        cached = _get_cached_path(item.url, material_directory)
        if cached:
            logger.info(f"cache hit: {cached}")
            video_paths.append(cached)
            clip_term_map[cached] = item.title
            total_duration += min(max_clip_duration, item.duration)
            n_cached += 1
        else:
            saved = _save_video_with_retry(item.url, material_directory)
            if saved:
                video_paths.append(saved)
                clip_term_map[saved] = item.title
                total_duration += min(max_clip_duration, item.duration)
                n_downloaded += 1

    logger.success(f"collected {len(video_paths)} videos: {n_cached} from cache, {n_downloaded} downloaded")

    if task_id and video_paths:
        try:
            meta_path = os.path.join(utils.task_dir(task_id), "clips-meta.json")
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(clip_term_map, f, ensure_ascii=False)
            logger.debug(f"saved clips-meta.json: {meta_path}")
        except Exception as e:
            logger.warning(f"failed to save clips-meta.json: {e}")

    return video_paths


def _get_cached_path(video_url: str, save_dir: str = "") -> str:
    """Return the cached file path if it already exists on disk, else empty string."""
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")
    url_without_query = video_url.split("?")[0]
    url_hash = utils.md5(url_without_query)
    video_path = os.path.join(save_dir, f"vid-{url_hash}.mp4")
    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        return video_path
    return ""


def _save_video_with_retry(video_url: str, save_dir: str, max_attempts: int = 3) -> str:
    for attempt in range(max_attempts):
        try:
            logger.info(f"downloading video: {video_url}")
            result = save_video(video_url=video_url, save_dir=save_dir)
            if result:
                return result
            # save_video returned "" without exception = invalid/corrupt file,
            # retrying the same URL won't help.
            logger.warning(f"video invalid after download, skipping: {video_url}")
            return ""
        except Exception as e:
            if attempt < max_attempts - 1:
                wait = 2 ** attempt
                logger.warning(f"download attempt {attempt + 1}/{max_attempts} failed: {e}, retrying in {wait}s")
                time.sleep(wait)
            else:
                logger.error(f"failed to download video after {max_attempts} attempts: {video_url} => {e}")
    return ""


if __name__ == "__main__":
    download_videos(
        "test123", ["Money Exchange Medium"], audio_duration=100, source="pixabay"
    )
