/** Message list: bubbles, markdown, grouped tool activity, approval cards. */
import { useEffect, useMemo, useRef } from 'react';
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import { Button, Loading, SafeMarkdown } from '@superset-ui/core/components';
import type { Msg } from './types';

const Feed = styled.div`
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: ${({ theme }) => theme.sizeUnit * 3}px;
  padding: ${({ theme }) => theme.sizeUnit * 4}px
    ${({ theme }) => theme.sizeUnit * 2}px;
`;

const UserBubble = styled.div`
  align-self: flex-end;
  max-width: 75%;
  background: ${({ theme }) => theme.colorPrimaryBg};
  border-radius: 14px 14px 2px 14px;
  padding: ${({ theme }) => theme.sizeUnit * 2}px
    ${({ theme }) => theme.sizeUnit * 3}px;
  white-space: pre-wrap;
  .files {
    margin-top: 4px;
    font-size: ${({ theme }) => theme.fontSizeSM}px;
    color: ${({ theme }) => theme.colorTextSecondary};
  }
`;

const AssistantBlock = styled.div`
  align-self: flex-start;
  max-width: 88%;
  line-height: 1.55;
  table {
    border-collapse: collapse;
  }
  th,
  td {
    border: 1px solid ${({ theme }) => theme.colorSplit};
    padding: 2px 8px;
  }
  pre {
    background: ${({ theme }) => theme.colorBgLayout};
    padding: ${({ theme }) => theme.sizeUnit * 2}px;
    border-radius: 6px;
    overflow-x: auto;
  }
`;

const ToolGroup = styled.details`
  align-self: flex-start;
  color: ${({ theme }) => theme.colorTextTertiary};
  font-size: ${({ theme }) => theme.fontSizeSM}px;
  summary {
    cursor: pointer;
    user-select: none;
  }
  div {
    font-family: ${({ theme }) => theme.fontFamilyCode};
    padding-left: ${({ theme }) => theme.sizeUnit * 4}px;
  }
`;

const ErrorLine = styled.div`
  align-self: flex-start;
  color: ${({ theme }) => theme.colorError};
`;

const ApprovalCard = styled.div`
  align-self: stretch;
  border: 1px solid ${({ theme }) => theme.colorPrimaryBorder};
  border-left: 4px solid ${({ theme }) => theme.colorPrimary};
  border-radius: 8px;
  padding: ${({ theme }) => theme.sizeUnit * 3}px;
  background: ${({ theme }) => theme.colorBgLayout};
  details {
    margin: ${({ theme }) => theme.sizeUnit * 2}px 0;
  }
  pre {
    max-height: 320px;
    overflow: auto;
    font-size: ${({ theme }) => theme.fontSizeSM}px;
  }
  .actions {
    display: flex;
    gap: ${({ theme }) => theme.sizeUnit * 2}px;
  }
`;

const EmbedCard = styled.div`
  align-self: stretch;
  flex: none; /* fixed-height iframe must not shrink when the feed overflows */
  border: 1px solid ${({ theme }) => theme.colorSplit};
  border-radius: 8px;
  overflow: hidden;
  .head {
    display: flex;
    justify-content: space-between;
    padding: ${({ theme }) => theme.sizeUnit}px
      ${({ theme }) => theme.sizeUnit * 2}px;
    border-bottom: 1px solid ${({ theme }) => theme.colorSplit};
    font-weight: 600;
  }
  iframe {
    width: 100%;
    height: 420px;
    border: none;
  }
`;

const Chips = styled.div`
  display: flex;
  flex-wrap: wrap;
  gap: ${({ theme }) => theme.sizeUnit * 2}px;
  button {
    border: 1px solid ${({ theme }) => theme.colorSplit};
    border-radius: 16px;
    background: ${({ theme }) => theme.colorBgContainer};
    padding: 6px 14px;
    cursor: pointer;
    &:hover {
      border-color: ${({ theme }) => theme.colorPrimary};
    }
  }
`;

const SUGGESTIONS = [
  'What data do we have?',
  'Build a dashboard summarizing our main table',
  'Any interesting trends in the last month?',
];

type Grouped =
  | { g: 'msg'; m: Msg }
  | { g: 'tools'; items: Extract<Msg, { kind: 'tool' }>[] };

export default function ChatFeed({
  messages,
  busy,
  onApproval,
  onOpen,
  onSuggestion,
}: {
  messages: Msg[];
  busy: boolean;
  onApproval: (approvalId: string, approve: boolean) => void;
  onOpen: (url: string) => void;
  onSuggestion: (text: string) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.scrollTo({ top: ref.current.scrollHeight });
  }, [messages, busy]);

  const grouped = useMemo<Grouped[]>(() => {
    const out: Grouped[] = [];
    messages.forEach(m => {
      if (m.kind === 'tool') {
        const last = out[out.length - 1];
        if (last?.g === 'tools') last.items.push(m);
        else out.push({ g: 'tools', items: [m] });
      } else out.push({ g: 'msg', m });
    });
    return out;
  }, [messages]);

  return (
    <Feed ref={ref} data-test="ai-analyst-feed">
      {messages.length === 0 && (
        <AssistantBlock>
          <SafeMarkdown
            source={t(
              "Hi! I can explore your data, answer questions about it, and " +
                'build or modify dashboards. Try one of these:',
            )}
          />
          <Chips>
            {SUGGESTIONS.map(s => (
              <button type="button" key={s} onClick={() => onSuggestion(s)}>
                {s}
              </button>
            ))}
          </Chips>
        </AssistantBlock>
      )}
      {grouped.map((entry, i) => {
        if (entry.g === 'tools')
          return (
            <ToolGroup key={i}>
              <summary>
                🔧{' '}
                {t('%s step(s)', entry.items.length)}
              </summary>
              {entry.items.map((tm, j) => (
                <div key={j}>
                  {tm.name}(
                  {Object.entries(tm.args)
                    .map(([k, v]) => `${k}=${String(v).slice(0, 60)}`)
                    .join(', ')}
                  )
                </div>
              ))}
            </ToolGroup>
          );
        const { m } = entry;
        if (m.kind === 'user')
          return (
            <UserBubble key={i}>
              {m.text}
              {m.attachments?.length ? (
                <div className="files">📎 {m.attachments.join(', ')}</div>
              ) : null}
            </UserBubble>
          );
        if (m.kind === 'assistant')
          return (
            <AssistantBlock key={i}>
              <SafeMarkdown source={m.text} />
            </AssistantBlock>
          );
        if (m.kind === 'error') return <ErrorLine key={i}>{m.text}</ErrorLine>;
        if (m.kind === 'embed')
          return (
            <EmbedCard key={i} data-test="ai-analyst-embed">
              <div className="head">
                <span>{m.title || t('Chart')}</span>
                <a href={m.url} target="_blank" rel="noreferrer">
                  {t('Open in new tab')}
                </a>
              </div>
              <iframe
                src={`${m.url}${m.url.includes('?') ? '&' : '?'}standalone=1`}
                title={m.title || t('Embedded chart')}
              />
            </EmbedCard>
          );
        // transcripts from older versions may contain kinds we no longer
        // render (e.g. the removed inline 'chart'); skip them silently
        if (m.kind !== 'approval') return null;
        return (
          <ApprovalCard key={i} data-test="ai-analyst-approval">
            <strong>{t('Apply to Superset?')}</strong>
            <p>{m.summary}</p>
            <details>
              <summary>{t('Show spec')}</summary>
              <pre>{m.specYaml}</pre>
            </details>
            {m.state === 'pending' && (
              <div className="actions">
                <Button
                  buttonStyle="primary"
                  onClick={() => onApproval(m.approvalId, true)}
                >
                  {t('Apply')}
                </Button>
                <Button onClick={() => onApproval(m.approvalId, false)}>
                  {t('Decline')}
                </Button>
              </div>
            )}
            {m.state === 'applied' && (
              <div className="actions">
                <span>✅ {t('Applied.')}</span>
                {m.url && (
                  <Button
                    buttonStyle="primary"
                    onClick={() => onOpen(m.url as string)}
                  >
                    {t('Open')}
                  </Button>
                )}
                {m.url && (
                  <a href={m.url} target="_blank" rel="noreferrer">
                    {t('Open in new tab')}
                  </a>
                )}
              </div>
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
  );
}
