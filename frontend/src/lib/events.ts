/** Live event stream (/ws/events/) that keeps cached queries fresh. */
import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { qk } from "./queries";

export function wsUrl(path: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}${path}`;
}

/**
 * Close a socket without the browser's "closed before the connection is established"
 * warning (React StrictMode mounts effects twice in development): a socket that is still
 * connecting is closed as soon as it opens instead.
 */
export function closeSocket(sock: WebSocket | null | undefined): void {
  if (!sock) return;
  if (sock.readyState === WebSocket.CONNECTING) {
    sock.onmessage = null;
    sock.onerror = null;
    sock.onopen = () => sock.close();
  } else if (sock.readyState === WebSocket.OPEN) {
    sock.close();
  }
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
          applyLiveEvent(qc, msg);
        } catch {
          /* ignore malformed */
        }
      };
      ws.onclose = (ev: CloseEvent) => {
        setConnected(false);
        if (life.closed) return;
        // 4401 session gone / 4403 access revoked: do not retry; refresh auth (shows login if signed out)
        if (ev.code === 4401 || ev.code === 4403) {
          void qc.invalidateQueries({ queryKey: qk.auth });
          return;
        }
        timer = setTimeout(connect, retry.current);
        retry.current = Math.min(retry.current * 2, 30_000);
      };
    };
    connect();
    return () => {
      life.closed = true;
      if (timer) clearTimeout(timer);
      closeSocket(ws);
    };
  }, [enabled, qc]);

  return connected;
}

type LiveEvent = { type: string; instance_id: string | null; payload: any };

/** Server lists (["instances", {filters}]), not the per-server queries that share the "instances" prefix. */
export function isInstanceListKey(key: readonly unknown[]): boolean {
  return key.length === 2 && key[0] === "instances" && typeof key[1] === "object" && key[1] !== null;
}

/** Job lists (["jobs", {filters}] or ["jobs"]), not job details (["jobs", id]). */
export function isJobListKey(key: readonly unknown[]): boolean {
  return key[0] === "jobs" && (key.length === 1 || (typeof key[1] === "object" && key[1] !== null));
}

/**
 * Map one live event to cache updates. Query keys match by prefix, so invalidating ["instances", id]
 * would refetch every query of that server (files, mods, metrics, backups...) on each progress tick;
 * only the summary queries are refreshed here, and the per-server queries only when a job finishes.
 */
export function applyLiveEvent(qc: QueryClient, msg: LiveEvent): void {
  const id = msg.instance_id;
  if (msg.type === "instance.status" && id) {
    void qc.invalidateQueries({ queryKey: qk.instance(id), exact: true });
    void qc.invalidateQueries({ predicate: (q) => isInstanceListKey(q.queryKey) });
    return;
  }
  if (!msg.type.startsWith("job.")) return;
  const job = msg.payload;
  if (job?.id) qc.setQueryData(qk.job(job.id), (old: any) => (old ? { ...old, ...job } : old));
  if (msg.type === "job.progress") {
    // progress ticks (up to ~1/s): update the server's active job in place, no refetch
    if (id && job?.id) {
      qc.setQueryData(qk.instance(id), (old: any) =>
        old?.active_job?.id === job.id ? { ...old, active_job: { ...old.active_job, ...job } } : old,
      );
    }
    return;
  }
  void qc.invalidateQueries({ predicate: (q) => isJobListKey(q.queryKey) });
  if (!id) return;
  if (msg.type === "job.created") {
    void qc.invalidateQueries({ queryKey: qk.instance(id), exact: true });
    return;
  }
  // finished / failed / abandoned: the job may have changed anything about the server
  void qc.invalidateQueries({ queryKey: qk.instance(id) });
  void qc.invalidateQueries({ predicate: (q) => isInstanceListKey(q.queryKey) });
}
