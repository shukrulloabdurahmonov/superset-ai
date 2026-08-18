/** Message composer: text, attachments (button / drag-drop / paste),
 * plan-mode toggle, send. */
import { useCallback, useRef, useState } from 'react';
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import { Button, Input, Switch, Tooltip } from '@superset-ui/core/components';
import type { Attachment } from './types';

const MAX_TOTAL_BYTES = 12 * 1024 * 1024;

const Wrap = styled.div`
  border-top: 1px solid ${({ theme }) => theme.colorSplit};
  padding: ${({ theme }) => theme.sizeUnit * 2}px;
  display: flex;
  flex-direction: column;
  gap: ${({ theme }) => theme.sizeUnit}px;
  &.dragging {
    outline: 2px dashed ${({ theme }) => theme.colorPrimary};
    outline-offset: -4px;
  }
`;

const Row = styled.div`
  display: flex;
  gap: ${({ theme }) => theme.sizeUnit * 2}px;
  align-items: flex-end;
`;

const Bar = styled.div`
  display: flex;
  align-items: center;
  gap: ${({ theme }) => theme.sizeUnit * 2}px;
  font-size: ${({ theme }) => theme.fontSizeSM}px;
  color: ${({ theme }) => theme.colorTextSecondary};
`;

const Chip = styled.span`
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 1px solid ${({ theme }) => theme.colorSplit};
  border-radius: 12px;
  padding: 1px 8px;
  button {
    border: none;
    background: none;
    cursor: pointer;
    color: ${({ theme }) => theme.colorTextTertiary};
  }
`;

function fileToAttachment(file: File): Promise<Attachment> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || '');
      resolve({
        name: file.name || 'pasted-image.png',
        mime: file.type || 'text/plain',
        data_b64: result.slice(result.indexOf(',') + 1),
      });
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

export default function Composer({
  busy,
  planMode,
  onPlanMode,
  onSend,
  draft,
  onDraft,
}: {
  busy: boolean;
  planMode: boolean;
  onPlanMode: (v: boolean) => void;
  onSend: (text: string, attachments: Attachment[]) => void;
  draft: string;
  onDraft: (v: string) => void;
}) {
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const addFiles = useCallback(async (files: FileList | File[]) => {
    const added = await Promise.all(Array.from(files).map(fileToAttachment));
    setAttachments(prev => {
      const next = [...prev, ...added];
      const total = next.reduce((s, a) => s + a.data_b64.length * 0.75, 0);
      if (total > MAX_TOTAL_BYTES) {
        // eslint-disable-next-line no-alert
        return prev;
      }
      return next;
    });
  }, []);

  const submit = useCallback(() => {
    if (busy || !draft.trim()) return;
    onSend(draft, attachments);
    onDraft('');
    setAttachments([]);
  }, [busy, draft, attachments, onSend, onDraft]);

  return (
    <Wrap
      className={dragging ? 'dragging' : ''}
      onDragOver={e => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={e => {
        e.preventDefault();
        setDragging(false);
        if (e.dataTransfer.files?.length) addFiles(e.dataTransfer.files);
      }}
    >
      {attachments.length > 0 && (
        <Bar>
          {attachments.map((a, i) => (
            <Chip key={i}>
              📎 {a.name}
              <button
                type="button"
                aria-label={t('Remove attachment')}
                onClick={() =>
                  setAttachments(prev => prev.filter((_, j) => j !== i))
                }
              >
                ✕
              </button>
            </Chip>
          ))}
        </Bar>
      )}
      <Row>
        <Tooltip title={t('Attach images or text/CSV files')}>
          <Button onClick={() => fileRef.current?.click()}>📎</Button>
        </Tooltip>
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          accept="image/png,image/jpeg,image/gif,image/webp,.csv,.txt,.json,.sql,.yaml,.yml,.md,text/*"
          onChange={e => {
            if (e.target.files?.length) addFiles(e.target.files);
            e.target.value = '';
          }}
        />
        <Input.TextArea
          autoSize={{ minRows: 1, maxRows: 8 }}
          value={draft}
          disabled={busy}
          placeholder={t('Ask about your data or describe a dashboard…')}
          onChange={e => onDraft(e.target.value)}
          onPaste={e => {
            const files = Array.from(e.clipboardData?.files || []);
            if (files.length) {
              e.preventDefault();
              addFiles(files);
            }
          }}
          onPressEnter={e => {
            if (!e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          data-test="ai-analyst-input"
        />
        <Button buttonStyle="primary" disabled={busy} onClick={submit}>
          {t('Send')}
        </Button>
      </Row>
      <Bar>
        <Switch
          checked={planMode}
          onChange={(v: boolean) => onPlanMode(v)}
          size="small"
        />
        <Tooltip
          title={t(
            'When on, dashboard builds start with a plan you confirm before ' +
              'anything is created.',
          )}
        >
          <span>{t('Plan first')}</span>
        </Tooltip>
      </Bar>
    </Wrap>
  );
}
