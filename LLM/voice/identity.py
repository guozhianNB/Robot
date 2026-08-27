# -*- coding: utf-8 -*-
r"""身份融合层：本期只有声纹一路，人脸路接口预留（规格 D8）。"""
from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class IdentityVote:
    candidate_uid: Optional[str]
    confidence: float
    source: str


class IdentitySource(Protocol):
    name: str
    def probe(self, wav) -> IdentityVote: ...


class VoiceprintSource:
    name = "voiceprint"

    def __init__(self, recognizer):
        self.recognizer = recognizer

    def probe(self, wav) -> IdentityVote:
        uid, score = self.recognizer.identify(wav)
        return IdentityVote(uid, score, self.name)


class VoiceprintOnlyFusion:
    """本期单源裁决。未来加 FaceSource 后在此加权/择优（接口不变）。"""

    def __init__(self, recognizer):
        self._source = VoiceprintSource(recognizer)

    def resolve(self, wav) -> IdentityVote:
        return self._source.probe(wav)


def effective_uid(vote: IdentityVote, current_uid: Optional[str],
                  locked_uid: Optional[str] = None) -> Optional[str]:
    """锁定优先：手动锁定时固定返回锁定用户（声纹只提示不切换，规格 D11）；
    未锁定：高置信度用识别结果，低置信度沿用当前 uid（宁问勿猜）。"""
    if locked_uid:
        return locked_uid
    if vote.candidate_uid is not None:
        return vote.candidate_uid
    return current_uid
