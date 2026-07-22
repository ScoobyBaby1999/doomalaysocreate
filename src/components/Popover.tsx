/**
 * Popover — a portal-rendered popover that escapes overflow clipping and
 * backdrop-filter containing blocks.
 *
 * Why a portal?
 *  - The chat input area + header use `backdrop-filter` (for the glassy
 *    glass-morphism look). Per CSS spec, `backdrop-filter` creates a
 *    containing block for `position: fixed` descendants — which means a
 *    normal `fixed inset-0` backdrop would be SCOPED to the header/input
 *    area, not the viewport. Clicking outside wouldn't close the popover.
 *  - The tool bar uses `overflow-x-auto`, which (per CSS spec) normalises
 *    `overflow-y: visible` to `auto`. So `position: absolute` popovers
 *    extending up/down out of the tool bar get CLIPPED.
 *
 * Rendering into document.body via createPortal sidesteps both issues:
 * the popover + backdrop are siblings of #root, with no ancestor overflow
 * or backdrop-filter to mess with positioning.
 *
 * Position is computed from the anchor element's bounding rect on open,
 * and updated on scroll/resize so the popover tracks the anchor.
 */
import {
  useEffect,
  useLayoutEffect,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";

export interface PopoverProps {
  open: boolean;
  onClose: () => void;
  /** The element the popover is anchored to. Position is computed from its
   *  getBoundingClientRect() on open + on scroll/resize. */
  anchorRef: RefObject<HTMLElement | null>;
  children: ReactNode;
  /** Horizontal alignment relative to the anchor. */
  align?: "left" | "right";
  /** Direction the popover extends from the anchor. Defaults to "up" (popover
   *  opens above the anchor — used for tool bars at the bottom of the screen).
   *  Use "down" for header dropdowns. */
  direction?: "up" | "down";
  /** Pixel width of the popover. Defaults to 240. */
  width?: number;
  /** Optional title row rendered at the top with a close button. */
  title?: string;
  /** Extra className to merge onto the popover panel. */
  className?: string;
  /** z-index of the popover panel. Backdrop is always 1 below. Defaults to 50. */
  zIndex?: number;
}

export function Popover({
  open,
  onClose,
  anchorRef,
  children,
  align = "left",
  direction = "up",
  width = 240,
  title,
  className = "",
  zIndex = 50,
}: PopoverProps) {
  // Compute anchor position on open + track changes. We re-read on scroll
  // (capture phase so we catch scrolls in any ancestor) and on resize.
  const [rect, setRect] = useState<DOMRect | null>(null);
  const [vw, setVw] = useState(
    typeof window !== "undefined" ? window.innerWidth : 0,
  );

  useLayoutEffect(() => {
    if (!open) return;
    const anchor = anchorRef.current;
    if (!anchor) return;
    const update = () => {
      setRect(anchor.getBoundingClientRect());
      setVw(window.innerWidth);
    };
    update();
    // Capture so we catch scrolls inside scroll containers (e.g. the chat
    // message list) before they're handled.
    window.addEventListener("scroll", update, true);
    window.addEventListener("resize", update);
    return () => {
      window.removeEventListener("scroll", update, true);
      window.removeEventListener("resize", update);
    };
  }, [open, anchorRef]);

  // Close on Escape — same UX as the existing ToolIcons popovers.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open || !rect) return null;

  // Compute position. We clamp inside the viewport so the popover doesn't
  // get cut off on small screens.
  const gap = 8;
  const popoverWidth = Math.min(width, vw - 16);
  let left: number;
  if (align === "right") {
    left = rect.right - popoverWidth;
  } else {
    left = rect.left;
  }
  // Horizontal clamp
  left = Math.max(8, Math.min(left, vw - popoverWidth - 8));

  let top: number;
  if (direction === "up") {
    // Popover opens above the anchor. `top` is the top edge of the popover.
    top = rect.top - gap; // we'll translate-y(-100%) via transform
  } else {
    top = rect.bottom + gap;
  }

  const panelStyle: React.CSSProperties = {
    position: "fixed",
    top,
    left,
    width: popoverWidth,
    zIndex,
    ...(direction === "up" ? { transform: "translateY(-100%)" } : {}),
  };

  return createPortal(
    <>
      {/* Backdrop — covers the full viewport, captures outside-clicks. */}
      <div
        style={{ position: "fixed", inset: 0, zIndex: zIndex - 10 }}
        onClick={onClose}
        aria-hidden="true"
      />
      {/* Panel */}
      <div
        style={panelStyle}
        className={`rounded-2xl border border-white/5 surface-card shadow-2xl shadow-black/40 p-1.5 max-h-[60vh] overflow-y-auto animate-popover-in-up ${className}`}
        role="dialog"
        aria-modal="false"
        onClick={(e) => e.stopPropagation()}
      >
        {title && (
          <div className="flex items-center justify-between px-2 pt-1 pb-1.5 mb-0.5 border-b border-white/5">
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground/70 font-medium">
              {title}
            </span>
            <button
              onClick={onClose}
              aria-label="Close"
              className="touch-target -mr-1 -mt-0.5 w-6 h-6 rounded-xl text-muted-foreground/70 hover:text-foreground hover:bg-white/5 transition-colors flex items-center justify-center"
            >
              <svg
                width="11"
                height="11"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <line x1="18" y1="6" x2="6" y2="18" />
                <line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          </div>
        )}
        {children}
      </div>
    </>,
    document.body,
  );
}
