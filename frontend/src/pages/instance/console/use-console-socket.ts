/**
 * WebSocket client for /ws/instances/{id}/console/ (backend/apps/console/consumers.py).
 *
 * Server → client: hello{can_command}, agent{connected,message?}, logs{lines}, log{line},
 * state{state}, reset, error{message}, closed, command_result{id,ok,command,output|error}.
 * Client → server: command{id,command}.
 *
 * Reconnects with exponential backoff (1 s → 30 s). The backoff only resets once a
 * connection has stayed up for a while so a server that accepts and immediately
 * drops us does not cause a tight loop.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { closeSocket, wsUrl } from "@/lib/events";
import { qk } from "@/lib/queries";

export type ConsoleServerMessage =
  | { type: "hello"; can_command: boolean }
  | { type: "agent"; connected: boolean; message?: string }
  | { type: "logs"; lines: string[] }
  | { type: "log"; line: string }
  | { type: "state"; state: string }
  | { type: "reset" }
  | { type: "error"; message: string }
  | { type: "closed" }
  | { type: "command_result"; id: string; ok: boolean; command?: string; output?: string; error?: string; code?: string };

/** Synthetic events emitted by the hook itself. */
export type ConsoleLocalEvent = { type: "_open"; reconnect: boolean } | { type: "_disconnected" };

export type ConnState = "connecting" | "connected" | "agent_lost" | "stream_closed" | "reconnecting" | "denied";

/** Close codes the panel uses when a session ended (4401) or access was revoked (4403): never retry those. */
export const NO_RETRY_CLOSE_CODES = new Set([4401, 4403]);

const MAX_BACKOFF = 30_000;
const HEALTHY_AFTER = 10_000;

export function useConsoleSocket(instanceId: string, onEvent: (msg: ConsoleServerMessage | ConsoleLocalEvent) => void) {
  const handler = useRef(onEvent);
  useLayoutEffect(() => {
    handler.current = onEvent;
  });

  const [state, setState] = useState<ConnState>("connecting");
  const [agentMessage, setAgentMessage] = useState<string | null>(null);
  /** null until the server's hello arrives */
  const [canCommand, setCanCommand] = useState<boolean | null>(null);
  const [containerState, setContainerState] = useState<string | null>(null);
  const [retryAt, setRetryAt] = useState<number | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectRef = useRef<() => void>(() => {});
  const qc = useQueryClient();
  // 4401: the session is gone (logout elsewhere, expiry, password change) -> refresh auth to show the login page.
  const onDenied = useRef<(code: number) => void>(() => {});
  onDenied.current = (code: number) => {
    if (code === 4401) void qc.invalidateQueries({ queryKey: qk.auth });
  };

  useEffect(() => {
    const life = { disposed: false };
    let ws: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let healthyTimer: ReturnType<typeof setTimeout> | undefined;
    let backoff = 1000;
    let everOpened = false;
    let immediate = false;

    const connect = () => {
      if (life.disposed) return;
      clearTimeout(retryTimer);
      setRetryAt(null);
      setState("connecting");
      const sock = new WebSocket(wsUrl(`/ws/instances/${instanceId}/console/`));
      ws = sock;
      wsRef.current = sock;
      sock.onopen = () => {
        healthyTimer = setTimeout(() => {
          backoff = 1000;
        }, HEALTHY_AFTER);
        handler.current({ type: "_open", reconnect: everOpened });
        everOpened = true;
      };
      sock.onmessage = (ev) => {
        let msg: ConsoleServerMessage;
        try {
          msg = JSON.parse(ev.data as string) as ConsoleServerMessage;
        } catch {
          return;
        }
        switch (msg.type) {
          case "hello":
            setCanCommand(!!msg.can_command);
            setState("connected");
            break;
          case "agent":
            setState(msg.connected ? "connected" : "agent_lost");
            setAgentMessage(msg.connected ? null : (msg.message ?? "Lost connection to the node agent"));
            break;
          case "state":
            setContainerState(msg.state);
            break;
          case "closed":
            // The agent ended the log stream; reconnect to resubscribe.
            setState("stream_closed");
            sock.close();
            break;
        }
        handler.current(msg);
      };
      sock.onclose = (ev: CloseEvent) => {
        if (wsRef.current === sock) wsRef.current = null;
        clearTimeout(healthyTimer);
        if (life.disposed || ws !== sock) return;
        handler.current({ type: "_disconnected" });
        if (NO_RETRY_CLOSE_CODES.has(ev.code)) {
          setState("denied");
          setRetryAt(null);
          onDenied.current(ev.code);
          return;
        }
        if (immediate) {
          immediate = false;
          connect();
          return;
        }
        const delay = backoff;
        backoff = Math.min(backoff * 2, MAX_BACKOFF);
        setState((s) => (s === "stream_closed" ? s : "reconnecting"));
        setRetryAt(Date.now() + delay);
        retryTimer = setTimeout(connect, delay);
      };
    };

    reconnectRef.current = () => {
      backoff = 1000;
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        immediate = true;
        ws.close();
      } else {
        connect();
      }
    };

    connect();
    return () => {
      life.disposed = true;
      clearTimeout(retryTimer);
      clearTimeout(healthyTimer);
      reconnectRef.current = () => {};
      closeSocket(ws);
      wsRef.current = null;
    };
  }, [instanceId]);

  const send = useCallback((msg: { type: "command"; id: string; command: string }): boolean => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    ws.send(JSON.stringify(msg));
    return true;
  }, []);

  const reconnectNow = useCallback(() => reconnectRef.current(), []);

  return { state, agentMessage, canCommand, containerState, retryAt, send, reconnectNow };
}
