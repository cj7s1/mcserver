/** Live event stream (/ws/events/) that keeps cached queries fresh. */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { qk } from "./queries";

export function wsUrl(path: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}${path}`;
}

export function useLiveEvents(enabled: boolean): boolean {
  const qc = useQueryClient();
  const [connected, setConnected] = useState(false);
  const retry = useRef(1000);

  useEffect(() => {
    if (!enabled) return;
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const life = { closed: false };

    const connect = () => {
      ws = new WebSocket(wsUrl("/ws/events/"));
      ws.onopen = () => {
        setConnected(true);
        retry.current = 1000;
      };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data) as { type: string; instance_id: string | null; payload: any };
          if (msg.type === "instance.status" && msg.instance_id) {
            qc.invalidateQueries({ queryKey: qk.instance(msg.instance_id) });
            qc.invalidateQueries({ queryKey: qk.instances });
          } else if (msg.type.startsWith("job.")) {
            qc.invalidateQueries({ queryKey: qk.jobs });
            if (msg.payload?.id) qc.setQueryData(qk.job(msg.payload.id), (old: any) => (old ? { ...old, ...msg.payload } : old));
            if (msg.instance_id) {
              qc.invalidateQueries({ queryKey: qk.instance(msg.instance_id) });
              if (msg.type === "job.finished") {
                qc.invalidateQueries({ queryKey: ["instances", msg.instance_id] });
                qc.invalidateQueries({ queryKey: qk.instances });
              }
            }
          }
        } catch {
          /* ignore malformed */
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (life.closed) return;
        timer = setTimeout(connect, retry.current);
        retry.current = Math.min(retry.current * 2, 30_000);
      };
    };
    connect();
    return () => {
      life.closed = true;
      if (timer) clearTimeout(timer);
      ws?.close();
    };
  }, [enabled, qc]);

  return connected;
}
