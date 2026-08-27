// SSE 事件订阅（EventSource 自动重连，规格 §5）
import { onUnmounted, ref } from "vue";
import { parseBusPayload } from "shared";

export function useBus(onEvent: (ev: import("shared").BusEvent) => void) {
  const connected = ref(false);
  let es: EventSource | null = null;
  let closed = false;

  function connect() {
    if (closed) return;
    es = new EventSource("/api/events");
    es.onopen = () => (connected.value = true);
    es.onmessage = (msg: MessageEvent) => {
      // EventSource 的 msg.data 已剥离 "data:" 前缀（WHATWG 标准）——
      // 直接 JSON.parse 按 payload.type 分发；不能用 parseSseChunk（它要求 data: 前缀，会全丢）
      const ev = parseBusPayload(msg.data as string);
      if (ev) onEvent(ev);
    };
    es.onerror = () => {
      connected.value = false;
      es?.close();
      setTimeout(connect, 3000);   // 自动重连
    };
  }

  connect();
  onUnmounted(() => {
    closed = true;
    es?.close();
  });
  return { connected };
}
