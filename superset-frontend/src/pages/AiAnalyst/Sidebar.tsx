/** Saved-chat list for the AI Analyst page. */
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import { Button } from '@superset-ui/core/components';
import type { ChatSummary } from './types';

const Wrap = styled.div`
  width: 232px;
  flex: none;
  display: flex;
  flex-direction: column;
  border-right: 1px solid ${({ theme }) => theme.colorSplit};
  background: ${({ theme }) => theme.colorBgLayout};
  padding: ${({ theme }) => theme.sizeUnit * 2}px;
  gap: ${({ theme }) => theme.sizeUnit * 2}px;
  overflow-y: auto;
`;

const Item = styled.div<{ active: boolean }>`
  display: flex;
  align-items: center;
  gap: 4px;
  border-radius: 6px;
  padding: ${({ theme }) => theme.sizeUnit}px
    ${({ theme }) => theme.sizeUnit * 2}px;
  cursor: pointer;
  background: ${({ theme, active }) =>
    active ? theme.colorPrimaryBg : 'transparent'};
  &:hover {
    background: ${({ theme }) => theme.colorPrimaryBgHover};
  }
  .title {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: ${({ theme }) => theme.fontSizeSM}px;
  }
  .del {
    visibility: hidden;
    border: none;
    background: none;
    cursor: pointer;
    color: ${({ theme }) => theme.colorTextTertiary};
  }
  &:hover .del {
    visibility: visible;
  }
`;

export default function Sidebar({
  chats,
  activeId,
  onOpen,
  onNew,
  onDelete,
}: {
  chats: ChatSummary[];
  activeId: string | null;
  onOpen: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
}) {
  return (
    <Wrap data-test="ai-analyst-sidebar">
      <Button buttonStyle="secondary" onClick={onNew}>
        {t('+ New chat')}
      </Button>
      {chats.map(c => (
        <Item
          key={c.id}
          active={c.id === activeId}
          onClick={() => onOpen(c.id)}
        >
          <span className="title" title={c.title}>
            {c.title}
          </span>
          <button
            type="button"
            className="del"
            aria-label={t('Delete chat')}
            onClick={e => {
              e.stopPropagation();
              onDelete(c.id);
            }}
          >
            ✕
          </button>
        </Item>
      ))}
    </Wrap>
  );
}
