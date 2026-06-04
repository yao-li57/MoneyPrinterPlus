import json
import math
import os
import os.path
import re
import time
from concurrent.futures import ThreadPoolExecutor
from os import path

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoConcatMode, VideoParams
from app.services import llm, material, subtitle, video, voice, upload_post
from app.services import state as sm
from app.utils import utils


def _retry(fn, *args, max_attempts: int = 3, **kwargs):
    """Call fn(*args, **kwargs) up to max_attempts times with exponential backoff."""
    for attempt in range(max_attempts):
        try:
            result = fn(*args, **kwargs)
            if result is not None:
                return result
        except Exception as e:
            if attempt < max_attempts - 1:
                wait = 2 ** attempt
                logger.warning(f"attempt {attempt + 1}/{max_attempts} failed: {e}, retrying in {wait}s")
                time.sleep(wait)
            else:
                logger.error(f"all {max_attempts} attempts failed: {e}")
    return None


def _load_checkpoint(task_id: str) -> dict:
    """
    Detect already-completed intermediate files in task_dir and return
    their values so start() can skip the corresponding steps.
    """
    task_dir = utils.task_dir(task_id)
    checkpoint = {}

    script_file = path.join(task_dir, "script.json")
    if path.exists(script_file):
        try:
            with open(script_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            checkpoint["video_script"] = data.get("script")
            checkpoint["video_terms"] = data.get("search_terms")
            logger.info("checkpoint: script.json found, will skip script + terms generation")
        except Exception as e:
            logger.warning(f"checkpoint: failed to read script.json: {e}")

    audio_file = path.join(task_dir, "audio.mp3")
    if path.exists(audio_file):
        checkpoint["audio_file"] = audio_file
        logger.info("checkpoint: audio.mp3 found, will skip TTS")

    subtitle_file = path.join(task_dir, "subtitle.srt")
    if path.exists(subtitle_file):
        checkpoint["subtitle_path"] = subtitle_file
        logger.info("checkpoint: subtitle.srt found, will skip subtitle generation")

    try:
        mp4_files = [
            path.join(task_dir, f)
            for f in os.listdir(task_dir)
            if f.endswith(".mp4")
            and not f.startswith("combined")
            and not f.startswith("final")
            and not f.startswith("temp")
        ]
        if mp4_files:
            checkpoint["downloaded_videos"] = mp4_files
            logger.info(f"checkpoint: {len(mp4_files)} downloaded video(s) found, will skip material download")
    except Exception:
        pass

    return checkpoint


def generate_script(task_id, params):
    logger.info("\n\n## generating video script")
    video_script = params.video_script.strip()
    if not video_script:
        video_script = llm.generate_script(
            video_subject=params.video_subject,
            language=params.video_language,
            paragraph_number=params.paragraph_number,
        )
    else:
        logger.debug(f"video script: \n{video_script}")

    if not video_script:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("failed to generate video script.")
        return None

    if config.app.get("critic_enabled", False):
        video_script = llm.critique_script(
            video_script=video_script,
            video_subject=params.video_subject,
            language=params.video_language,
            paragraph_number=params.paragraph_number or 1,
            score_threshold=config.app.get("critic_score_threshold", 0.75),
            max_iterations=config.app.get("critic_max_iterations", 2),
        )

    return video_script


def generate_terms(task_id, params, video_script):
    logger.info("\n\n## generating video terms")
    video_terms = params.video_terms
    if not video_terms:
        video_terms = llm.generate_terms(
            video_subject=params.video_subject, video_script=video_script, amount=5
        )
    else:
        if isinstance(video_terms, str):
            video_terms = [term.strip() for term in re.split(r"[,，]", video_terms)]
        elif isinstance(video_terms, list):
            video_terms = [term.strip() for term in video_terms]
        else:
            raise ValueError("video_terms must be a string or a list of strings.")

        logger.debug(f"video terms: {utils.to_json(video_terms)}")

    if not video_terms:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("failed to generate video terms.")
        return None

    return video_terms


def save_script_data(task_id, video_script, video_terms, params):
    script_file = path.join(utils.task_dir(task_id), "script.json")
    script_data = {
        "script": video_script,
        "search_terms": video_terms,
        "params": params,
    }

    with open(script_file, "w", encoding="utf-8") as f:
        f.write(utils.to_json(script_data))


def generate_audio(task_id, params, video_script):
    '''
    Generate audio for the video script.
    If a custom audio file is provided, it will be used directly.
    There will be no subtitle maker object returned in this case.
    Otherwise, TTS will be used to generate the audio.
    Returns:
        - audio_file: path to the generated or provided audio file
        - audio_duration: duration of the audio in seconds
        - sub_maker: subtitle maker object if TTS is used, None otherwise
    '''
    logger.info("\n\n## generating audio")
    # /audio 和 /subtitle 请求模型不包含 custom_audio_file，
    # 这里统一做兼容读取，避免直调接口时抛属性错误。
    custom_audio_file = getattr(params, "custom_audio_file", None)
    if not custom_audio_file or not os.path.exists(custom_audio_file):
        if custom_audio_file:
            logger.warning(
                f"custom audio file not found: {custom_audio_file}, using TTS to generate audio."
            )
        else:
            logger.info("no custom audio file provided, using TTS to generate audio.")
        audio_file = path.join(utils.task_dir(task_id), "audio.mp3")
        sub_maker = _retry(
            voice.tts,
            text=video_script,
            voice_name=voice.parse_voice_name(params.voice_name),
            voice_rate=params.voice_rate,
            voice_file=audio_file,
        )
        if sub_maker is None:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                """failed to generate audio:
1. check if the language of the voice matches the language of the video script.
2. check if the network is available. If you are in China, it is recommended to use a VPN and enable the global traffic mode.
            """.strip()
            )
            return None, None, None
        audio_duration = math.ceil(voice.get_audio_duration(sub_maker))
        if audio_duration == 0:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("failed to get audio duration.")
            return None, None, None
        return audio_file, audio_duration, sub_maker
    else:
        logger.info(f"using custom audio file: {custom_audio_file}")
        audio_duration = voice.get_audio_duration(custom_audio_file)
        if audio_duration == 0:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("failed to get audio duration from custom audio file.")
            return None, None, None
        return custom_audio_file, audio_duration, None

def generate_subtitle(task_id, params, video_script, sub_maker, audio_file):
    '''
    Generate subtitle for the video script.
    If subtitle generation is disabled or no subtitle maker is provided, it will return an empty string.
    Otherwise, it will generate the subtitle using the specified provider.
    Returns:
        - subtitle_path: path to the generated subtitle file
    '''
    logger.info("\n\n## generating subtitle")
    if not params.subtitle_enabled or sub_maker is None:
        return ""

    subtitle_path = path.join(utils.task_dir(task_id), "subtitle.srt")
    subtitle_provider = config.app.get("subtitle_provider", "edge").strip().lower()
    logger.info(f"\n\n## generating subtitle, provider: {subtitle_provider}")

    subtitle_fallback = False
    if subtitle_provider == "edge":
        voice.create_subtitle(
            text=video_script, sub_maker=sub_maker, subtitle_file=subtitle_path
        )
        if not os.path.exists(subtitle_path):
            subtitle_fallback = True
            logger.warning("subtitle file not found, fallback to whisper")

    if subtitle_provider == "whisper" or subtitle_fallback:
        _retry(subtitle.create, audio_file=audio_file, subtitle_file=subtitle_path)
        logger.info("\n\n## correcting subtitle")
        subtitle.correct(subtitle_file=subtitle_path, video_script=video_script)

    subtitle_lines = subtitle.file_to_subtitles(subtitle_path)
    if not subtitle_lines:
        logger.warning(f"subtitle file is invalid: {subtitle_path}")
        return ""

    return subtitle_path


def get_video_materials(task_id, params, video_terms, audio_duration):
    if params.video_source == "local":
        logger.info("\n\n## preprocess local materials")
        materials = video.preprocess_video(
            materials=params.video_materials, clip_duration=params.video_clip_duration
        )
        if not materials:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                "no valid materials found, please check the materials and try again."
            )
            return None
        return [material_info.url for material_info in materials]
    else:
        logger.info(f"\n\n## downloading videos from {params.video_source}")
        downloaded_videos = material.download_videos(
            task_id=task_id,
            search_terms=video_terms,
            source=params.video_source,
            video_aspect=params.video_aspect,
            video_contact_mode=params.video_concat_mode,
            audio_duration=audio_duration * params.video_count,
            max_clip_duration=params.video_clip_duration,
            video_subject=params.video_subject,
        )
        if not downloaded_videos:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                "failed to download videos, maybe the network is not available. if you are in China, please use a VPN."
            )
            return None
        return downloaded_videos


def generate_final_videos(
    task_id, params, downloaded_videos, audio_file, subtitle_path, video_terms=None
):
    final_video_paths = []
    combined_video_paths = []
    video_concat_mode = (
        params.video_concat_mode if params.video_count == 1 else VideoConcatMode.random
    )
    video_transition_mode = params.video_transition_mode

    # Semantic timeline alignment: reorder clips to match subtitle content
    aligned_videos = downloaded_videos
    semantic_alignment = config.app.get("semantic_alignment", True)
    if semantic_alignment and subtitle_path and video_terms:
        meta_path = path.join(utils.task_dir(task_id), "clips-meta.json")
        if path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    clip_term_map = json.load(f)
                aligned_videos = video.align_clips_to_subtitles(
                    subtitle_path=subtitle_path,
                    downloaded_videos=downloaded_videos,
                    clip_term_map=clip_term_map,
                    max_clip_duration=params.video_clip_duration,
                )
                video_concat_mode = VideoConcatMode.sequential
                logger.info("semantic_alignment: clips reordered, using sequential mode")
            except Exception as e:
                logger.warning(f"semantic_alignment failed, using original clip order: {e}")
        else:
            logger.debug("semantic_alignment: clips-meta.json not found, skipping alignment")

    _progress = 50
    for i in range(params.video_count):
        index = i + 1
        combined_video_path = path.join(
            utils.task_dir(task_id), f"combined-{index}.mp4"
        )
        logger.info(f"\n\n## combining video: {index} => {combined_video_path}")
        video.combine_videos(
            combined_video_path=combined_video_path,
            video_paths=aligned_videos,
            audio_file=audio_file,
            video_aspect=params.video_aspect,
            video_concat_mode=video_concat_mode,
            video_transition_mode=video_transition_mode,
            max_clip_duration=params.video_clip_duration,
            threads=params.n_threads,
        )

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_path = path.join(utils.task_dir(task_id), f"final-{index}.mp4")

        logger.info(f"\n\n## generating video: {index} => {final_video_path}")
        video.generate_video(
            video_path=combined_video_path,
            audio_path=audio_file,
            subtitle_path=subtitle_path,
            output_file=final_video_path,
            params=params,
        )

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_paths.append(final_video_path)
        combined_video_paths.append(combined_video_path)

    return final_video_paths, combined_video_paths


def start(task_id, params: VideoParams, stop_at: str = "video"):
    logger.info(f"start task: {task_id}, stop_at: {stop_at}")
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=5, step="script")

    # Load checkpoint: skip steps whose output files already exist on disk
    checkpoint = _load_checkpoint(task_id)

    # 1. Generate script
    if checkpoint.get("video_script"):
        video_script = checkpoint["video_script"]
        logger.info("checkpoint: reusing existing script")
    else:
        video_script = generate_script(task_id, params)
        if not video_script or "Error: " in video_script:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            return

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=10)

    if stop_at == "script":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, script=video_script
        )
        return {"script": video_script}

    # 2+3: Generate terms and audio
    # terms and TTS are independent — run in parallel when both are needed.
    audio_file, audio_duration, sub_maker = None, None, None
    video_terms = checkpoint.get("video_terms") or ""

    # Restore audio from checkpoint if available
    checkpoint_audio = checkpoint.get("audio_file")
    if checkpoint_audio:
        audio_file = checkpoint_audio
        audio_duration = math.ceil(voice.get_audio_duration(audio_file))
        # sub_maker stays None; subtitle generation will fall back to Whisper if needed

    if params.video_source != "local" and stop_at != "terms":
        sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=15, step="terms_and_tts")
        need_terms = not video_terms
        need_audio = not audio_file
        if need_terms and need_audio:
            with ThreadPoolExecutor(max_workers=2) as executor:
                terms_future = executor.submit(generate_terms, task_id, params, video_script)
                audio_future = executor.submit(generate_audio, task_id, params, video_script)
            video_terms = terms_future.result()
            if not video_terms:
                sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
                return
            audio_file, audio_duration, sub_maker = audio_future.result()
            if not audio_file:
                sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
                return
        elif need_terms:
            video_terms = generate_terms(task_id, params, video_script)
            if not video_terms:
                sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
                return
        elif need_audio:
            audio_file, audio_duration, sub_maker = generate_audio(task_id, params, video_script)
            if not audio_file:
                sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
                return
        else:
            logger.info("checkpoint: reusing existing audio and terms")
    elif params.video_source != "local":
        # stop_at == "terms": only terms needed
        sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=15, step="terms")
        if not video_terms:
            video_terms = generate_terms(task_id, params, video_script)
            if not video_terms:
                sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
                return
    else:
        # local source: only TTS needed
        sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=15, step="tts")
        if not audio_file:
            audio_file, audio_duration, sub_maker = generate_audio(task_id, params, video_script)
            if not audio_file:
                sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
                return

    save_script_data(task_id, video_script, video_terms, params)

    if stop_at == "terms":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, terms=video_terms
        )
        return {"script": video_script, "terms": video_terms}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=30)

    if stop_at == "audio":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            audio_file=audio_file,
        )
        return {"audio_file": audio_file, "audio_duration": audio_duration}

    # 4+5: Generate subtitle and fetch materials in parallel — both depend only on TTS output.
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=35, step="subtitle_and_material")

    checkpoint_subtitle = checkpoint.get("subtitle_path")
    checkpoint_videos = checkpoint.get("downloaded_videos")
    need_subtitle = not checkpoint_subtitle
    need_materials = not checkpoint_videos

    if need_subtitle and need_materials:
        with ThreadPoolExecutor(max_workers=2) as executor:
            subtitle_future = executor.submit(
                generate_subtitle, task_id, params, video_script, sub_maker, audio_file
            )
            materials_future = executor.submit(
                get_video_materials, task_id, params, video_terms, audio_duration
            )
        subtitle_path = subtitle_future.result()
        downloaded_videos = materials_future.result()
    elif need_subtitle:
        logger.info("checkpoint: reusing existing downloaded videos")
        downloaded_videos = checkpoint_videos
        subtitle_path = generate_subtitle(task_id, params, video_script, sub_maker, audio_file)
    elif need_materials:
        logger.info("checkpoint: reusing existing subtitle")
        subtitle_path = checkpoint_subtitle
        downloaded_videos = get_video_materials(task_id, params, video_terms, audio_duration)
    else:
        logger.info("checkpoint: reusing existing subtitle and downloaded videos")
        subtitle_path = checkpoint_subtitle
        downloaded_videos = checkpoint_videos

    if not downloaded_videos:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return

    if stop_at == "subtitle":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            subtitle_path=subtitle_path,
        )
        return {"subtitle_path": subtitle_path}

    if stop_at == "materials":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            materials=downloaded_videos,
        )
        return {"materials": downloaded_videos}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=50, step="compose")

    # 仅完整视频生成流程才需要处理视频拼接模式；
    # 这样可以避免 /subtitle 和 /audio 这类请求访问不存在的字段。
    if type(params.video_concat_mode) is str:
        params.video_concat_mode = VideoConcatMode(params.video_concat_mode)

    # 6. Generate final videos
    final_video_paths, combined_video_paths = generate_final_videos(
        task_id, params, downloaded_videos, audio_file, subtitle_path, video_terms=video_terms
    )

    if not final_video_paths:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        return

    logger.success(
        f"task {task_id} finished, generated {len(final_video_paths)} videos."
    )

    # 7. Cross-post to TikTok/Instagram (if enabled)
    cross_post_results = []
    if upload_post.upload_post_service.is_configured() and upload_post.upload_post_service.auto_upload:
        logger.info("\n\n## cross-posting videos to TikTok/Instagram")
        for video_path in final_video_paths:
            result = upload_post.cross_post_video(
                video_path=video_path,
                title=params.video_subject or "Check out this video! #shorts #viral"
            )
            cross_post_results.append(result)
            if result.get('success'):
                logger.info(f"✅ Cross-posted: {video_path}")
            else:
                logger.warning(f"⚠️ Failed to cross-post: {video_path} - {result.get('error', 'Unknown error')}")

    kwargs = {
        "videos": final_video_paths,
        "combined_videos": combined_video_paths,
        "script": video_script,
        "terms": video_terms,
        "audio_file": audio_file,
        "audio_duration": audio_duration,
        "subtitle_path": subtitle_path,
        "materials": downloaded_videos,
        "cross_post_results": cross_post_results if cross_post_results else None,
    }
    sm.state.update_task(
        task_id, state=const.TASK_STATE_COMPLETE, progress=100, **kwargs
    )
    return kwargs


if __name__ == "__main__":
    task_id = "task_id"
    params = VideoParams(
        video_subject="金钱的作用",
        voice_name="zh-CN-XiaoyiNeural-Female",
        voice_rate=1.0,
    )
    start(task_id, params, stop_at="video")
