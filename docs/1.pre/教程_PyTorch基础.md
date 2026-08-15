# PyTorch 基础教程

> 面向 Python 新手，零基础也能上手。目标：学会用 PyTorch 加载模型做推理（跑通 YOLO 的前提）。

---

## 目录

1. [PyTorch 是什么？](#1-pytorch-是什么)
2. [安装](#2-安装)
3. [张量（Tensor）—— PyTorch 的基本单位](#3-张量tensor-pytorch-的基本单位)
4. [张量运算](#4-张量运算)
5. [自动求导（Autograd）](#5-自动求导autograd)
6. [构建神经网络](#6-构建神经网络)
7. [训练一个最简单的模型](#7-训练一个最简单的模型)
8. [保存和加载模型](#8-保存和加载模型)
9. [用预训练模型做推理](#9-用预训练模型做推理)
10. [在 GPU 上运行](#10-在-gpu-上运行)
11. [常见报错与排坑](#11-常见报错与排坑)

---

## 1. PyTorch 是什么？

**PyTorch** 是 Facebook（现 Meta）开发的深度学习框架。通俗理解：

> **NumPy + GPU 加速 + 自动求导 = PyTorch**

你只需要知道三件事：

| 概念 | 通俗理解 |
|------|---------|
| **张量（Tensor）** | 就是"超级数组"，和 NumPy 的 ndarray 差不多，但能跑在 GPU 上 |
| **自动求导（Autograd）** | 框架自动帮你算"导数/梯度"，不用自己手推微积分 |
| **神经网络（nn.Module）** | 搭积木一样搭网络，框架帮你把前向传播、反向传播都包了 |

对本项目而言，你**不需要会训练模型**，只需要会**加载别人训练好的模型，传入数据，拿到结果**。

---

## 2. 安装

### 2.1 创建虚拟环境（强烈推荐）

```bash
# 创建一个干净的 Python 环境
conda create -n pytorch_env python=3.10 -y
conda activate pytorch_env
```

> 如果不装 conda，也可以用 `python -m venv pytorch_env`

### 2.2 安装 PyTorch

打开 [pytorch.org](https://pytorch.org)，选你的配置，复制命令。比如（CPU 版，先不用 GPU）：

```bash
pip install torch torchvision torchaudio
```

### 2.3 验证安装

打开 Python，输入：

```python
import torch
print(torch.__version__)   # 查看版本号
print(torch.cuda.is_available())  # 有没有 GPU（没有就显示 False）
```

如果能打印版本号且不报错，就装好了。

---

## 3. 张量（Tensor）—— PyTorch 的基本单位

### 3.1 从 Python 列表创建

```python
import torch

# 一维张量（向量）
t1 = torch.tensor([1, 2, 3, 4, 5])
print(t1)        # tensor([1, 2, 3, 4, 5])
print(t1.shape)  # torch.Size([5])

# 二维张量（矩阵）
t2 = torch.tensor([[1, 2], [3, 4], [5, 6]])
print(t2)
print(t2.shape)  # torch.Size([3, 2])  → 3行2列

# 三维张量（图片就是三维的！）
t3 = torch.rand(3, 224, 224)  # 3通道，高224，宽224
print(t3.shape)  # torch.Size([3, 224, 224])
```

> 🔑 **理解 shape 太重要了**——项目里 80% 的报错都是 shape 不匹配。

### 3.2 从 NumPy 数组创建

```python
import numpy as np

np_array = np.array([[1, 2], [3, 4]])
tensor_from_np = torch.from_numpy(np_array)
print(tensor_from_np)

# 反过来：张量 → NumPy
back_to_np = tensor_from_np.numpy()
```

### 3.3 常用的创建方式

```python
# 全零
z = torch.zeros(2, 3)     # 2行3列，全是0

# 全一
o = torch.ones(2, 3)      # 2行3列，全是1

# 随机数（均匀分布 0~1）
r = torch.rand(2, 3)

# 单位矩阵
e = torch.eye(3)

# 指定数据类型
x = torch.ones(2, 3, dtype=torch.float32)
```

### 3.4 张量的属性

```python
x = torch.rand(3, 224, 224)

print(x.shape)       # 形状 → torch.Size([3, 224, 224])
print(x.dtype)       # 数据类型 → torch.float32
print(x.device)      # 在 CPU 还是 GPU → cpu
print(x.numel())     # 元素总数 → 3*224*224 = 150528
```

---

## 4. 张量运算

### 4.1 基本数学运算

```python
a = torch.tensor([1, 2, 3])
b = torch.tensor([4, 5, 6])

print(a + b)        # tensor([5, 7, 9])
print(a - b)        # tensor([-3, -3, -3])
print(a * b)        # tensor([4, 10, 18])  逐元素相乘
print(a / b)        # tensor([0.25, 0.40, 0.50])

# 矩阵乘法（重点！）
A = torch.tensor([[1, 2], [3, 4]])
B = torch.tensor([[5, 6], [7, 8]])
C = torch.mm(A, B)  # 矩阵乘
print(C)

# 或者用 @ 运算符
C = A @ B
```

### 4.2 改变形状（reshape / view）

```python
x = torch.tensor([[1, 2, 3],
                  [4, 5, 6]])  # shape [2, 3]

# 改成 [3, 2]
y = x.reshape(3, 2)
print(y)

# 展平成一维
z = x.flatten()      # tensor([1, 2, 3, 4, 5, 6])

# 增加/减少维度
a = x.unsqueeze(0)   # shape [1, 2, 3]  在维度0加一维
b = x.squeeze()      # 去掉所有长度为1的维度
```

### 4.3 其他常用运算

```python
x = torch.tensor([[1, 2, 3], [4, 5, 6]], dtype=torch.float32)

print(x.sum())           # 所有元素和 → 21
print(x.mean())          # 平均值 → 3.5
print(x.max())           # 最大值 → 6
print(x.argmax())        # 最大值索引 → 5（展平后的位置）

# 按维度算
print(x.sum(dim=0))      # 按列求和 → tensor([5, 7, 9])
print(x.sum(dim=1))      # 按行求和 → tensor([6, 15])

# 拼接
a = torch.tensor([[1, 2]])
b = torch.tensor([[3, 4]])
c = torch.cat([a, b], dim=0)  # 按行拼 → [[1,2],[3,4]]
d = torch.cat([a, b], dim=1)  # 按列拼 → [[1,2,3,4]]
```

### 4.4 设备移动（CPU ↔ GPU）

```python
x = torch.tensor([1, 2, 3])

# 移到 GPU（如果可用）
if torch.cuda.is_available():
    x = x.cuda()

# 或者更推荐的方式
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
x = x.to(device)

# 移回 CPU
x = x.cpu()
```

---

## 5. 自动求导（Autograd）

> 这部分**理解原理即可**，项目中不需要自己写——但懂了才能理解训练是怎么回事。

```python
# 1. 创建一个需要梯度的张量
x = torch.tensor([2.0], requires_grad=True)

# 2. 做一些运算
y = x ** 2 + 3 * x + 1    # y = x² + 3x + 1

# 3. 反向传播（算导数）
y.backward()

# 4. 查看梯度：dy/dx = 2x + 3 = 2*2 + 3 = 7
print(x.grad)  # tensor([7.0])
```

**项目中的应用：** 你不需要手动调用 `backward()`，YOLO / 其他模型的训练代码会帮你做。你只需要知道"模型训练 = 前向传播 → 算损失 → 反向传播 → 更新参数"这个循环就行。

```
┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│ 输入数据  │ ──→ │  模型前向  │ ──→ │  算损失   │ ──→ │ 反向传播  │ ──→ 更新参数
│  (图片)   │     │ (推理)    │     │ (对比答案)│     │ (算梯度)  │
└──────────┘     └──────────┘     └──────────┘     └──────────┘
```

---

## 6. 构建神经网络

> PyTorch 里所有网络都继承 `torch.nn.Module`，只需要实现 `__init__` 和 `forward`。

### 6.1 一个最简单的网络

```python
import torch.nn as nn

class MyFirstNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.fc1 = nn.Linear(10, 5)   # 全连接层：输入10 → 输出5
        self.fc2 = nn.Linear(5, 2)    # 全连接层：输入5 → 输出2

    def forward(self, x):
        # x 的形状: [batch_size, 10]
        x = self.fc1(x)               # → [batch_size, 5]
        x = torch.relu(x)             # 激活函数（引入非线性）
        x = self.fc2(x)               # → [batch_size, 2]
        return x

# 使用
model = MyFirstNet()
input_data = torch.rand(3, 10)        # 3个样本，每个10维
output = model(input_data)
print(output.shape)                   # torch.Size([3, 2])
```

### 6.2 卷积层（处理图片用）

```python
# 卷积层：输入1通道 → 输出8通道，卷积核3x3
conv = nn.Conv2d(in_channels=1, out_channels=8, kernel_size=3)

# 池化层：2x2 最大池化（降采样）
pool = nn.MaxPool2d(kernel_size=2)

# 一个简单的图片分类网络
class SimpleCNN(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 8, 3)      # 输入1通道，输出8通道
        self.pool = nn.MaxPool2d(2)           # 2x2池化
        self.conv2 = nn.Conv2d(8, 16, 3)
        self.fc = nn.Linear(16 * 6 * 6, 10)   # 全连接分类

    def forward(self, x):
        # x: [batch, 1, 28, 28]
        x = self.pool(torch.relu(self.conv1(x)))  # → [batch, 8, 13, 13]
        x = self.pool(torch.relu(self.conv2(x)))  # → [batch, 16, 5, 5]
        x = x.flatten(1)                           # 展平 → [batch, 16*5*5]
        x = self.fc(x)                             # → [batch, 10]
        return x
```

> 🔑 **对本项目最重要的理解：** 模型就是一个函数 `y = model(x)`，输入图片，输出预测结果。你不用关心内部细节，YOLO 的作者已经帮你搭好了。

---

## 7. 训练一个最简单的模型

> 跑通这个例子，你就理解了训练的全过程。

```python
import torch
import torch.nn as nn
import torch.optim as optim

# 1. 造一些假数据（y = 2*x + 1 附近）
x = torch.rand(100, 1) * 10      # 100个点，范围0~10
y = 2 * x + 1 + torch.randn(100, 1) * 0.5  # 加一点噪声

# 2. 定义模型
model = nn.Linear(1, 1)           # 最简单的线性回归：y = wx + b

# 3. 定义损失函数和优化器
criterion = nn.MSELoss()          # 均方误差
optimizer = optim.SGD(model.parameters(), lr=0.01)  # 随机梯度下降

# 4. 训练循环
for epoch in range(1000):
    # 前向传播：模型预测
    pred = model(x)
    
    # 算损失
    loss = criterion(pred, y)
    
    # 反向传播
    optimizer.zero_grad()         # 梯度清零（重要！）
    loss.backward()               # 算梯度
    optimizer.step()              # 更新参数
    
    if epoch % 100 == 0:
        print(f"Epoch {epoch}, Loss: {loss.item():.4f}")

# 5. 看训练结果
print(f"训练得到的参数: w={model.weight.item():.3f}, b={model.bias.item():.3f}")
print(f"真实参数: w=2.000, b=1.000")
```

输出类似：
```
Epoch 0, Loss: 42.1356
Epoch 100, Loss: 0.3709
Epoch 200, Loss: 0.2875
...
Epoch 1000, Loss: 0.2820
训练得到的参数: w=2.010, b=0.972
```

这就是训练的本质——**不断调整参数，让 loss 越来越小**。

---

## 8. 保存和加载模型

### 8.1 保存（训练完后）

```python
# 方式一：保存整个模型（不推荐，移植性差）
torch.save(model, "model.pth")

# 方式二：只保存参数（推荐！）
torch.save(model.state_dict(), "model_weights.pth")
```

### 8.2 加载（项目中最常用的操作）

```python
# 1. 先创建模型结构
model = MyFirstNet()

# 2. 再加载权重
model.load_state_dict(torch.load("model_weights.pth"))

# 3. 切换到评估模式（重要！）
model.eval()
```

> 🔑 `model.eval()` 告诉模型现在是推理模式，不是训练模式。某些层（如 Dropout、BatchNorm）在两种模式下行为不同。

---

## 9. 用预训练模型做推理（⭐ 项目核心）

> 这是你们在项目中**最常用的能力**——加载别人训练好的模型，传入图片，拿到结果。

### 9.1 加载 torchvision 预训练模型

```python
import torch
import torchvision.models as models
from PIL import Image
import torchvision.transforms as transforms

# 1. 加载预训练的 ResNet18（图片分类模型）
model = models.resnet18(pretrained=True)
model.eval()

# 2. 图片预处理
transform = transforms.Compose([
    transforms.Resize(224),           # 缩放到 224x224
    transforms.CenterCrop(224),
    transforms.ToTensor(),            # PIL图片 → 张量
    transforms.Normalize(             # 标准化
        mean=[0.485, 0.456, 0.406],
        std=[0.229, 0.224, 0.225]
    )
])

# 3. 加载图片
img = Image.open("dog.jpg")
img_tensor = transform(img)           # → [3, 224, 224]

# 4. 加一个 batch 维度（模型需要 batch 维）
img_batch = img_tensor.unsqueeze(0)   # → [1, 3, 224, 224]

# 5. 推理
with torch.no_grad():                 # 推理时不需要算梯度，省内存
    output = model(img_batch)         # → [1, 1000]  （1000个类别的分数）

# 6. 取最高分的类别
pred_class_id = output.argmax(dim=1).item()
print(f"预测类别ID: {pred_class_id}")
```

### 9.2 这段代码的模板可复用性

上面 1~6 步是**标准推理流水线**，你们在项目里跑 YOLO、跑任何模型都是这个流程：

```
图片文件 → 预处理（resize + 转张量） → 加batch维 → 模型推理 → 解析结果
```

YOLO 的推理也完全一样，只是预处理和结果解析的部分 YOLO 库帮你封装好了。

### 9.3 用 YOLO 推理的直观对比

```python
# 实际上 YOLO 把上面那些步骤都封装成一行了：
from ultralytics import YOLO

model = YOLO("yolov8n.pt")           # 加载模型
results = model("dog.jpg")           # 推理
results[0].show()                    # 显示结果
```

> 你现在应该能理解 YOLO 背后在做什么了——无非是加载权重 → 预处理图片 → 推理 → 后处理。

---

## 10. 在 GPU 上运行

```python
# 如果有 GPU，把模型和数据都移到 GPU
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

model = MyFirstNet().to(device)          # 模型移到 GPU
input_data = torch.rand(3, 10).to(device) # 数据移到 GPU

output = model(input_data)
print(output.device)  # cuda:0
```

> **验证你的 GPU 能不能用：**
> ```python
> import torch
> print(torch.cuda.is_available())      # True 表示有可用 GPU
> print(torch.cuda.get_device_name(0))  # GPU 型号，比如 "NVIDIA GeForce RTX 3060"
> ```

---

## 11. 常见报错与排坑

### ❌ `RuntimeError: Expected all tensors to be on the same device`

**原因：** 模型在 GPU，数据在 CPU（或反过来），运算时报错。

**解决：** 确保都在同一个 device 上。

```python
# 统一管理
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model = model.to(device)
data = data.to(device)
```

### ❌ `RuntimeError: shape '[16, 3, 224, 224]' is invalid for input of size 10000`

**原因：** 数据的 shape 和模型期望的 shape 不匹配。

**解决：** 打印 shape 检查：

```python
print(data.shape)          # 看看实际是什么形状
print(model)               # 看看模型期望什么输入
```

### ❌ `RuntimeError: CUDA out of memory`

**原因：** GPU 显存不够。

**解决：**
- 减小 batch size（`batch_size=8` → `batch_size=4`）
- 用 `with torch.no_grad():` 包推理代码
- 用完的变量及时删除

### ❌ 加载模型时报 `KeyError`

**原因：** 模型的架构和权重文件的架构不匹配。

**解决：** 确认你创建的模型和保存权重的模型是同一个架构。

---

## 总结：你真正需要记住的

对于本项目，你最需要掌握的是以下 **6 个操作**：

| # | 操作 | 代码 |
|---|------|------|
| 1 | 创建张量 | `x = torch.tensor([1, 2, 3])` |
| 2 | 查看 shape | `x.shape` |
| 3 | 改变形状 | `x.reshape(a, b)` / `x.flatten()` |
| 4 | 加载模型 | `model = torch.load("weights.pth")` 或 YOLO 的 `YOLO("model.pt")` |
| 5 | 模型推理 | `output = model(input_data)` |
| 6 | 不加梯度 | `with torch.no_grad():` |

> 其他的——训练、优化器、损失函数——YOLO 作者已经帮你写好了，你不必自己造轮子。

---

## 动手练习

1. **练习 1：** 创建一个 `[1, 3, 640, 640]` 的全一张量，把它 reshape 成 `[1, 3, -1]`，看看 -1 变成了多少。
2. **练习 2：** 用 `torch.rand(3, 224, 224)` 模拟一张图片，unsqueeze(0) 后传给一个 `nn.Conv2d(3, 16, 3)`，看输出 shape。
3. **练习 3：** 下载一张图片，用 torchvision 的 ResNet18 跑一次分类推理，打印 top-3 预测类别。
