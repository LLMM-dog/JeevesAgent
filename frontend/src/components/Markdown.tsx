import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Markdown 渲染。
 *
 * memo 是必要的：流式输出时每个 chunk 都会触发重渲染，
 * 不 memo 的话已完成的消息会被反复重新解析 Markdown，长会话下明显卡顿。
 */
const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // 外链在新标签打开，并加 noreferrer（防止目标页拿到来源）
          a: ({ href, children, ...rest }) => (
            <a
              href={href}
              target="_blank"
              rel="noreferrer noopener"
              {...rest}
            >
              {children}
            </a>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
});

export default Markdown;
