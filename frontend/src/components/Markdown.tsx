import type { ReactNode } from "react";
import { parseMarkdown, type Inline } from "../lib/markdown";

interface Props {
  text: string;
  /** Called with a citation label such as "E3". Labels not in `known` render as plain text. */
  onCite?: (label: string) => void;
  known?: Set<string>;
}

function renderInlines(inlines: Inline[], { onCite, known }: Props): ReactNode[] {
  return inlines.map((inline, i) => {
    switch (inline.kind) {
      case "code":
        return <code key={i}>{inline.text}</code>;
      case "bold":
        return <strong key={i}>{inline.text}</strong>;
      case "cite":
        return (
          <span key={i} className="cites">
            [
            {inline.labels.map((label, j) => {
              const clickable = !!onCite && (!known || known.has(label));
              return (
                <span key={label + j}>
                  {j > 0 && ", "}
                  {clickable ? (
                    <button type="button" className="cite" onClick={() => onCite?.(label)}>
                      {label}
                    </button>
                  ) : (
                    label
                  )}
                </span>
              );
            })}
            ]
          </span>
        );
      default:
        return <span key={i}>{inline.text}</span>;
    }
  });
}

/** Renders a safe markdown subset. Every string is a React text node, so no HTML is ever injected. */
export default function Markdown(props: Props) {
  const blocks = parseMarkdown(props.text);
  return (
    <div className="markdown">
      {blocks.map((block, i) => {
        switch (block.kind) {
          case "heading": {
            const Tag = `h${Math.min(6, Math.max(3, block.level + 1))}` as "h3" | "h4" | "h5" | "h6";
            return <Tag key={i}>{renderInlines(block.inlines, props)}</Tag>;
          }
          case "list": {
            const Tag = block.ordered ? "ol" : "ul";
            return (
              <Tag key={i}>
                {block.items.map((item, j) => (
                  <li key={j}>{renderInlines(item, props)}</li>
                ))}
              </Tag>
            );
          }
          case "code":
            return (
              <pre key={i}>
                <code>{block.text}</code>
              </pre>
            );
          case "quote":
            return <blockquote key={i}>{renderInlines(block.inlines, props)}</blockquote>;
          default:
            return <p key={i}>{renderInlines(block.inlines, props)}</p>;
        }
      })}
    </div>
  );
}
