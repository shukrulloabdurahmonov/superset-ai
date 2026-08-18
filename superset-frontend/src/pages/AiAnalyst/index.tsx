/**
 * AI Analyst chat page (fork-local).
 *
 * Layout: [Sidebar: saved chats] [Chat pane] [Preview pane (optional)].
 * Opening an applied dashboard docks the chat to the left and renders the
 * dashboard in a standalone-mode iframe; further prompts keep editing it and
 * every apply refreshes the preview.
 */
import { useState } from 'react';
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import ChatFeed from './ChatFeed';
import Composer from './Composer';
import PreviewPane from './PreviewPane';
import Sidebar from './Sidebar';
import { useChat } from './useChat';

const Page = styled.div`
  display: flex;
  height: calc(100vh - 66px);
  background: ${({ theme }) => theme.colorBgContainer};
`;

const ChatPane = styled.div<{ docked: boolean }>`
  display: flex;
  flex-direction: column;
  min-width: 0;
  ${({ docked }) =>
    docked ? 'flex: none; width: 440px;' : 'flex: 1; max-width: 960px;'}
  margin: ${({ docked }) => (docked ? '0' : '0 auto')};
  border-right: ${({ docked, theme }) =>
    docked ? `1px solid ${theme.colorSplit}` : 'none'};
`;

const Title = styled.div`
  display: flex;
  align-items: center;
  gap: ${({ theme }) => theme.sizeUnit * 2}px;
  padding: ${({ theme }) => theme.sizeUnit * 2}px
    ${({ theme }) => theme.sizeUnit * 3}px 0;
  h2 {
    margin: 0;
  }
  .sidebar-toggle {
    border: 1px solid ${({ theme }) => theme.colorSplit};
    border-radius: 6px;
    background: ${({ theme }) => theme.colorBgContainer};
    cursor: pointer;
    padding: 2px 8px;
    color: ${({ theme }) => theme.colorTextSecondary};
  }
`;

export default function AiAnalyst() {
  const {
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
  } = useChat();
  const [draft, setDraft] = useState('');
  const [showSidebar, setShowSidebar] = useState(true);

  return (
    <Page>
      {showSidebar && (
        <Sidebar
          chats={chats}
          activeId={chatId}
          onOpen={openChat}
          onNew={newChat}
          onDelete={removeChat}
        />
      )}
      <ChatPane docked={!!preview}>
        <Title>
          <button
            type="button"
            className="sidebar-toggle"
            title={showSidebar ? t('Hide chat history') : t('Show chat history')}
            onClick={() => setShowSidebar(v => !v)}
          >
            {showSidebar ? '◀' : '▶'}
          </button>
          <h2>{t('AI Analyst')}</h2>
        </Title>
        <ChatFeed
          messages={messages}
          busy={busy}
          onApproval={resolveApproval}
          onOpen={openPreview}
          onSuggestion={setDraft}
          onPlanApprove={() =>
            send('Approved — proceed and build exactly that plan.', [])
          }
        />
        <Composer
          busy={busy}
          planMode={planMode}
          onPlanMode={setPlanMode}
          onSend={send}
          draft={draft}
          onDraft={setDraft}
        />
      </ChatPane>
      {preview && (
        <PreviewPane
          url={preview.url}
          nonce={preview.nonce}
          onClose={closePreview}
        />
      )}
    </Page>
  );
}
