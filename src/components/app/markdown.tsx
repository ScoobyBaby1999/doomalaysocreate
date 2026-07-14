'use client'

import ReactMarkdown from 'react-markdown'
import { memo, useState } from 'react'
import { Check, Copy } from 'lucide-react'

/**
 * Markdown renderer for chat/panel responses.
 * Renders code blocks, bold, lists, headers, tables, links.
 * Includes copy-to-clipboard on code blocks.
 */
export const Markdown = memo(function Markdown({ content, className = '' }: { content: string; className?: string }) {
  return (
    <div className={`md-body ${className}`}>
      <ReactMarkdown
        components={{
          pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer noopener">
              {children}
            </a>
          ),
          code: ({ className: cls, children, ...props }) => {
            // Inline code (no language class and not inside <pre>)
            const isBlock = (cls || '').startsWith('language-')
            if (!isBlock) return <code className={cls} {...props}>{children}</code>
            return <code className={cls} {...props}>{children}</code>
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
})

function CodeBlock({ children }: { children: React.ReactNode }) {
  const [copied, setCopied] = useState(false)
  const text = extractText(children)

  return (
    <div className="relative group my-3">
      <button
        onClick={() => {
          navigator.clipboard.writeText(text)
          setCopied(true)
          setTimeout(() => setCopied(false), 1500)
        }}
        className="absolute top-2 right-2 z-10 size-7 rounded-md border border-border/60 bg-background/80 backdrop-blur flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
        aria-label="Copy code"
      >
        {copied ? <Check className="size-3.5 text-emerald-500" /> : <Copy className="size-3.5" />}
      </button>
      <pre className="font-mono text-xs bg-muted/50 p-3 pr-10 rounded-lg overflow-x-auto border border-border">
        {children}
      </pre>
    </div>
  )
}

function extractText(node: React.ReactNode): string {
  if (typeof node === 'string') return node
  if (typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(extractText).join('')
  if (node && typeof node === 'object' && 'props' in node) {
    // @ts-expect-error accessing props.children
    return extractText(node.props?.children)
  }
  return ''
}
