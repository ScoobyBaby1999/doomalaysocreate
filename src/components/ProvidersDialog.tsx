import { useMemo } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useModelStore, type ProviderGroup } from "../lib/model-store";

function IcoX() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
  );
}
function IcoExternal() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>
  );
}
function IcoKey() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m15.5 7.5 2.3 2.3a1 1 0 0 0 1.4 0l2.1-2.1a1 1 0 0 0 0-1.4L21 5"/><path d="m21 2-9.6 9.6"/><circle cx="7.5" cy="15.5" r="5.5"/></svg>
  );
}
function IcoShield() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/></svg>
  );
}

function confidenceColor(c: "high" | "medium" | "low"): string {
  if (c === "high") return "#22c55e";
  if (c === "medium") return "#f59e0b";
  return "#ef4444";
}

function ProviderCard({ provider }: { provider: ProviderGroup }) {
  return (
    <div
      className="flex flex-col rounded-lg border overflow-hidden"
      style={{ borderColor: `${provider.color}30` }}
    >
      <div
        className="flex items-center gap-2 px-3 shrink-0"
        style={{ height: 30, backgroundColor: `${provider.color}0d`, borderBottom: `1px solid ${provider.color}1f` }}
      >
        <span className="inline-block w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: provider.color }} />
        <span className="text-[12px] font-semibold text-foreground leading-none">{provider.displayName}</span>
        <span className="text-[10px] text-muted-foreground leading-none">{provider.region}</span>
        <span className="text-[10px] text-muted-foreground leading-none ml-auto tabular-nums">{provider.models.length} models</span>
      </div>

      <div className="flex flex-col gap-3 p-3">
        <div className="flex gap-2">
          <span className="shrink-0 mt-px" style={{ color: confidenceColor(provider.privacy.confidence) }}>
            <IcoShield />
          </span>
          <div className="flex flex-col gap-1 min-w-0">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Privacy</span>
            <p className="text-[11px] leading-snug text-foreground">{provider.privacy.notice}</p>
            {provider.privacy.retention && (
              <p className="text-[10px] leading-snug text-muted-foreground">
                <span className="font-medium">Retention:</span> {provider.privacy.retention}
              </p>
            )}
            {provider.privacy.training && (
              <p className="text-[10px] leading-snug text-muted-foreground">
                <span className="font-medium">Training:</span> {provider.privacy.training}
              </p>
            )}
            <div className="flex flex-wrap gap-x-3 gap-y-0.5 mt-0.5">
              {provider.privacy.sources.map((s) => (
                <a
                  key={s}
                  href={s}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-[9px] text-primary/80 hover:text-primary underline underline-offset-2 truncate max-w-[220px] inline-flex items-center gap-0.5"
                  title={s}
                >
                  <IcoExternal /> {new URL(s).hostname.replace("www.", "")}
                </a>
              ))}
            </div>
          </div>
        </div>

        {provider.usageLimits && (
          <div className="flex gap-2">
            <span className="shrink-0 mt-px text-muted-foreground">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2v20M2 12h20"/><circle cx="12" cy="12" r="10"/></svg>
            </span>
            <div className="min-w-0">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground block">Usage limits</span>
              <p className="text-[10px] leading-snug text-foreground">{provider.usageLimits}</p>
            </div>
          </div>
        )}

        <div className="flex flex-wrap gap-2 pt-1">
          <a
            href={provider.settingsUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-xl text-[11px] font-medium text-primary-foreground transition-opacity hover:opacity-90"
            style={{ backgroundColor: provider.color }}
          >
            <IcoKey /> {provider.manageLabel}
            <IcoExternal />
          </a>
        </div>
      </div>
    </div>
  );
}

export function ProvidersDialog() {
  const { providers, providersDialogOpen, providersDialogProvider, closeProvidersDialog } = useModelStore();

  const ordered = useMemo(() => {
    if (!providersDialogProvider) return providers;
    const focused = providers.find((p) => p.name === providersDialogProvider);
    if (!focused) return providers;
    return [focused, ...providers.filter((p) => p.name !== providersDialogProvider)];
  }, [providers, providersDialogProvider]);

  const focusedName = providersDialogProvider
    ? providers.find((p) => p.name === providersDialogProvider)?.displayName
    : null;

  return (
    <AnimatePresence>
      {providersDialogOpen && (
        <>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="fixed inset-0 z-[60] bg-black/40 backdrop-blur-sm"
            onClick={closeProvidersDialog}
            aria-hidden="true"
          />
          <motion.div
            initial={{ opacity: 0, scale: 0.97, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 8 }}
            transition={{ duration: 0.2, ease: [0.19, 1, 0.22, 1] }}
            className="fixed inset-0 z-[60] flex items-center justify-center p-3 sm:p-6 pointer-events-none"
          >
            <div
              className="pointer-events-auto w-full max-w-3xl flex flex-col rounded-xl border border-border/70 bg-background/95 backdrop-blur-xl shadow-2xl shadow-black/20 overflow-hidden"
              style={{ height: "min(82vh, 680px)" }}
              onClick={(e) => e.stopPropagation()}
              role="dialog"
              aria-modal="true"
              aria-label="Providers & privacy settings"
            >
              <div className="flex items-center justify-between gap-3 px-4 shrink-0" style={{ height: 40, borderBottom: "1px solid var(--border)" }}>
                <div className="flex items-center gap-2 min-w-0">
                  <span className="text-[13px] font-semibold text-foreground">Providers &amp; Privacy</span>
                  {focusedName && (
                    <span className="text-[10px] text-muted-foreground">· focused on {focusedName}</span>
                  )}
                </div>
                <button
                  onClick={closeProvidersDialog}
                  className="flex items-center justify-center size-6 rounded-xl text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors"
                  aria-label="Close"
                >
                  <IcoX />
                </button>
              </div>

              <div className="flex-1 min-h-0 overflow-y-auto">
                <div className="flex flex-col gap-3 p-4">
                  <div className="rounded-xl border border-dashed border-border/60 bg-muted/30 px-3 py-2">
                    <p className="text-[10px] leading-snug text-muted-foreground">
                      <span className="font-semibold text-foreground">Temporary providers screen.</span> Use the
                      buttons below to open each provider&apos;s console and disable data-retention / training
                      toggles for maximum privacy. A dedicated providers screen is planned.
                    </p>
                  </div>

                  {ordered.map((p) => (
                    <ProviderCard key={p.name} provider={p} />
                  ))}
                </div>
              </div>

              <div className="flex items-center justify-between px-4 shrink-0" style={{ height: 32, borderTop: "1px solid var(--border)" }}>
                <span className="text-[10px] text-muted-foreground">
                  privacy data sourced from official docs · <span className="text-green-600">✓ high</span> / <span className="text-amber-600">⚠ medium</span> / <span className="text-red-600">✗ low</span> confidence
                </span>
                <span className="text-[10px] text-muted-foreground">
                  <kbd className="px-1 py-px rounded bg-muted border border-border text-[9px] font-mono">esc</kbd> to close
                </span>
              </div>
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
