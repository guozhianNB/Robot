# Please install OpenAI SDK first: `pip3 install openai`
import os
from openai import OpenAI

from dotenv import load_dotenv
load_dotenv()  # 加载环境变量（.env 文件中的 DEEPSEEK_API_KEY）

client = OpenAI(
    api_key=os.environ.get('DEEPSEEK_API_KEY'),
    base_url="https://api.deepseek.com")

messages = [
    {"role": "system", "content": "You are a helpful assistant。answer in Chinese."},
    {"role": "user", "content": "hi"},
    ]
thinking = "disabled"
# ============ 流式回答 ============
# stream=True 之后，response 不再是"完整答案对象"，
# 而是一个迭代器：模型每生成一小段就返回一个 chunk。
# 所以我们能边收到边打印 → 用户看到"打字机"效果。
response = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=messages,
    stream=True,           # ① 开启流式
    reasoning_effort="max",  # low/high/max
    extra_body={"thinking": {"type": thinking}}
)

reasoning_content = ""
content = ""

print("🧠 思考过程：", end="", flush=True)
for chunk in response:
    choice = chunk.choices[0]
    delta = choice.delta

    # ② 每个 chunk 拆成两段：
    #    reasoning_content = 思考过程（thinking 模式下才有）
    #    content          = 正式回答

    reasoning = getattr(delta, "reasoning_content", None)
    if reasoning:
        reasoning_content += delta.reasoning_content
        print(delta.reasoning_content, end="", flush=True)  # ③ 实时打印，不换行、立即刷新
    elif delta.content:
        content += delta.content
        print(delta.content, end="", flush=True)

    # ④ 结束标志：模型说"我说完了"
    if choice.finish_reason == "stop":
        break

print("\n\n===== 最终汇总 =====")
print("Reasoning Content:", reasoning_content)
print("Content:", content)