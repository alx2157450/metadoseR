// 文本来源标记（E6）：人工输入 / 接受的 AI 建议 / 外部粘贴。
export type Span = { text: string; origin: "human" | "ai_accepted" | "pasted"; ai_run?: string | null };

/** 在 [start, end) 处用 insert 替换，插入部分标记为 origin。 */
export function spliceSpans(spans: Span[], start: number, end: number, insert: string, origin: Span["origin"]): Span[] {
  const out: Span[] = [];
  let pos = 0;
  let inserted = false;
  const put = (s: Span) => {
    if (!s.text) return;
    const last = out[out.length - 1];
    if (last && last.origin === s.origin && (last.ai_run ?? null) === (s.ai_run ?? null)) last.text += s.text;
    else out.push({ ...s });
  };
  for (const s of spans) {
    const a = pos;
    const b = pos + s.text.length;
    pos = b;
    if (b <= start) {
      put(s);
      continue;
    }
    if (a >= end) {
      if (!inserted) { put({ text: insert, origin }); inserted = true; }
      put(s);
      continue;
    }
    if (a < start) put({ ...s, text: s.text.slice(0, start - a) });
    if (!inserted) { put({ text: insert, origin }); inserted = true; }
    if (b > end) put({ ...s, text: s.text.slice(end - a) });
  }
  if (!inserted) put({ text: insert, origin });
  return out;
}

