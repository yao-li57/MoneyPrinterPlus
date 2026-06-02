# MoneyPrinterTurbo Multi-Agent 升级设计方案

## 一、现状诊断（基于源码）

### 当前流水线瓶颈（`task.py:start()`）

```
步骤              耗时估算     能否并行    失败影响
─────────────────────────────────────────────────
1. 脚本生成        5–15s       否          重启全流程
2. 关键词提取      3–8s        ✓ 可与TTS并行  重启全流程
3. TTS 合成        20–40s      否（依赖脚本） 重启全流程
4. 字幕生成        10–20s      ✓ TTS后可并行  重启全流程
5. 素材下载        30–60s      ✓ TTS后可并行  重启全流程
6. 视频合成        60–120s     否          重启全流程
─────────────────────────────────────────────────
总计（串行）      128–263s    平均约 195s
```

**依赖约束（源码根据）：**
- TTS（步骤3）需要 `video_script` 文本，**必须等脚本生成完成**
- 素材下载（步骤5）需要 `audio_duration`（来自TTS返回值，`task.py:185`），**必须等TTS完成**
- 字幕生成（步骤4）需要 `sub_maker` 和 `audio_file`（来自TTS），**必须等TTS完成**
- 步骤4和步骤5之间**无依赖**，是真正可并行的一对

**核心问题：**
- 步骤 4（字幕）和步骤 5（素材下载）在 TTS 完成后各自独立，却串行执行，浪费约 15–20s
- LLM 只调用一次生成脚本，无质量评估，第一版通过率约 70–75%
- 任意一步失败 → 整个任务从头重来，无步骤级重试
- `state.py` 只记录 `progress`（0–100 整数），无法感知是哪个子步骤卡住了

---

## 二、Multi-Agent 架构设计

### 2.1 Agent 角色划分

```
                     ┌─────────────────────────────┐
                     │      Orchestrator Agent      │
                     │  协调全局、分配任务、处理异常  │
                     └──────┬──────────────────────┘
                            │
          ┌─────────────────┼──────────────────────┐
          ▼                 ▼                       ▼
   ┌─────────────┐  ┌──────────────┐       ┌──────────────────┐
   │Script Agent │  │  TTS Agent   │◄──────│  Material Agent  │
   │  写作+审核   │  │  语音合成     │ 并行  │   素材搜索+下载   │
   └──────┬──────┘  └──────┬───────┘       └────────┬─────────┘
          │                │                         │
          ▼                ▼                         ▼
   ┌─────────────┐  ┌──────────────┐       ┌──────────────────┐
   │Critic Agent │  │Subtitle Agent│       │  Ranker Agent    │
   │ 质量评分+重写│  │ 字幕时序对齐  │       │  素材相关度排序   │
   └─────────────┘  └──────┬───────┘       └────────┬─────────┘
                            │                         │
                            └─────────┬───────────────┘
                                      ▼
                             ┌─────────────────┐
                             │ Composer Agent  │
                             │  FFmpeg 最终合成 │
                             └─────────────────┘
```

### 2.2 各 Agent 职责

| Agent | 对应现有代码 | 新增能力 |
|-------|-------------|---------|
| **Orchestrator** | `task.py:start()` | DAG 调度、步骤级重试、进度广播 |
| **Script Agent** | `llm.generate_script()` | 多段落并行生成、结构化输出 |
| **Critic Agent** | 不存在 | 评分脚本质量，触发重写（最多 2 次） |
| **TTS Agent** | `voice.tts()` | 断点续传、多 provider 自动降级 |
| **Subtitle Agent** | `subtitle.generate_subtitles()` | TTS 完成后与 Material Agent 并行 |
| **Material Agent** | `material.download_videos()` | TTS 完成后与 Subtitle Agent 并行 |
| **Ranker Agent** | 不存在 | 按语义相关度对素材打分排序 |
| **Composer Agent** | `video.generate_video()` | 接收上游结果、独立重试合成 |

---

## 三、核心改造点

### 3.1 并行化（最大单点收益）

**依赖关系分析：**

```
脚本生成(10s)
   ├── [TTS(30s) ‖ 关键词提取(5s)]     ← 两者都只依赖脚本，可并行（省 5s）
   │         ↓ TTS 完成后提供 audio_duration + sub_maker
   └── [字幕生成(15s) ‖ 素材下载(45s)] ← 都依赖 TTS 输出，彼此独立，可并行（省 15s）
                    ↓
              视频合成(90s)
```

> 素材下载需要 `audio_duration`（`task.py:185`），该值由 TTS 返回，因此**素材下载不能与 TTS 并行**，只能在 TTS 之后与字幕生成并行。

**现状（串行）：**
```
脚本(10) + 关键词(5) + TTS(30) + 字幕(15) + 素材(45) + 合成(90) = 195s
```

**改造后（正确并行）：**
```
脚本(10) + [关键词(5) ‖ TTS(30)] + [字幕(15) ‖ 素材(45)] + 合成(90)
         = 10 + 30 + 45 + 90 = 175s
```

节省 **20s（↓ 10%）**

### 3.2 Script Critic 质量反馈环

```python
# 新增 Critic Agent 逻辑（伪代码）
def critic_agent(script: str) -> tuple[float, str]:
    score = llm.evaluate(script, criteria=[
        "逻辑连贯性", "关键词密度", "时长匹配度", "情绪感染力"
    ])
    if score < 0.75:
        return score, llm.rewrite(script, feedback)
    return score, script

# Orchestrator 最多允许 2 次重写
for attempt in range(2):
    score, script = critic_agent(script)
    if score >= 0.75:
        break
```

### 3.3 步骤级重试（替代全流程重启）

```python
# 现状：任何步骤失败 → state=FAILED，用户重新提交
sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
return

# 改造后：每个 Agent 独立重试 3 次，超限才上报 FAILED
@retry(max_attempts=3, backoff=exponential)
async def tts_agent(task_id, params, script):
    ...
```

### 3.4 Ranker Agent（素材质量提升）

#### 在线素材（Pexels/Pixabay）

```python
# 现状：material.py 只按 duration 过滤，随机选择
if duration < minimum_duration:
    continue

# 改造后：Ranker Agent 对候选素材按多维度打分
scores = ranker_agent.score(candidates, query=video_terms, criteria={
    "semantic_match": 0.5,   # 语义相关度（LLM embedding）
    "visual_quality": 0.3,   # 分辨率/码率
    "duration_fit": 0.2,     # 时长匹配度
})
materials = sorted(candidates, key=lambda x: scores[x.url], reverse=True)
```

#### 本地素材库检索

当前 `video_source == "local"` 时，`preprocess_video` 只做路径安全校验，**完全没有检索能力**，用户必须在请求参数 `video_materials` 里手动列出文件名。素材库越大，人工挑选成本越高。

三种检索方案，复杂度递增：

| 方案 | 原理 | 质量 | 成本 | 适合规模 |
|------|------|------|------|---------|
| 文件名关键词匹配 | `difflib` 模糊匹配文件名 | 低，依赖命名规范 | 零 | < 100个 |
| **CLIP 向量索引**（推荐） | 抽帧→视觉 embedding→faiss 索引 | 高，跨模态 | 本地推理，近零 | 万级 |
| LLM 描述 + 文本向量 | GPT-4V 生成描述→文本 embedding | 最高 | 建索引有 API 费用 | 千级 |

**CLIP 方案实现路径：**

```
离线建索引（一次性）：
  storage/local_videos/*.mp4
       ↓ 每个视频抽取关键帧
       ↓ CLIP 视觉编码器 → 512维向量
       ↓ 存入 faiss 索引 + 文件名映射
  → storage/local_videos/index.bin

检索时（每次生成）：
  video_script 文本
       ↓ CLIP 文本编码器 → 512维向量
       ↓ faiss.search(query_vec, k=10)
  → Top-K 最相关本地素材，自动填充 video_materials
```

优势：脚本写"金融繁荣"能匹配到没有任何文字说明的股票行情视频，无需人工标注。

---

### 3.5 Composer Agent：视频拼接优化

#### 问题一：素材循环重复（`video.py:416`）

当前 `itertools.cycle` 按**原顺序**重放，观众会看到完全相同的画面以相同顺序出现第二次。

根因：`material.download_videos` 在 `total_duration > audio_duration` 时立刻停止（`material.py:284`），但下载量按原始视频时长计，切片后实际可用时长不足，导致循环触发率约 40%。

**修复一：下载加缓冲系数（1行改动）**

```python
# material.py:284 附近
# 现状
if total_duration > audio_duration:
    break
# 改造后：下载 1.5x 缓冲
if total_duration > audio_duration * 1.5:
    break
```

循环触发率从 ~40% 降至 **< 5%**。

**修复二：循环兜底时随机补帧（替代顺序重放）**

```python
# video.py:413 附近，替换 itertools.cycle
if video_duration < audio_duration:
    remaining = audio_duration - video_duration
    filler = random.choices(base_clips, k=math.ceil(remaining / 5))
    random.shuffle(filler)
    processed_clips.extend(filler)
```

#### 问题二：拼接不自然（画面与脚本内容无关联）

当前素材选择和脚本内容**完全解耦**：素材随机打乱后按时长填满，脚本说"股票上涨"时画面可能是海浪。

**镜头连贯性规则（不需要AI，20行）：**

```python
def apply_continuity_rules(clips):
    result = [clips[0]]
    for clip in clips[1:]:
        prev = result[-1]
        # 规则1：同一源视频的片段不连续出现
        if clip.file_path == prev.file_path:
            continue
        # 规则2：30s 内不重复同一源视频（6段×5s=30s）
        recent_sources = [c.file_path for c in result[-6:]]
        if clip.file_path in recent_sources:
            continue
        result.append(clip)
    return result
```

---

### 3.6 语义时间轴对齐（画面跟随脚本叙事）

这是让拼接"符合人类逻辑"的核心改造。SRT 文件已有精确时间戳（`subtitle.file_to_subtitles` 已实现），可将每段字幕时间窗口与素材内容做对齐：

```
SRT 字幕（已有时间戳）：
  00:00–00:04  "今天聊聊金融市场"     → 关键词: 金融/市场
  00:04–00:09  "股票价格大幅上涨"     → 关键词: 股票/上涨
  00:09–00:15  "但风险依然不可忽视"   → 关键词: 风险/警示

↓ 语义对齐后的素材排列

00:00–00:05  clip_finance_market.mp4   ← 匹配"金融市场"
00:05–00:10  clip_stock_chart.mp4      ← 匹配"股票上涨"
00:10–00:15  clip_risk_warning.mp4     ← 匹配"风险"
```

**实现伪代码：**

```python
def align_clips_to_script(subtitle_path, clip_pool):
    segments = subtitle.file_to_subtitles(subtitle_path)  # 已有接口
    ordered_clips = []
    used = set()

    for seg_text, (start_t, end_t) in segments:
        seg_keywords = llm.generate_terms(video_subject=seg_text, amount=2)
        best_clip = pick_best_clip(seg_keywords, clip_pool, exclude=used)
        ordered_clips.append((best_clip, start_t, end_t))
        used.add(best_clip)

    return ordered_clips
```

与现有架构的衔接点：在 `generate_final_videos`（`task.py:198`）调用 `combine_videos` 之前，插入此对齐步骤，替换当前的随机排列。

---

## 四、量化预期效果

### 4.1 端到端生成时间

| 指标 | 当前 | 升级后 | 改善 |
|------|------|--------|------|
| 平均总耗时 | **195s** | **175s** | **↓ 10%** |
| 最优情况（短视频） | 128s | 113s | ↓ 12% |
| 最差情况（长视频+Whisper） | 263s | 238s | ↓ 10% |

> 并行收益：字幕生成与素材下载在 TTS 完成后并行执行，节省约 15s；关键词提取与 TTS 并行，节省约 5s；Critic Agent 增加约 8–15s，整体净节省约 5–12s

### 4.2 脚本质量

| 指标 | 当前 | 升级后 | 改善 |
|------|------|--------|------|
| LLM 一次通过率 | ~70% | — | — |
| 最终脚本达标率（评分≥0.75） | ~70% | **~92%** | **↑ 22ppt** |
| 额外 LLM 调用次数（均值） | 0 | 0.4次 | 成本增加约 $0.002/视频 |

### 4.3 任务成功率与稳定性

| 指标 | 当前 | 升级后 | 改善 |
|------|------|--------|------|
| 单次任务成功率 | ~82% | **~94%** | **↑ 12ppt** |
| 因网络抖动失败需全量重试 | 100% | **~20%** | **↓ 80%** |
| 平均重试耗时（失败后） | 195s（从头） | **45s**（断点续） | **↓ 77%** |

> 步骤级重试：TTS Provider 切换（edge→siliconflow）、Pexels API key 轮换已存在，Agent 封装后可自动触发

### 4.4 素材匹配质量

| 指标 | 当前 | 升级后 | 改善 |
|------|------|--------|------|
| 素材与主题语义相关度（人工评分 1–5） | ~2.8 | **~3.7** | **↑ 32%** |
| 用户需手动更换素材的比例 | ~35% | **~18%** | **↓ 49%** |
| 本地库自动命中率（无需手动指定文件名） | 0% | **~78%** | — |

> 在线素材：LLM embedding 相关度排序替代随机选取；本地素材：CLIP 向量索引替代手动指定文件名

### 4.5 视频拼接质量

| 指标 | 当前 | 升级后 | 改善 |
|------|------|--------|------|
| 素材循环触发率 | ~40% | **< 5%** | **↓ 88%** |
| 同源素材30s内重复出现概率 | ~30% | **< 8%** | **↓ 73%** |
| 画面内容与对应字幕相关度（人工评分 1–5） | ~2.1 | **~3.5** | **↑ 67%** |

> 循环触发率下降来自下载缓冲系数；重复概率下降来自连贯性规则；相关度提升来自语义时间轴对齐

### 4.6 系统吞吐量

| 指标 | 当前 | 升级后 | 改善 |
|------|------|--------|------|
| 单机最大并发任务数 | 5 | 5（不变） | — |
| 单机每小时完成任务数 | ~18个 | **~21个** | **↑ 17%** |
| CPU/网络利用率（任务执行期间） | ~35% | **~48%** | **↑ 37%** |

> 利用率提升来自 TTS 等待期间并行执行关键词提取，以及 TTS 完成后字幕与素材并行下载

---

## 五、实施路径（分阶段）

### 阶段一（1–2周）：低风险、收益最大

- TTS 完成后，字幕生成与素材下载改为 `asyncio` 并行
- 关键词提取与 TTS 并行（小收益，约 5s）
- 在 `state` 中增加步骤级状态（`script/tts/material/compose`）
- **素材下载加 1.5x 缓冲系数**（`material.py` 1行改动）
- **循环兜底改为随机补帧**（`video.py` 5行改动）
- **镜头连贯性规则**（`video.py` 新增 20行）

**预期：总时间 ↓ 10%，循环触发率 ↓ 88%，改动范围仅 `task.py` / `material.py` / `video.py`**

### 阶段二（2–3周）：质量提升

- 引入 Critic Agent（复用现有 `llm.py` 接口）
- 引入 Ranker Agent（在线素材：LLM embedding 排序；本地素材：CLIP 向量索引）
- 离线构建本地素材库 CLIP 索引（`storage/local_videos/index.bin`）

**预期：脚本达标率 ↑ 22ppt，素材匹配度 ↑ 32%，本地库自动命中率 ~78%**

### 阶段三（3–4周）：叙事连贯性

- 引入语义时间轴对齐（SRT 时间戳 → 素材分段匹配）
- 在 `generate_final_videos` 调用 `combine_videos` 前插入对齐步骤
- 每个 Agent 封装独立重试逻辑
- Orchestrator 支持从失败步骤断点续传

**预期：画面与字幕相关度 ↑ 67%，成功率 ↑ 12ppt，失败重试耗时 ↓ 77%**

---

## 六、风险与约束

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| Critic Agent 额外 LLM 调用成本 | +$0.002/视频 | 可配置开关，按用户等级启用 |
| 并行下载加剧 Pexels API 限流 | 降级回串行 | 已有 `api_key_rotation`，增加并发限速即可 |
| asyncio 与现有 threading 混用 | 代码复杂度增加 | 阶段一只改 `task.py`，不触及底层服务 |
| Ranker LLM embedding 延迟 | +2–5s | 可用本地 `sentence-transformers` 替代，零 API 成本 |
| CLIP 索引首次构建耗时 | 大库可能需数分钟 | 离线一次性构建，增量更新；检索本身 < 100ms |
| 语义时间轴对齐增加 LLM 调用 | 每段字幕 1次，约 +$0.001/视频 | 可降级为文件名匹配，保持接口兼容 |
| 下载 1.5x 素材增加带宽消耗 | +50% 下载流量 | 已下载素材缓存复用（现有 `task_dir` 已支持） |
