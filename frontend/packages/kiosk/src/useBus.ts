// SSE 事件订阅（EventSource 自动重连，规格 §5）
import { onUnmounted, ref } from "vue";
import type { BusEvent } from "shared";
import { parseSseChunk } from "shared";

export function useBus(onEvent: (ev: BusEvent) => void) {
  const connected = ref(false);
  let es: EventSource | null = null;
  let closed = false;

  function connect() {
    if (closed) return;
    es = new EventSource("/api/events");
    es.onopen = () => (connected.value = true);
    es.onmessage = (msg: MessageEvent) => {
      for (const ev of parseSseChunk(msg.data as string)) onEvent(ev);
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
