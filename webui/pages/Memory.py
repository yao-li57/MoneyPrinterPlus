"""
记忆管理面板 — 查看 / 删除 / 清空三类本地记忆。

布局：三个 tab（素材记忆 / 脚本样本 / 失败记录），每 tab 内：
  - 顶部统计卡片
  - 数据表格（逐行显示，每行附 Delete 按钮）
  - 底部"清空该类记忆"按钮

此页面在 Streamlit multi-page 模式下运行（webui/pages/ 目录）。
"""

import os
import sys
from datetime import datetime

import streamlit as st

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
if root_dir not in sys.path:
    sys.path.append(root_dir)

from app.services.memory import store as mem_store
from app.utils import utils

# ---- i18n (minimal inline, reuse Main.py tr() pattern) --------------------

import json

_i18n_dir = os.path.join(root_dir, "webui", "i18n")
_system_locale = utils.get_system_locale()


def _load_translations(locale: str) -> dict:
    for name in (locale, "zh", "en"):
        p = os.path.join(_i18n_dir, f"{name}.json")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                return data.get("Translation", data)
            except Exception:
                pass
    return {}


_translations = _load_translations(_system_locale)


def tr(key: str) -> str:
    return _translations.get(key, key)


# ---- helpers ----------------------------------------------------------------

def _fmt_ts(ts) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def _delete_btn(label: str, key: str) -> bool:
    return st.button(label, key=key, type="secondary", use_container_width=True)


# ---- page -------------------------------------------------------------------

st.set_page_config(page_title=tr("Memory Management"), page_icon="🧠", layout="wide")
st.title(f"🧠 {tr('Memory Management')}")
st.caption(tr("Memory Management Description"))

try:
    stats = mem_store.stats()
except Exception as _e:
    st.error(f"Could not load memory store: {_e}")
    st.stop()

# Top-level stats
_s1, _s2, _s3 = st.columns(3)
_s1.metric(tr("Materials"), stats["materials"])
_s2.metric(tr("Scripts"), stats["scripts"])
_s3.metric(tr("Failures"), stats["failures"])

st.divider()

tab_mat, tab_scripts, tab_fail = st.tabs(
    [tr("Materials"), tr("Scripts"), tr("Failures")]
)

# ---- Materials tab ----------------------------------------------------------
with tab_mat:
    rows = mem_store.list_materials(limit=200)
    if not rows:
        st.info(tr("No Records"))
    else:
        for row in rows:
            with st.container(border=True):
                c1, c2 = st.columns([5, 1])
                with c1:
                    url_display = row["url"]
                    if len(url_display) > 80:
                        url_display = url_display[:77] + "..."
                    st.markdown(f"**{url_display}**")
                    st.caption(
                        f"{tr('Subject')}: {row.get('subject') or '-'} &nbsp;·&nbsp;"
                        f"{tr('Used Count')}: {row.get('used_count', 0)} &nbsp;·&nbsp;"
                        f"{tr('In Final Count')}: {row.get('in_final_count', 0)} &nbsp;·&nbsp;"
                        f"{tr('Last Used')}: {_fmt_ts(row.get('last_used_at'))}"
                    )
                with c2:
                    if _delete_btn(tr("Delete"), key=f"del_mat_{row['url']}"):
                        mem_store.delete_material(row["url"])
                        st.rerun()

    st.divider()
    if st.button(tr("Clear All"), key="clear_materials", type="primary"):
        mem_store._conn  # ensure init
        mem_store._conn.execute("DELETE FROM material_memory")
        st.success(tr("Memory Cleared"))
        st.rerun()

# ---- Scripts tab ------------------------------------------------------------
with tab_scripts:
    STATUS_LABELS = {"accepted": "✅", "rejected": "❌", "edited": "✏️"}
    rows = mem_store.list_script_samples(limit=200)
    if not rows:
        st.info(tr("No Records"))
    else:
        for row in rows:
            with st.container(border=True):
                c1, c2 = st.columns([5, 1])
                with c1:
                    badge = STATUS_LABELS.get(row.get("status", ""), "")
                    st.markdown(
                        f"{badge} **{tr('Subject')}**: {row.get('subject') or '-'}  "
                        f"&nbsp;·&nbsp; {_fmt_ts(row.get('created_at'))}"
                    )
                    preview = (row.get("script") or "")[:200]
                    if len(row.get("script", "")) > 200:
                        preview += "…"
                    st.text(preview)
                    if row.get("edited_to"):
                        st.caption(f"✏️ Edited → {row['edited_to'][:100]}")
                with c2:
                    if _delete_btn(tr("Delete"), key=f"del_script_{row['id']}"):
                        mem_store.delete_script_sample(row["id"])
                        st.rerun()

    st.divider()
    if st.button(tr("Clear All"), key="clear_scripts", type="primary"):
        mem_store._conn
        mem_store._conn.execute("DELETE FROM script_samples")
        st.success(tr("Memory Cleared"))
        st.rerun()

# ---- Failures tab -----------------------------------------------------------
with tab_fail:
    rows = mem_store.list_failures(limit=200)
    if not rows:
        st.info(tr("No Records"))
    else:
        for row in rows:
            with st.container(border=True):
                c1, c2 = st.columns([5, 1])
                with c1:
                    st.markdown(
                        f"🔴 **{tr('Stage')}**: `{row.get('stage', '-')}` &nbsp;·&nbsp;"
                        f"{_fmt_ts(row.get('created_at'))}"
                    )
                    if row.get("error"):
                        st.code(row["error"][:300], language=None)
                    detail_parts = []
                    if row.get("provider"):
                        detail_parts.append(f"{tr('Provider')}: {row['provider']}")
                    if row.get("task_id"):
                        detail_parts.append(f"task_id: `{row['task_id']}`")
                    if detail_parts:
                        st.caption(" &nbsp;·&nbsp; ".join(detail_parts))
                    if row.get("recovery"):
                        st.success(f"{tr('Recovery')}: {row['recovery']}")
                with c2:
                    if _delete_btn(tr("Delete"), key=f"del_fail_{row['id']}"):
                        mem_store.delete_failure(row["id"])
                        st.rerun()

    st.divider()
    if st.button(tr("Clear All"), key="clear_failures", type="primary"):
        mem_store._conn
        mem_store._conn.execute("DELETE FROM failure_records")
        st.success(tr("Memory Cleared"))
        st.rerun()
