/**
 * Chat state + API access for the AI Analyst page: SSE streaming, saved
 * chats, approvals, and the dashboard preview pane.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import type { ApprovalState, Attachment, ChatSummary, Msg } from './types';

async function csrfToken(): Promise<string> {
  const res = await fetch('/api/v1/security/csrf_token/', {
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  });
  return (await res.json()).result;
}

async function jsonFetch(url: string, init: RequestInit = {}) {
  const res = await fetch(url, { credentials: 'same-origin', ...init });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  return res.json();
}

export function useChat() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [busy, setBusy] = useState(false);
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [chatId, setChatId] = useState<string | null>(null);
  const [planMode, setPlanMode] = useState(true);
  const [preview, setPreview] = useState<{ url: string; nonce: number } | null>(
    null,
  );
  const chatIdRef = useRef<string | null>(null);
  chatIdRef.current = chatId;

  const push = useCallback((m: Msg) => setMessages(prev => [...prev, m]), []);

  const loadChats = useCallback(async () => {
    try {
      const body = await jsonFetch('/api/v1/ai_analyst/chats');
      setChats(body.result || []);
    } catch {
      // sidebar is non-critical
    }
  }, []);

  useEffect(() => {
    loadChats();
  }, [loadChats]);

  const send = useCallback(
    async (text: string, attachments: Attachment[]) => {
      const message = text.trim();
      if (!message || busy) return;
      push({
        kind: 'user',
        text: message,
        ...(attachments.length
          ? { attachments: attachments.map(a => a.name) }
          : {}),
      });
      setBusy(true);
      try {
        const token = await csrfToken();
        const res = await fetch('/api/v1/ai_analyst/chat', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': token,
          },
          body: JSON.stringify({
            message,
            chat_id: chatIdRef.current,
            plan_mode: planMode,
            attachments,
          }),
        });
        if (!res.ok || !res.body) {
          push({ kind: 'error', text: `${res.status}: ${await res.text()}` });
          return;
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';
        let event = '';
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          let idx = buf.indexOf('\n');
          while (idx >= 0) {
            const line = buf.slice(0, idx).trimEnd();
            buf = buf.slice(idx + 1);
            idx = buf.indexOf('\n');
            if (line.startsWith('event: ')) event = line.slice(7);
            if (!line.startsWith('data: ')) continue; // eslint-disable-line no-continue
            const data = JSON.parse(line.slice(6));
            if (event === 'chat') setChatId(data.chat_id);
            else if (event === 'text')
              push({ kind: 'assistant', text: data.text });
            else if (event === 'tool')
              push({ kind: 'tool', name: data.name, args: data.args });
            else if (event === 'approval_request')
              push({
                kind: 'approval',
                approvalId: data.approval_id,
                summary: data.summary,
                specYaml: data.spec_yaml,
                state: 'pending',
              });
            else if (event === 'embed') push({ kind: 'embed', ...data });
            else if (event === 'plan')
              push({ kind: 'plan', text: data.text });
            else if (event === 'error')
              push({ kind: 'error', text: data.message });
          }
        }
      } catch (e) {
        push({ kind: 'error', text: String(e) });
      } finally {
        setBusy(false);
        loadChats();
      }
    },
    [busy, planMode, push, loadChats],
  );

  const resolveApproval = useCallback(
    async (approvalId: string, approve: boolean) => {
      let state: ApprovalState = approve ? 'applied' : 'declined';
      let url: string | undefined;
      let detail: string | undefined;
      try {
        const token = await csrfToken();
        const res = await fetch('/api/v1/ai_analyst/apply', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': token,
          },
          body: JSON.stringify({
            chat_id: chatIdRef.current,
            approval_id: approvalId,
            approve,
          }),
        });
        const body = await res.json();
        if (!res.ok) {
          state = 'failed';
          detail = body.message || res.statusText;
        } else if (approve) {
          url = body.result?.url;
        }
      } catch (e) {
        state = 'failed';
        detail = String(e);
      }
      setMessages(prev =>
        prev.map(m =>
          m.kind === 'approval' && m.approvalId === approvalId
            ? { ...m, state, url, detail }
            : m,
        ),
      );
      // a re-apply to the dashboard that is open in the preview pane
      // refreshes it in place
      if (url) {
        setPreview(prev =>
          prev && prev.url === url
            ? { url, nonce: prev.nonce + 1 }
            : prev,
        );
      }
      return url;
    },
    [],
  );

  const openChat = useCallback(async (id: string) => {
    const body = await jsonFetch(`/api/v1/ai_analyst/chats/${id}`);
    setChatId(body.result.chat_id);
    setMessages(body.result.transcript || []);
    setPreview(null);
  }, []);

  const newChat = useCallback(() => {
    setChatId(null);
    setMessages([]);
    setPreview(null);
  }, []);

  const removeChat = useCallback(
    async (id: string) => {
      const token = await csrfToken();
      await fetch(`/api/v1/ai_analyst/chats/${id}`, {
        method: 'DELETE',
        credentials: 'same-origin',
        headers: { 'X-CSRFToken': token },
      });
      if (chatIdRef.current === id) newChat();
      loadChats();
    },
    [loadChats, newChat],
  );

  const openPreview = useCallback(
    (url: string) => setPreview({ url, nonce: 1 }),
    [],
  );
  const closePreview = useCallback(() => setPreview(null), []);

  return {
    messages,
    busy,
    chats,
    chatId,
    planMode,
    setPlanMode,
    preview,
    openPreview,
    closePreview,
    send,
    resolveApproval,
    openChat,
    newChat,
    removeChat,
  };
}
