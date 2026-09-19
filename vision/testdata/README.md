# 视觉测试图（不入库）

这里放**人脸检测/识别**单测用的固定样本。图是二进制、体积不小，所以**不入库**
（`.gitignore` 已排除），删掉后相关用例会 `skip`（不报错）—— 需要时按下面重新拉取。

`tests/test_face.py`、`tests/test_faceid.py` 依赖这三张：

| 文件 | 用途 | 来源 |
|---|---|---|
| `portrait_lena.jpg` | 单人脸基准（512×512），含已知人脸框 `[212,187,357,389]` | OpenCV 官方样例图 `samples/data/lena.jpg`（经典测试图，出处见 OpenCV 仓库） |
| `noface_graf.png` | **无脸负对照**（800×640 纹理图）——验证"不误检" | OpenCV 官方样例图 `samples/data/graf1.png` |
| `group_solvay.jpg` | **多脸基准**（3000×2171，Solvay 1927 合影，**29 人**） | Wikimedia Commons `File:Solvay_conference_1927.jpg`（公有领域历史照片） |

## 重新拉取

```powershell
# 前两张走 jsDelivr 镜像 GitHub 上的 OpenCV 样例（本机 raw.githubusercontent 不通）
Invoke-WebRequest 'https://cdn.jsdelivr.net/gh/opencv/opencv@master/samples/data/lena.jpg'  -OutFile portrait_lena.jpg
Invoke-WebRequest 'https://cdn.jsdelivr.net/gh/opencv/opencv@master/samples/data/graf1.png' -OutFile noface_graf.png

# Solvay 合影：必须先用 Commons API 取直链（直接猜缩略图尺寸会被拒：400 "Use thumbnail sizes listed"）
$api = 'https://commons.wikimedia.org/w/api.php?action=query&format=json&prop=imageinfo&iiprop=url&titles=File:Solvay_conference_1927.jpg'
$url = (Invoke-RestMethod $api).query.pages.PSObject.Properties.Value.imageinfo[0].url
Invoke-WebRequest $url -OutFile group_solvay.jpg
```

> 为什么用这批图：它们分别覆盖"单人准不准""会不会误检""多脸能不能全检出来"三件事，
> 且都是公开可再分发的素材（`group_solvay.jpg` 是 1927 年的公有领域照片），
> **不用真人样本进仓库**。真人阈值标定请用 `vision/人脸识别注意事项.md` §3 的流程在本机做。
