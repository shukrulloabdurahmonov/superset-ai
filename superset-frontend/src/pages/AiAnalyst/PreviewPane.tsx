/** Docked dashboard preview: standalone-mode iframe next to the chat. */
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import { Button } from '@superset-ui/core/components';

const Wrap = styled.div`
  flex: 1;
  display: flex;
  flex-direction: column;
  border-left: 1px solid ${({ theme }) => theme.colorSplit};
  min-width: 0;
`;

const Header = styled.div`
  display: flex;
  align-items: center;
  gap: ${({ theme }) => theme.sizeUnit * 2}px;
  padding: ${({ theme }) => theme.sizeUnit}px
    ${({ theme }) => theme.sizeUnit * 2}px;
  border-bottom: 1px solid ${({ theme }) => theme.colorSplit};
  .spacer {
    flex: 1;
  }
  .path {
    font-family: ${({ theme }) => theme.fontFamilyCode};
    font-size: ${({ theme }) => theme.fontSizeSM}px;
    color: ${({ theme }) => theme.colorTextSecondary};
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
`;

const Frame = styled.iframe`
  flex: 1;
  border: none;
  width: 100%;
`;

export default function PreviewPane({
  url,
  nonce,
  onClose,
}: {
  url: string;
  nonce: number;
  onClose: () => void;
}) {
  const sep = url.includes('?') ? '&' : '?';
  return (
    <Wrap data-test="ai-analyst-preview">
      <Header>
        <span className="path">{url}</span>
        <span className="spacer" />
        <a href={url} target="_blank" rel="noreferrer">
          {t('Open in new tab')}
        </a>
        <Button size="small" onClick={onClose}>
          {t('Close')}
        </Button>
      </Header>
      <Frame
        key={nonce}
        src={`${url}${sep}standalone=3`}
        title={t('Dashboard preview')}
      />
    </Wrap>
  );
}
