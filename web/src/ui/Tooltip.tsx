import { type ReactNode } from 'react'
import { Tooltip as TooltipRoot, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'

// Hover label — re-skinned onto the shadcn/Radix Tooltip. Radix portals the content to <body>, so
// it is never clipped by a node card's `overflow: hidden` (it escapes the card's box). Same
// (label, children, side) API as before, so every call site stays unchanged.
export function Tooltip({ label, children, side = 'top' }: {
  label: string; children: ReactNode; side?: 'top' | 'bottom'
}) {
  return (
    <TooltipRoot>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent side={side}>{label}</TooltipContent>
    </TooltipRoot>
  )
}
