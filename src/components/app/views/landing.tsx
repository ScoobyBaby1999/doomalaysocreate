'use client'

import { useApp } from '../store'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Gavel, MessageSquare, Boxes, KeyRound, BarChart3, Wand2, Shield, Zap, Globe } from 'lucide-react'

export function Landing() {
  const setView = useApp((s) => s.setView)

  return (
    <div className="relative">
      {/* hero */}
      <section className="relative overflow-hidden grid-bg">
        <div className="absolute inset-0 bg-gradient-to-b from-transparent via-background/40 to-background pointer-events-none" />
        <div className="relative max-w-5xl mx-auto px-4 sm:px-6 pt-20 pb-24 text-center">
          <div className="inline-flex items-center gap-2 rounded-full border border-primary/30 bg-primary/10 px-3 py-1 text-xs text-primary mb-6 animate-fade-in-up">
            <span className="size-1.5 rounded-full bg-primary animate-pulse-dot" />
            multi-tenant · bring-your-own-keys · frontier panel
          </div>
          <h1 className="text-4xl sm:text-6xl font-bold tracking-tight leading-[1.05] animate-fade-in-up">
            One panel.<br />
            <span className="text-primary">Every frontier model.</span><br />
            Millions of spaces.
          </h1>
          <p className="mt-6 max-w-2xl mx-auto text-base sm:text-lg text-muted-foreground animate-fade-in-up">
            doomalaysocreate fans your prompt out to a diverse panel of frontier LLMs — each on its own
            provider — and merges their uncorrelated opinions into one verdict. Every user brings their
            own API keys. Every Space is an isolated duplicate environment.
          </p>
          <div className="mt-8 flex flex-col sm:flex-row items-center justify-center gap-3 animate-fade-in-up">
            <Button size="lg" className="gap-2 glow-emerald" onClick={() => setView('auth')}>
              Start free — no key needed
            </Button>
            <Button size="lg" variant="outline" onClick={() => setView('auth')}>
              I have an account
            </Button>
          </div>
          <div className="mt-6 text-xs text-muted-foreground">
            Built-in <span className="text-primary font-medium">Z.ai</span> provider works instantly.
            Add your own keys for NVIDIA, Groq, OpenRouter, Cloudflare, GitHub Models & more.
          </div>
        </div>
      </section>

      {/* features */}
      <section className="max-w-6xl mx-auto px-4 sm:px-6 py-16">
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {FEATURES.map((f) => {
            const Icon = f.icon
            return (
              <Card key={f.title} className="p-5 bg-card/60 border-border/60 hover:border-primary/40 transition-colors">
                <div className="size-9 rounded-lg bg-primary/10 text-primary flex items-center justify-center mb-3">
                  <Icon className="size-5" />
                </div>
                <h3 className="font-semibold mb-1">{f.title}</h3>
                <p className="text-sm text-muted-foreground leading-relaxed">{f.desc}</p>
              </Card>
            )
          })}
        </div>
      </section>

      {/* how it works */}
      <section className="max-w-5xl mx-auto px-4 sm:px-6 py-12">
        <h2 className="text-2xl font-bold text-center mb-10">How the panel works</h2>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          {STEPS.map((s, i) => (
            <div key={s.title} className="relative">
              <div className="text-3xl font-bold text-primary/30 mb-2">0{i + 1}</div>
              <h4 className="font-semibold mb-1">{s.title}</h4>
              <p className="text-sm text-muted-foreground">{s.desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* cta */}
      <section className="max-w-3xl mx-auto px-4 sm:px-6 py-20 text-center">
        <div className="text-5xl mb-4">🔥</div>
        <h2 className="text-3xl font-bold mb-3">Spin up your own Space</h2>
        <p className="text-muted-foreground mb-6">
          Create a Space, invite your team, drop in your provider keys, and run the panel.
          Duplicate a Space to fork a fresh environment in one click.
        </p>
        <Button size="lg" className="glow-emerald" onClick={() => setView('auth')}>
          Get started
        </Button>
      </section>
    </div>
  )
}

const FEATURES = [
  {
    icon: Gavel,
    title: 'Judge Panel',
    desc: 'Fan one input out to a diverse panel of frontier LLMs across providers. Merge consensus-tagged bullets. One judge failing never fails the request.',
  },
  {
    icon: MessageSquare,
    title: 'Streaming Chat',
    desc: 'Single-model chat with real-time token streaming. Uses your own provider key, or the zero-config built-in Z.ai provider.',
  },
  {
    icon: Boxes,
    title: 'Duplicate Spaces',
    desc: 'Each Space is an isolated tenant. Fork any Space into a fresh duplicate environment with one click — perfect for teams and templates.',
  },
  {
    icon: KeyRound,
    title: 'Bring Your Own Keys',
    desc: 'Every user stores their own provider API keys, encrypted at rest with AES-256-GCM. Your keys, your quota, your call.',
  },
  {
    icon: Wand2,
    title: 'Templates',
    desc: 'Run multi-stage pipelines: repo audit, design doc, red team, panel debate. Each stage routed to a model big enough for it.',
  },
  {
    icon: BarChart3,
    title: 'Per-Space Metrics',
    desc: 'Track calls, success rate, throttle rate, latency and tokens per provider per model. The substrate for tuning fan-out.',
  },
]

const STEPS = [
  { title: 'You submit', desc: 'Paste a plan, code, or a question. Pick a role and effort.' },
  { title: 'We fan out', desc: 'The input goes to N frontier judges, each on a different provider.' },
  { title: 'Judges reason', desc: 'Each model critiques independently. Async jobs stream partial results.' },
  { title: 'We merge', desc: 'Consensus-tagged bullets. Independent agreement = strongest signal.' },
]
