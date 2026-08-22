# -*- coding: utf-8 -*-
r"""声纹：3D-Speaker ERes2NetV2（modelscope）。注册 / 验证(1:1) / 识别(1:N)。"""
from pathlib import Path
import numpy as np

from . import config


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-6))


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

    def enroll(self, uid: str, segments: list[np.ndarray]) -> np.ndarray:
        """segments: 若干段 16k 语音；逐段提特征取平均，落盘 npz。"""
        embs = [self.embed(s) for s in segments if len(s) >= config.SAMPLE_RATE]
        if not embs:
            raise ValueError("没有足够长（≥1s）的语音段用于注册")
        profile = np.mean(embs, axis=0).astype(np.float32)
        profile = profile / (np.linalg.norm(profile) + 1e-6)
        self._profiles[uid] = profile
        np.savez(self.profile_dir / f"{uid}.npz", emb=profile)
        return profile

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

    def _load(self):
        for f in self.profile_dir.glob("*.npz"):
            try:
                self._profiles[f.stem] = np.load(f)["emb"].astype(np.float32)
            except Exception:
                continue
