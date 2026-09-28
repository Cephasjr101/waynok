# Waynok Design System (MASTER)

Derived via UI/UX Pro Max reasoning rules for: P2P logistics marketplace, dark UI, utilitarian.
Every UI change must conform to this file. Page-specific deviations go in pages/<page>.md.

## Style
- Restrained dark, functional. NO neon, NO AI purple/pink gradients, NO decorative gradients on information.
- Gradient (#ffb020 -> #ff7a1a) allowed ONLY on: primary buttons, logo, active states.
- Information (stats, cards, panels, chat) is FLAT: solid fill, 1px hairline border, soft shadow.

## Color
- Accent:  #ffb020 (amber)   - actions, focus, active nav
- Accent2: #ff7a1a (orange)  - gradient partner only
- BG:      #0b0e14           - page background (flat, no radial washes)
- Panel:   #121828 / #141a28- surfaces
- Line:    #1e2a45           - borders/hairlines
- Text:    #eef1f8 / dim #8f98b3
- Semantic: success #3ecf8e, danger #ff5d5d - states only, never decoration
- One accent. No per-number/per-card color variety.

## Typography
- Display/UI: "Sora" (600-800) - headings, stat numbers, buttons
- Data/mono:  "IBM Plex Mono"  - stat labels, IDs, coordinates, timestamps
- No third typeface. Stat numbers: 700 weight, 24px, flat color - identical everywhere.

## Spacing & Shape
- Radius: cards 12-14px, buttons 12px, chips 999px
- Panel padding 22px; stat padding 16-18px; page gutter 16-24px

## Icons
- MANDATORY: SVG icons (inline or sprite). Phosphor/Heroicons/Lucide sets.
- EMOJIS ARE NOT ICONS. Phase out remaining emoji icons page by page; never add new ones.

## Motion
- Transitions .2s ease; hovers lift 2px max
- MUST respect prefers-reduced-motion (see index.html media query)
- No decorative infinite animations (blob/pulse) on production pages

## Accessibility (non-negotiable)
- Contrast >= 4.5:1 for text
- :focus-visible outline on all interactives (already: 2px amber)
- cursor:pointer on all clickable non-button elements
- Labels never clip: chips wrap or truncate with title= full value
- Responsive check at 375 / 768 / 1024 / 1440
