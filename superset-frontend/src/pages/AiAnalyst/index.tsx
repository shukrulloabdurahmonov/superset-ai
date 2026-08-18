/**
 * AI Analyst chat page (fork-local).
 *
 * Streams /api/v1/ai_analyst/chat SSE events into a conversation view and
 * renders approval cards for gated applies. All state is client-side; the
 * backend keeps the agent session keyed by session_id.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import {
  Button,
  Input,
  Loading,
  SafeMarkdown,
} from '@superset-ui/core/components';

type ApprovalState = 'pending' | 'applied' | 'declined' | 'failed';

type Msg =
  | { kind: 'user'; text: string }
  | { kind: 'assistant'; text: string }
  | { kind: 'tool'; name: string; args: Record<string, string> }
  | { kind: 'error'; text: string }
  | {
      kind: 'approval';
      approvalId: string;
      summary: string;
      specYaml: string;
      state: ApprovalState;
      url?: string;
      detail?: string;
    };

const Page = styled.div`
  display: flex;
  flex-direction: column;
  height: calc(100vh - 100px);
  max-width: 920px;
  margin: 0 auto;
  padding: ${({ theme }) => theme.sizeUnit * 4}px;
`;

const Feed = styled.div`
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: ${({ theme }) => theme.sizeUnit * 3}px;
  padding-bottom: ${({ theme }) => theme.sizeUnit * 4}px;
`;

const UserBubble = styled.div`
  align-self: flex-end;
  max-width: 75%;
  background: ${({ theme }) => theme.colorPrimaryBg};
  border-radius: 12px;
  padding: ${({ theme }) => theme.sizeUnit * 2}px
    ${({ theme }) => theme.sizeUnit * 3}px;
  white-space: pre-wrap;
`;

const AssistantBlock = styled.div`
  align-self: flex-start;
  max-width: 85%;
`;

const ToolLine = styled.div`
  align-self: flex-start;
  color: ${({ theme }) => theme.colorTextTertiary};
  font-family: ${({ theme }) => theme.fontFamilyCode};
  font-size: ${({ theme }) => theme.fontSizeSM}px;
`;

const ErrorLine = styled.div`
  align-self: flex-start;
  color: ${({ theme }) => theme.colorError};
`;

const ApprovalCard = styled.div`
  align-self: stretch;
  border: 1px solid ${({ theme }) => theme.colorPrimaryBorder};
  border-radius: 8px;
  padding: ${({ theme }) => theme.sizeUnit * 3}px;
  background: ${({ theme }) => theme.colorBgLayout};

  details {
    margin-top: ${({ theme }) => theme.sizeUnit * 2}px;
  }
  pre {
    max-height: 320px;
    overflow: auto;
    font-size: ${({ theme }) => theme.fontSizeSM}px;
  }
`;

const Composer = styled.div`
  display: flex;
  gap: ${({ theme }) => theme.sizeUnit * 2}px;
  padding-top: ${({ theme }) => theme.sizeUnit * 2}px;
  border-top: 1px solid ${({ theme }) => theme.colorSplit};
`;

async function csrfToken(): Promise<string> {
  const res = await fetch('/api/v1/security/csrf_token/', {
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  });
  return (await res.json()).result;
}

export default function AiAnalyst() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const sessionRef = useRef<string | null>(null);
  const feedRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    feedRef.current?.scrollTo({ top: feedRef.current.scrollHeight });
  }, [messages]);

  const push = useCallback(
    (m: Msg) => setMessages(prev => [...prev, m]),
    [],
  );

  const send = useCallback(async () => {
    const message = input.trim();
    if (!message || busy) return;
    setInput('');
    push({ kind: 'user', text: message });
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
          session_id: sessionRef.current,
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
          if (event === 'session') sessionRef.current = data.session_id;
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
          else if (event === 'error')
            push({ kind: 'error', text: data.message });
        }
      }
    } catch (e) {
      push({ kind: 'error', text: String(e) });
    } finally {
      setBusy(false);
    }
  }, [busy, input, push]);

  const resolveApproval = useCallback(
    async (approvalId: string, approve: boolean) => {
      const token = await csrfToken();
      let state: ApprovalState = approve ? 'applied' : 'declined';
      let url: string | undefined;
      let detail: string | undefined;
      try {
        const res = await fetch('/api/v1/ai_analyst/apply', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': token,
          },
          body: JSON.stringify({
            session_id: sessionRef.current,
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
    },
    [],
  );

  return (
    <Page>
      <h2>{t('AI Analyst')}</h2>
      <Feed ref={feedRef} data-test="ai-analyst-feed">
        {messages.length === 0 && (
          <AssistantBlock>
            <SafeMarkdown
              source={t(
                'Ask me about your data, or ask me to build or change a ' +
                  'dashboard — for example: *"What tables do we have?"* or ' +
                  '*"Build a dashboard summarizing orders by region."*',
              )}
            />
          </AssistantBlock>
        )}
        {messages.map((m, i) => {
          if (m.kind === 'user') return <UserBubble key={i}>{m.text}</UserBubble>;
          if (m.kind === 'assistant')
            return (
              <AssistantBlock key={i}>
                <SafeMarkdown source={m.text} />
              </AssistantBlock>
            );
          if (m.kind === 'tool')
            return (
              <ToolLine key={i}>
                · {m.name}(
                {Object.entries(m.args)
                  .map(([k, v]) => `${k}=${v.slice(0, 60)}`)
                  .join(', ')}
                )
              </ToolLine>
            );
          if (m.kind === 'error') return <ErrorLine key={i}>{m.text}</ErrorLine>;
          return (
            <ApprovalCard key={i} data-test="ai-analyst-approval">
              <strong>{t('Apply to Superset?')}</strong>
              <p>{m.summary}</p>
              <details>
                <summary>{t('Show spec')}</summary>
                <pre>{m.specYaml}</pre>
              </details>
              {m.state === 'pending' && (
                <>
                  <Button
                    buttonStyle="primary"
                    onClick={() => resolveApproval(m.approvalId, true)}
                  >
                    {t('Apply')}
                  </Button>{' '}
                  <Button onClick={() => resolveApproval(m.approvalId, false)}>
                    {t('Decline')}
                  </Button>
                </>
              )}
              {m.state === 'applied' && (
                <p>
                  ✅ {t('Applied.')}{' '}
                  {m.url && <a href={m.url}>{t('Open the dashboard')}</a>}
                </p>
              )}
              {m.state === 'declined' && <p>🚫 {t('Declined.')}</p>}
              {m.state === 'failed' && (
                <ErrorLine>
                  {t('Apply failed:')} {m.detail}
                </ErrorLine>
              )}
            </ApprovalCard>
          );
        })}
        {busy && <Loading position="inline-centered" />}
      </Feed>
      <Composer>
        <Input.TextArea
          autoSize={{ minRows: 1, maxRows: 6 }}
          value={input}
          disabled={busy}
          placeholder={t('Ask about your data or describe a dashboard…')}
          onChange={e => setInput(e.target.value)}
          onPressEnter={e => {
            if (!e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          data-test="ai-analyst-input"
        />
        <Button buttonStyle="primary" disabled={busy} onClick={send}>
          {t('Send')}
        </Button>
      </Composer>
    </Page>
  );
}
