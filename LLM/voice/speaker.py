# -*- coding: utf-8 -*-
r"""声纹：3D-Speaker ERes2NetV2（modelscope）。注册 / 验证(1:1) / 识别(1:N)。"""
import re
from pathlib import Path
import numpy as np

from . import config


def _check_uid(uid: str) -> None:
    """uid 白名单校验（防路径穿越）：只允许字母/数字/下划线/连字符，非法抛 ValueError。"""
    if not isinstance(uid, str) or re.fullmatch(r"[A-Za-z0-9_-]+", uid) is None:
        raise ValueError("非法 uid")


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-6))


def merge_profile(old: np.ndarray | None, old_count: int, new: np.ndarray):
    """声纹合并平均：new 并入旧档案。返回 (merged_emb, new_count)。
    old 为 None 或 old_count<=0 时视为新建（count=1）。"""
    if old is None or old_count <= 0:
        emb = np.asarray(new, dtype=np.float32)
        return emb / (np.linalg.norm(emb) + 1e-6), 1
    merged = (old.astype(np.float32) * old_count + np.asarray(new, dtype=np.float32)) / (old_count + 1)
    merged = merged / (np.linalg.norm(merged) + 1e-6)
    return merged.astype(np.float32), old_count + 1


def classify(emb: np.ndarray, profiles: dict, threshold: float):
    """profiles: {uid: emb}。返回 (uid|None, 最高分)。纯函数，便于测试。"""
    if not profiles:
        return None, 0.0
    best_uid, best = None, -1.0
    for uid, prof in profiles.items():
        s = cosine(prof, emb)
        if s > best:
            best, best_uid = s, uid
    if best >= threshold:
        return best_uid, best
    return None, best


class SpeakerRecognizer:
    def __init__(self, threshold=config.SPK_THRESHOLD, model_id=config.SPK_MODEL_ID,
                 profile_dir=config.SPEAKER_DIR):
        self.threshold = threshold
        self.model_id = model_id
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pipeline = None
        self._profiles = {}
        self._load()

    def _ensure_model(self):
        if self._pipeline is None:
            import os
            # 模型缓存/配置统一放 LLM/models/voice/modelscope（已被 .gitignore 排除），
            # 避免散落到用户 HOME；已有环境变量时尊重外部设置。
            os.environ.setdefault(
                "MODELSCOPE_CACHE", str(config.MODEL_DIR / "modelscope" / "cache"))
            os.environ.setdefault(
                "MODELSCOPE_HOME", str(config.MODEL_DIR / "modelscope" / "home"))
            from modelscope.pipelines import pipeline  # 懒加载，避免拖慢启动
            self._pipeline = pipeline(
                task="speaker-verification", model=self.model_id, device="cpu")

    def embed(self, wav_16k: np.ndarray) -> np.ndarray:
        self._ensure_model()
        ret = self._pipeline([wav_16k], output_emb=True)
        return np.asarray(ret["embs"][0], dtype=np.float32)

    def enroll(self, uid: str, segments: list[np.ndarray], append: bool = False) -> np.ndarray:
        """segments: 若干段 16k 语音；逐段提特征取平均，落盘 npz。
        append=True 时与已有档案合并平均（档案样本计数 +1）。"""
        _check_uid(uid)
        embs = [self.embed(s) for s in segments if len(s) >= config.SAMPLE_RATE]
        if not embs:
            raise ValueError("没有足够长（≥1s）的语音段用于注册")
        new = np.mean(embs, axis=0).astype(np.float32)
        return self.enroll_embedding(uid, new, append=append)

    def enroll_embedding(self, uid: str, emb: np.ndarray, append: bool = False) -> np.ndarray:
        """直接用特征向量入档（录制暂存路径用）；append=True 合并平均。"""
        _check_uid(uid)
        old = self._profiles.get(uid)
        old_count = self.sample_count(uid)
        merged, count = merge_profile(old if append else None, old_count if append else 0, emb)
        self._profiles[uid] = merged
        np.savez(self.profile_dir / f"{uid}.npz", emb=merged, count=count)
        return merged

    def verify(self, uid: str, wav_16k: np.ndarray) -> tuple[bool, float]:
        if uid not in self._profiles:
            return False, 0.0
        score = cosine(self._profiles[uid], self.embed(wav_16k))
        return score >= self.threshold, score

    def identify(self, wav_16k: np.ndarray) -> tuple[str | None, float]:
        emb = self.embed(wav_16k)
        return classify(emb, self._profiles, self.threshold)

    def list_profiles(self) -> list[str]:
        return sorted(self._profiles.keys())

    def delete(self, uid: str) -> None:
        """清除该 uid 的声纹档案（文件 + 内存）。"""
        _check_uid(uid)
        self._profiles.pop(uid, None)
        f = self.profile_dir / f"{uid}.npz"
        if f.exists():
            f.unlink()

    def sample_count(self, uid: str) -> int:
        """档案样本计数：新 npz 读 count；旧 npz（无 count）视为 1；无档案 0。"""
        _check_uid(uid)
        f = self.profile_dir / f"{uid}.npz"
        if not f.exists():
            return 0
        try:
            d = np.load(f)
            return int(d["count"]) if "count" in d else 1
        except Exception:
            return 1

    def reload(self) -> None:
        """清空内存档案并从磁盘重新加载（供外部实例同步用）。"""
        self._profiles = {}
        self._load()

    def _load(self):
        for f in self.profile_dir.glob("*.npz"):
            try:
                self._profiles[f.stem] = np.load(f)["emb"].astype(np.float32)
            except Exception:
                continue
