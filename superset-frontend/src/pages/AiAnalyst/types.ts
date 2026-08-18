export type ApprovalState = 'pending' | 'applied' | 'declined' | 'failed';

export type InlineChart = {
  title: string;
  chart_type: 'bar' | 'line' | 'area' | 'pie' | 'scatter';
  rows: Record<string, any>[];
  x: string;
  y: string[];
  series: string | null;
};

export type Msg =
  | { kind: 'user'; text: string; attachments?: string[] }
  | { kind: 'assistant'; text: string }
  | { kind: 'tool'; name: string; args: Record<string, string> }
  | { kind: 'error'; text: string }
  | ({ kind: 'chart' } & InlineChart)
  | { kind: 'embed'; slice_id: number; title: string; url: string }
  | {
      kind: 'approval';
      approvalId: string;
      summary: string;
      specYaml: string;
      state: ApprovalState;
      url?: string;
      detail?: string;
    };

export type Attachment = { name: string; mime: string; data_b64: string };

export type ChatSummary = {
  id: string;
  title: string;
  changed_on: string | null;
};
