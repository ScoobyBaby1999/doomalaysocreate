'use client'

import { useApp } from '../store'
import { api } from '../api-client'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { useEffect, useState } from 'react'
import { BarChart3 } from 'lucide-react'

export function Metrics() {
  const currentSpace = useApp((s) => s.currentSpace)
  const setView = useApp((s) => s.setView)
  const [data, setData] = useState<any>(null)

  useEffect(() => {
    if (currentSpace) {
      api.metrics(currentSpace.id).then(setData).catch(() => setData(null))
    }
  }, [currentSpace])

  if (!currentSpace) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-center">
        <BarChart3 className="size-10 text-muted-foreground mb-3" />
        <p className="text-sm text-muted-foreground mb-3">Select a Space to view metrics.</p>
        <Button onClick={() => setView('spaces')}>Go to Spaces</Button>
      </div>
    )
  }

  const t = data?.totals
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Metrics</h1>
        <p className="text-sm text-muted-foreground">Per-space operational data: calls, success, throttle, latency, tokens.</p>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <Stat label="Calls" value={t?.calls ?? 0} />
        <Stat label="Success" value={`${Math.round((t?.successRate ?? 0) * 100)}%`} />
        <Stat label="429s" value={t?.throttle429 ?? 0} />
        <Stat label="Avg latency" value={`${Math.round(t?.avgLatencyMs ?? 0)}ms`} />
        <Stat label="Tokens" value={(t?.tokensIn ?? 0) + (t?.tokensOut ?? 0)} />
      </div>
      <Card className="border-border/60">
        <CardHeader><CardTitle className="text-sm">By Provider / Model</CardTitle></CardHeader>
        <CardContent>
          {!data?.byProvider?.length ? (
            <p className="text-sm text-muted-foreground py-4 text-center">No data yet. Run the panel or chat to generate metrics.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-muted-foreground border-b border-border/50">
                    <th className="py-1.5 pr-3">Provider</th>
                    <th className="py-1.5 pr-3">Model</th>
                    <th className="py-1.5 pr-3 text-right">Calls</th>
                    <th className="py-1.5 pr-3 text-right">Success</th>
                    <th className="py-1.5 pr-3 text-right">429</th>
                    <th className="py-1.5 pr-3 text-right">Latency</th>
                    <th className="py-1.5 text-right">Tokens</th>
                  </tr>
                </thead>
                <tbody>
                  {data.byProvider.map((r: any, i: number) => (
                    <tr key={i} className="border-b border-border/30">
                      <td className="py-1.5 pr-3 font-medium">{r.provider}</td>
                      <td className="py-1.5 pr-3 font-mono">{r.model}</td>
                      <td className="py-1.5 pr-3 text-right">{r.calls}</td>
                      <td className="py-1.5 pr-3 text-right">{Math.round(r.successRate * 100)}%</td>
                      <td className="py-1.5 pr-3 text-right">{r.throttle429}</td>
                      <td className="py-1.5 pr-3 text-right">{Math.round(r.avgLatencyMs)}ms</td>
                      <td className="py-1.5 text-right">{r.tokensIn + r.tokensOut}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded-lg border border-border/60 bg-card/40 p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-xl font-semibold mt-0.5">{value}</div>
    </div>
  )
}
