"""
回归测试：覆盖最近 5 个稳定性修复，避免再次踩坑。

| commit  | 场景                                            |
|---------|-------------------------------------------------|
| ed6e151 | VFR 视频 fps=None 时不应被当作无效素材删除      |
| 8e9097d | VideoFileClip 的 stdout 输出必须重定向          |
| d2e1056 | 长音频 + 短 clip 时下载次数必须有上限           |
| e1be32e | clip_duration 较大时仍需收集至少 5 条素材       |
| 8c18a24 | 合成阶段异常时任务必须标记 FAILED 而非崩溃      |
"""

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.config import config
from app.models import const
from app.models.schema import MaterialInfo, VideoParams
from app.services import material, task as tm


class _ConfigIsolatedTestCase(unittest.TestCase):
    """每个用例独立的配置隔离，避免污染全局 config.app/proxy。"""

    def setUp(self):
        self._original_app = dict(config.app)
        self._original_proxy = dict(config.proxy)
        config.proxy.clear()
        # 关闭 ranker 避免 download_videos 测试触发真实 LLM 调用
        config.app["ranker_enabled"] = False

    def tearDown(self):
        config.app.clear()
        config.app.update(self._original_app)
        config.proxy.clear()
        config.proxy.update(self._original_proxy)


class TestSaveVideoVfr(_ConfigIsolatedTestCase):
    """ed6e151：VFR（变帧率）视频 fps=None 但 duration>0，缓存不应被删除。"""

    def test_save_video_keeps_file_when_fps_is_none(self):
        fake_response = SimpleNamespace(content=b"fake-video-content")

        class FakeVfrClip:
            def __init__(self, path):
                self.duration = 8.0
                self.fps = None  # VFR：moviepy 拿不到稳定帧率

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "app.services.material.requests.get", return_value=fake_response
            ), patch("app.services.material.VideoFileClip", FakeVfrClip):
                video_path = material.save_video(
                    "https://example.com/vfr.mp4", save_dir=tmp
                )

            self.assertNotEqual(video_path, "", "VFR video should be returned, not dropped")
            self.assertTrue(
                os.path.exists(video_path),
                "VFR video file must not be deleted just because fps is None",
            )


class TestSaveVideoStdoutRedirect(_ConfigIsolatedTestCase):
    """8e9097d：VideoFileClip 的 stdout 必须被截获，避免 Windows cp1252 崩溃。"""

    def test_save_video_does_not_leak_video_probe_stdout(self):
        fake_response = SimpleNamespace(content=b"fake-video-content")

        class NoisyVideoFileClip:
            def __init__(self, path):
                # 模拟 moviepy 在 cp1252 stdout 下会崩溃的非 ASCII 输出
                sys.stdout.write(f"loaded video: {path}\n")
                self.duration = 5.0
                self.fps = 24

            def close(self):
                pass

        captured_outer = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            # patch sys.stdout 后，redirect_stdout 仍能将内层写入捕获到自己的 buffer。
            # 如果回归（取消了 redirect_stdout），NoisyVideoFileClip 的输出会落到
            # captured_outer，断言失败。
            with patch("sys.stdout", captured_outer), patch(
                "app.services.material.requests.get", return_value=fake_response
            ), patch("app.services.material.VideoFileClip", NoisyVideoFileClip):
                video_path = material.save_video(
                    "https://example.com/v.mp4", save_dir=tmp
                )

        self.assertEqual(
            captured_outer.getvalue(),
            "",
            "VideoFileClip output must be redirected, not leaked to stdout",
        )
        self.assertNotEqual(video_path, "")


def _make_items(count: int, duration: int = 5):
    items = []
    for i in range(count):
        m = MaterialInfo()
        m.url = f"https://example.com/v{i}.mp4"
        m.duration = duration
        items.append(m)
    return items


class TestDownloadVideosCap(_ConfigIsolatedTestCase):
    """d2e1056：超长音频 + 短 clip 时下载尝试必须有上限，避免无限下载。"""

    def test_download_videos_caps_attempts(self):
        items = _make_items(200, duration=1)

        save_calls = []

        def fake_save(url, save_dir, max_attempts=3):
            save_calls.append(url)
            return ""  # 全部下载失败，确保循环走到 cap 触发条件

        with patch(
            "app.services.material.search_videos_pexels", return_value=items
        ), patch(
            "app.services.material._save_video_with_retry", side_effect=fake_save
        ), patch(
            "app.services.material._get_cached_path", return_value=""
        ):
            material.download_videos(
                task_id="",
                search_terms=["x"],
                source="pexels",
                audio_duration=10,
                max_clip_duration=2,
            )

        # needed_clips = max(5, ceil(10*1.5/2)) = max(5, 8) = 8
        # max_candidates = needed_clips * 4 = 32
        self.assertLessEqual(
            len(save_calls),
            32,
            f"Should not exceed cap of 32 download attempts, got {len(save_calls)}",
        )
        self.assertLess(
            len(save_calls),
            200,
            "Cap must trigger before exhausting all 200 items",
        )


class TestDownloadVideosMinClips(_ConfigIsolatedTestCase):
    """e1be32e：clip_duration 大于 audio_duration*1.5 时也需收集至少 5 条素材。"""

    def test_download_videos_collects_at_least_5_clips_when_clip_duration_is_long(self):
        items = _make_items(10, duration=10)
        saved_paths = iter(f"/fake/cache/v{i}.mp4" for i in range(10))

        def fake_save(url, save_dir, max_attempts=3):
            return next(saved_paths)

        with patch(
            "app.services.material.search_videos_pexels", return_value=items
        ), patch(
            "app.services.material._save_video_with_retry", side_effect=fake_save
        ), patch(
            "app.services.material._get_cached_path", return_value=""
        ):
            result = material.download_videos(
                task_id="",
                search_terms=["x"],
                source="pexels",
                audio_duration=5,        # 短音频
                max_clip_duration=10,    # clip_duration > audio*1.5
            )

        self.assertGreaterEqual(
            len(result),
            5,
            f"At least 5 clips should be collected for variety, got {len(result)}",
        )


class TestStartComposeFailure(_ConfigIsolatedTestCase):
    """8c18a24：合成阶段抛异常时任务应被标记 FAILED，而非让整个进程崩溃。"""

    def test_start_marks_task_failed_when_compose_raises(self):
        checkpoint = {
            "video_script": "已有的脚本",
            "video_terms": ["term1", "term2"],
            "audio_file": "/tmp/fake_audio.mp3",
            "subtitle_path": "/tmp/fake_sub.srt",
            "downloaded_videos": ["/tmp/v1.mp4", "/tmp/v2.mp4"],
        }

        params = VideoParams(
            video_subject="test",
            video_script="",
            video_terms="",
            video_aspect="9:16",
            video_concat_mode="random",
            video_transition_mode="None",
            video_clip_duration=3,
            video_count=1,
            video_source="pexels",
            video_language="",
            voice_name="zh-CN-XiaoxiaoNeural-Female",
            voice_volume=1.0,
            voice_rate=1.0,
            bgm_type="random",
            bgm_file="",
            bgm_volume=0.2,
            subtitle_enabled=True,
            subtitle_position="bottom",
            custom_position=70.0,
            font_name="MicrosoftYaHeiBold.ttc",
            text_fore_color="#FFFFFF",
            text_background_color=True,
            font_size=60,
            stroke_color="#000000",
            stroke_width=1.5,
            n_threads=2,
            paragraph_number=1,
        )

        with patch(
            "app.services.task._load_checkpoint", return_value=checkpoint
        ), patch(
            "app.services.task.voice.get_audio_duration", return_value=10
        ), patch(
            "app.services.task.save_script_data"
        ), patch(
            "app.services.task.generate_final_videos",
            side_effect=RuntimeError("simulated ffmpeg crash"),
        ), patch(
            "app.services.task.sm.state"
        ) as mock_state:
            result = tm.start(task_id="test-compose-fail", params=params)

        self.assertIsNone(result, "start() must return None on compose failure")

        failed_calls = [
            call
            for call in mock_state.update_task.call_args_list
            if call.kwargs.get("state") == const.TASK_STATE_FAILED
        ]
        self.assertGreaterEqual(
            len(failed_calls),
            1,
            "Task state must be set to FAILED at least once after compose error",
        )


# ---------------------------------------------------------------------------
# M2: 记忆模块 — 跨任务素材去重测试
# ---------------------------------------------------------------------------

class TestRankerRecencyPenalty(_ConfigIsolatedTestCase):
    """rank_video_candidates: 最近用过的 URL 应后置，新鲜 URL 前置。"""

    def setUp(self):
        super().setUp()
        config.app["memory_enabled"] = True
        config.app["ranker_enabled"] = True

    def _make_candidates(self, url_term_pairs):
        items = []
        for url, term in url_term_pairs:
            m = MaterialInfo()
            m.url = url
            m.title = term
            m.duration = 5
            items.append(m)
        return items

    def test_recently_used_url_demoted_to_back(self):
        """
        LLM 按相关度给出两个 term（cats > dogs），stale_url 对应 cats（排名更好），
        但 stale_url 3 天前用过（bucket 2）→ recency 优先，应排到 fresh_url 后面。
        """
        import time
        recent_ts = int(time.time()) - 3 * 86400   # 3 天前 → bucket 2
        # fresh_url 从未出现在 memory → bucket 0

        stale_url = "https://x/stale.mp4"
        fresh_url = "https://x/fresh.mp4"

        candidates = self._make_candidates([
            (stale_url, "cats"),   # LLM 给 cats 更高排名
            (fresh_url, "dogs"),
        ])

        # LLM: cats 比 dogs 更相关
        # Memory: stale(cats) 3 天内用过，fresh(dogs) 从未用过
        fake_histories = {stale_url: recent_ts}  # fresh_url 不在 memory → bucket 0

        from app.services import llm
        with patch("app.services.llm._generate_response", return_value='["cats", "dogs"]'), \
             patch("app.services.memory.store.get_material_histories",
                   return_value=fake_histories):
            result = llm.rank_video_candidates(candidates, "cats and dogs", ["cats", "dogs"])

        urls = [item.url for item in result]
        self.assertEqual(urls[0], fresh_url,
                         "fresh/never-used URL must come first even if its LLM rank is lower")
        self.assertEqual(urls[1], stale_url,
                         "recently-used URL must be demoted regardless of LLM rank")

    def test_memory_failure_falls_back_to_original_ranking(self):
        """memory.get_material_histories 崩溃时 ranker 应回退到原始顺序，不抛异常。"""
        candidates = self._make_candidates([
            ("https://x/a.mp4", "cats"),
            ("https://x/b.mp4", "dogs"),
        ])

        from app.services import llm
        with patch("app.services.llm._generate_response", return_value='["cats", "dogs"]'), \
             patch("app.services.memory.store.get_material_histories",
                   side_effect=RuntimeError("db locked")):
            result = llm.rank_video_candidates(candidates, "pets", ["cats", "dogs"])

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].url, "https://x/a.mp4")

    def test_memory_disabled_skips_recency_step(self):
        """memory_enabled = false 时完全跳过 recency 步骤。"""
        config.app["memory_enabled"] = False
        candidates = self._make_candidates([
            ("https://x/a.mp4", "cats"),
            ("https://x/b.mp4", "dogs"),
        ])

        from app.services import llm
        with patch("app.services.llm._generate_response", return_value='["dogs", "cats"]'), \
             patch("app.services.memory.store.get_material_histories") as mock_hist:
            result = llm.rank_video_candidates(candidates, "pets", ["cats", "dogs"])

        mock_hist.assert_not_called()
        self.assertEqual(result[0].url, "https://x/b.mp4", "dogs ranked first by LLM")


class TestDownloadVideosMemoryWrite(_ConfigIsolatedTestCase):
    """download_videos: 每条成功收集的素材都应写入 memory（cache hit + download）。"""

    def setUp(self):
        super().setUp()
        config.app["memory_enabled"] = True

    def test_cache_hit_recorded_in_memory(self):
        items = _make_items(1, duration=10)
        items[0].url = "https://x/cached.mp4"
        items[0].title = "cats"

        with patch("app.services.material.search_videos_pexels", return_value=items), \
             patch("app.services.material._get_cached_path", return_value="/fake/cached.mp4"), \
             patch("app.services.material._save_video_with_retry", return_value=""), \
             patch("app.services.memory.store.record_material_use") as mock_record:
            material.download_videos(
                task_id="t1", search_terms=["cats"], source="pexels",
                audio_duration=5, max_clip_duration=10, video_subject="cats",
            )

        mock_record.assert_called()
        call_kwargs = mock_record.call_args.kwargs
        self.assertEqual(call_kwargs["url"], "https://x/cached.mp4")
        self.assertEqual(call_kwargs["subject"], "cats")

    def test_downloaded_clip_recorded_in_memory(self):
        items = _make_items(1, duration=10)
        items[0].url = "https://x/downloaded.mp4"
        items[0].title = "dogs"

        with patch("app.services.material.search_videos_pexels", return_value=items), \
             patch("app.services.material._get_cached_path", return_value=""), \
             patch("app.services.material._save_video_with_retry",
                   return_value="/fake/downloaded.mp4"), \
             patch("app.services.memory.store.record_material_use") as mock_record:
            material.download_videos(
                task_id="t2", search_terms=["dogs"], source="pexels",
                audio_duration=5, max_clip_duration=10, video_subject="dogs",
            )

        mock_record.assert_called()
        call_kwargs = mock_record.call_args.kwargs
        self.assertEqual(call_kwargs["url"], "https://x/downloaded.mp4")

    def test_memory_write_failure_does_not_abort_collection(self):
        """memory.record_material_use 崩溃时不应中断素材收集。"""
        items = _make_items(3, duration=10)

        with patch("app.services.material.search_videos_pexels", return_value=items), \
             patch("app.services.material._get_cached_path", return_value=""), \
             patch("app.services.material._save_video_with_retry",
                   side_effect=[f"/fake/v{i}.mp4" for i in range(3)]), \
             patch("app.services.memory.store.record_material_use",
                   side_effect=RuntimeError("db full")):
            result = material.download_videos(
                task_id="t3", search_terms=["x"], source="pexels",
                audio_duration=5, max_clip_duration=10,
            )

        self.assertGreaterEqual(len(result), 1, "collection must succeed even if memory write fails")

    def test_memory_disabled_skips_write(self):
        config.app["memory_enabled"] = False
        items = _make_items(1, duration=10)

        with patch("app.services.material.search_videos_pexels", return_value=items), \
             patch("app.services.material._get_cached_path", return_value=""), \
             patch("app.services.material._save_video_with_retry",
                   return_value="/fake/v0.mp4"), \
             patch("app.services.memory.store.record_material_use") as mock_record:
            material.download_videos(
                task_id="t4", search_terms=["x"], source="pexels",
                audio_duration=5, max_clip_duration=10,
            )

        mock_record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
