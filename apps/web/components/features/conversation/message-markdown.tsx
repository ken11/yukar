"use client";

import type { Components } from "react-markdown";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";

/**
 * MessageMarkdown — agent prose rendered as a document, in the app's
 * instrument language (tokens, hairlines, no cards). Raw HTML is never
 * rendered (react-markdown default).
 *
 * `peak` renders the terrain's high ground: the sentences addressed to the
 * user (questions, conclusions) get the larger composed setting.
 */

const REMARK_PLUGINS = [remarkGfm, remarkBreaks];

function components(peak: boolean): Components {
  const bodyText = peak
    ? "text-[18px] font-medium leading-[1.7] text-on-surface"
    : "text-body-md leading-[var(--leading-prose,1.6)] text-on-surface";
  return {
    p: ({ children }) => <p className={`${bodyText} mb-3 last:mb-0`}>{children}</p>,
    ul: ({ children }) => (
      <ul className={`${bodyText} mb-3 list-disc space-y-1 pl-5 last:mb-0`}>{children}</ul>
    ),
    ol: ({ children }) => (
      <ol className={`${bodyText} mb-3 list-decimal space-y-1 pl-5 last:mb-0`}>{children}</ol>
    ),
    li: ({ children }) => <li>{children}</li>,
    strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
    em: ({ children }) => <em>{children}</em>,
    a: ({ children, href }) => (
      <a
        href={href}
        target="_blank"
        rel="noreferrer noopener"
        className="underline decoration-[var(--color-outline)] underline-offset-2 hover:decoration-[var(--color-on-surface)]"
      >
        {children}
      </a>
    ),
    code: ({ children, className }) => {
      // Block code arrives wrapped in <pre> (handled below); inline code has no language class.
      const isBlock = typeof className === "string" && className.includes("language-");
      if (isBlock) return <code className={className}>{children}</code>;
      return (
        <code
          className="rounded-[3px] border px-1 py-0.5 font-mono text-[0.85em]"
          style={{
            borderColor: "var(--color-outline-variant)",
            backgroundColor: "var(--color-surface-container-low)",
          }}
        >
          {children}
        </code>
      );
    },
    pre: ({ children }) => (
      <pre
        className="mb-3 overflow-x-auto rounded-[3px] border p-3 font-mono text-[12px] leading-relaxed last:mb-0"
        style={{
          borderColor: "var(--color-outline-variant)",
          backgroundColor: "var(--color-surface-container-lowest)",
        }}
      >
        {children}
      </pre>
    ),
    blockquote: ({ children }) => (
      <blockquote
        className="mb-3 border-l-2 pl-3 text-on-surface-variant last:mb-0"
        style={{ borderColor: "var(--color-outline)" }}
      >
        {children}
      </blockquote>
    ),
    // Headings inside chat prose stay modest — the terrain, not the message,
    // owns the large type.
    h1: ({ children }) => <p className={`${bodyText} mb-2 font-semibold`}>{children}</p>,
    h2: ({ children }) => <p className={`${bodyText} mb-2 font-semibold`}>{children}</p>,
    h3: ({ children }) => <p className={`${bodyText} mb-2 font-semibold`}>{children}</p>,
    hr: () => (
      <hr
        className="my-3 border-0 border-t"
        style={{ borderColor: "var(--color-outline-variant)" }}
      />
    ),
    table: ({ children }) => (
      <div className="mb-3 overflow-x-auto last:mb-0">
        <table className="border-collapse text-[13px]">{children}</table>
      </div>
    ),
    th: ({ children }) => (
      <th
        className="border px-2 py-1 text-left font-semibold"
        style={{ borderColor: "var(--color-outline-variant)" }}
      >
        {children}
      </th>
    ),
    td: ({ children }) => (
      <td className="border px-2 py-1" style={{ borderColor: "var(--color-outline-variant)" }}>
        {children}
      </td>
    ),
  };
}

export function MessageMarkdown({ text, peak = false }: { text: string; peak?: boolean }) {
  return (
    <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={components(peak)}>
      {text}
    </ReactMarkdown>
  );
}
