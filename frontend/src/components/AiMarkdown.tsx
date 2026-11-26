import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type Props = {
  children: string;
  className?: string;
};

/**
 * Normalize GFM pipe-tables when the model collapses rows onto one line
 * (e.g. `| a | b || :--- | :--- || c | d |`). Real newlines keep remark-gfm happy.
 */
function normalizePipeTables(src: string): string {
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  const out: string[] = [];

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed.includes("|") || (trimmed.match(/\|/g) || []).length < 2) {
      out.push(line);
      continue;
    }
    // Split jammed rows: "... | || next |" or "... | | :--- |"
    const parts = trimmed
      .split(/\|\s*\|(?=\s*[:\-|*\dA-Za-z+])/g)
      .map((p) => p.trim())
      .filter(Boolean);
    if (parts.length <= 1) {
      out.push(line);
      continue;
    }
    for (const part of parts) {
      let row = part;
      if (!row.startsWith("|")) row = `| ${row}`;
      if (!row.endsWith("|")) row = `${row} |`;
      out.push(row);
    }
  }
  return out.join("\n");
}

/**
 * Renders AI Agent replies as Markdown (GFM tables, bold, lists, links, code).
 * Plain text still works; Malayalam / Unicode pass through unchanged.
 *
 * Table cells wrap content in `.ai-md-cell` so Snippet/Context can line-clamp
 * without breaking table layout (display:-webkit-box on td is unreliable).
 */
export default function AiMarkdown({ children, className }: Props) {
  const text = normalizePipeTables((children ?? "").trimEnd());
  if (!text) return null;

  return (
    <div className={`ai-md ${className ?? ""}`.trim()}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children: linkChildren }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {linkChildren}
            </a>
          ),
          table: ({ children: tableChildren }) => (
            <div className="ai-md-table-wrap">
              <table>{tableChildren}</table>
            </div>
          ),
          th: ({ children: thChildren }) => (
            <th>
              <div className="ai-md-cell">{thChildren}</div>
            </th>
          ),
          td: ({ children: tdChildren }) => (
            <td>
              <div className="ai-md-cell">{tdChildren}</div>
            </td>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
